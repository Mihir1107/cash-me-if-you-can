"""
Orchestrator. Runs the three-way reconciliation and decides, per stage, what
happens when a source is unusable.

Tier order for bank narrations, cheapest first:
    1. matcher.link_bank_rows      regex/normalisation, deterministic, free
    2. fuzzy_resolver              string recovery,     deterministic, free
    3. llm_resolver                one LLM call per row that survived 1 and 2

Tiers 2 and 3 only ever *propose* which settlement a bank credit belongs to.
Stage B then runs once over everything and does the amount, batch-total and
date-window verification itself, so a proposal from any tier faces exactly the
same checks a clean regex match does.
"""

import time

import pandas as pd

from src.matcher import (
    MatchResult,
    link_bank_rows,
    match_ledger_to_settlement,
    match_settlement_to_bank,
)
from src.fuzzy_resolver import resolve_unresolved_bank_rows as fuzzy_resolve
from src.llm_resolver import resolve_unresolved_bank_rows as llm_resolve
from src.audit import AuditTrail, verify_chain
from src.close_gate import evaluate_close
from src.money import build_exposure_index
from src.report import build_report, print_summary, save_report
from src import schema

REQUIRED_COLUMNS = {
    "ledger": {"ledger_id", "order_id", "amount", "date"},
    "settlements": {"settlement_id", "order_id", "gross_amount", "fee", "tax",
                    "settled_amount", "settlement_date", "utr"},
    "bank": {"txn_id", "date", "amount", "narration"},
}


class SourceUnavailable(Exception):
    """
    A source file is missing, unreadable, or structurally wrong.

    Carries the facts as attributes rather than only in the message. Callers
    that need to explain this to a person -- the web app does -- were parsing
    the prose back out with string splits, which broke the moment the wording
    changed. The wording is for humans; these are for code.
    """

    def __init__(self, message, source=None, missing=None, columns=None):
        super().__init__(message)
        self.source = source              # "ledger" | "settlements" | "bank"
        self.missing = list(missing or [])   # required fields not resolved
        self.columns = list(columns or [])   # what the file actually has


def propose_and_verify(df, found, name, filename=None):
    """
    Let the model propose the columns the alias table could not place, then
    check the proposal against the file before believing any of it.

    Same boundary as the narration tier. The model reads header names -- a
    language problem, and the only part of a finance export carrying no
    financial data -- and proposes. `verify_mapping` then does arithmetic on the
    real rows: amounts must be numeric, dates must parse, identifiers must
    identify, and a settlement must foot against its own parts.

    A failed proposal is discarded WHOLE, not partially kept. A mapping is a
    single claim about what a file means: if the model confused gross and
    settled, nothing it said about that file has earned any trust.

    The alias table always wins where it was confident -- the model may only
    fill gaps -- and anything still unfilled goes to the human.
    """
    from src import schema_llm

    proposal = schema_llm.propose_mapping(df.columns, name,
                                          filename=filename or name)
    if not proposal["mapping"]:
        out = dict(found)
        if proposal.get("note"):
            out["llm_note"] = proposal["note"]
        return out

    # deterministic wins; the model only fills what the table left empty
    merged = dict(proposal["mapping"])
    merged.update(found["mapping"])

    spec = schema.FIELDS[name]
    if not all(f in merged for f in spec["required"]):
        return found

    verdict = schema.verify_mapping(df, merged, name)
    out = dict(found)
    if not verdict["ok"]:
        out["llm_rejected"] = verdict["failures"]
        return out

    out["mapping"] = merged
    out["ready"] = True
    out["llm_filled"] = sorted(set(merged) - set(found["mapping"]))
    out["llm_verified"] = [c["check"] for c in verdict["checks"] if c["ok"]]
    out["unresolved"] = [u for u in found["unresolved"] if u["field"] not in merged]
    return out


def load_source(path, name, mapping=None, allow_llm=False):
    """
    Read one CSV and get it into the column names this pipeline relies on.

    Nobody's real export uses these names, so `src/schema.py` maps theirs onto
    ours -- deterministically, by a curated alias table, never by guessing. Pass
    an explicit `mapping` ({field: their column}) to override, which is what the
    web app sends once a human has confirmed the columns it could not place.

    `allow_llm` lets the model propose the columns the table could not place --
    header names only, never data -- and every proposal is checked against the
    file before it is used. Off by default: a run that silently depends on a
    network call is not the default anyone should get.

    Raises SourceUnavailable when a required field cannot be resolved. The
    message carries the columns the file actually has, because "missing
    order_id" is unhelpful next to a file whose column is called "Order Ref".
    """
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise SourceUnavailable(
            f"{name} could not be read from {path}: {e}", source=name) from e

    if mapping:
        resolved = dict(mapping)
    else:
        found = schema.resolve(df.columns, name)
        if found["split_amount"] and not found["ready"]:
            # a statement with separate debit and credit columns: a different
            # shape rather than a different spelling, so convert before mapping
            df = schema.merge_split_amount(df, found["split_amount"]["debit"],
                                           found["split_amount"]["credit"])
            found = schema.resolve(df.columns, name)

        if not found["ready"] and allow_llm:
            found = propose_and_verify(df, found, name)

        if not found["ready"]:
            missing = [u["field"] for u in found["unresolved"] if u["required"]]
            raise SourceUnavailable(
                f"{name} at {path} is missing required columns: {sorted(missing)}"
                f" (columns found: {list(df.columns)})",
                source=name, missing=sorted(missing), columns=list(df.columns),
            )
        resolved = found["mapping"]

    absent = [c for c in resolved.values() if c not in df.columns]
    if absent:
        raise SourceUnavailable(
            f"{name} at {path} was mapped to columns it does not have: {absent}",
            source=name, columns=list(df.columns),
        )

    missing = REQUIRED_COLUMNS[name] - set(resolved)
    if missing:
        raise SourceUnavailable(
            f"{name} at {path} is missing required columns: {sorted(missing)}",
            source=name, missing=sorted(missing), columns=list(df.columns),
        )

    out = schema.apply_mapping(df, resolved)
    # How the columns were decided travels with the frame, because the
    # cross-source check downstream treats a model's guess and a person's
    # confirmed choice differently -- and it should.
    out.attrs["schema_decided_by"] = (
        "explicit" if mapping else
        "model" if found.get("llm_filled") else
        "table")
    return out


def _degrade_stage_b(settlements, reason):
    """
    Bank source unusable. Every settlement becomes an explicit exception rather
    than silently disappearing -- an unverifiable settlement must never end up
    on the matched side of the ledger just because we couldn't read the bank.
    """
    # One verdict per order, not per settlement row. The healthy Stage B path
    # collapses duplicate settlement rows into one leg per order; this path was
    # written separately and never picked that up, so a duplicated settlement
    # produced two identical exceptions and inflated the headline count.
    seen = []
    for _, row in settlements.iterrows():
        if row["order_id"] not in seen:
            seen.append(row["order_id"])

    return [
        MatchResult(
            order_id=order_id, stage="settlement_bank", status="exception",
            reason_code="bank_source_unavailable",
            basis=f"bank statement unusable, settlement could not be verified: {reason}",
            confidence=0.0,
        )
        for order_id in seen
    ]


def _order_universe(ledger, settlements):
    """Every order either side knows about, counted once."""
    ids = set()
    if len(ledger):
        ids |= set(ledger["order_id"])
    if len(settlements):
        ids |= set(settlements["order_id"])
    return len(ids)


def _throughput(order_count, bank, timings, t_start, llm_calls):
    """
    The brief asks for throughput alongside accuracy. Report it honestly: total
    wall-clock, per-stage breakdown, and how many LLM round-trips it actually
    took -- the last one being the number that scales with cost.
    """
    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 2)
    bank_rows = 0 if bank is None else len(bank)
    total_rows = order_count + bank_rows
    return {
        "wall_clock_ms": elapsed_ms,
        "orders": order_count,
        "bank_rows": bank_rows,
        "records_processed": total_rows,
        "records_per_second": round(total_rows / (elapsed_ms / 1000), 1) if elapsed_ms else 0.0,
        # actual API round-trips, not rows offered to the tier. With no key the
        # tier short-circuits without calling anything, and this stays 0.
        "llm_calls": llm_calls,
        "llm_calls_per_100_orders": round(100 * llm_calls / order_count, 2) if order_count else 0.0,
        "stage_ms": timings,
    }


def run_reconciliation(data_dir="data", output_dir="output",
                       enable_fuzzy=True, enable_llm=True, write_outputs=True,
                       column_mappings=None, allow_llm_schema=False):
    """
    enable_fuzzy / enable_llm exist so the evaluator can ablate the tiers and
    measure what each one is actually worth. Turning a tier off never turns a
    row into a match -- it turns it into an honest exception.

    column_mappings is {source: {field: their column}}, for files whose headers
    the alias table could not place on its own. It only ever arrives from a
    human who was shown the columns and chose -- nothing guesses one.
    """
    column_mappings = column_mappings or {}
    audit = AuditTrail()
    timings = {}
    t_start = time.perf_counter()

    def stage(name, fn):
        """Wall-clock per stage. The brief asks for throughput, so measure it."""
        t0 = time.perf_counter()
        result = fn()
        timings[name] = round((time.perf_counter() - t0) * 1000, 2)
        return result

    # The ledger and the settlement file are the backbone: without both there is
    # no reconciliation to run, and pretending otherwise would report a match
    # rate computed over nothing. Fail loudly instead.
    ledger, settlements = stage("load_sources", lambda: (
        load_source(f"{data_dir}/internal_ledger.csv", "ledger",
                    column_mappings.get("ledger"), allow_llm_schema),
        load_source(f"{data_dir}/razorpay_settlements.csv", "settlements",
                    column_mappings.get("settlements"), allow_llm_schema),
    ))

    # Three views of the same money, so that they relate is itself checkable --
    # and it is the only check with any teeth on the ledger and bank sides,
    # neither of which has an internal identity the way settlements do.
    source_check = schema.verify_sources(ledger, settlements)
    inferred = [f for f in (ledger, settlements)
                if f.attrs.get("schema_decided_by") == "model"]
    if inferred and not source_check["ok"]:
        raise SourceUnavailable(
            "the columns proposed for these files do not describe the same "
            "money: " + "; ".join(source_check["failures"]),
            source="ledger", columns=list(ledger.columns),
        )

    # --- Stage A: ledger <-> settlement (deterministic) ---
    stage_a_results, _ = stage(
        "stage_a_ledger_settlement",
        lambda: match_ledger_to_settlement(ledger, settlements))
    for r in stage_a_results:
        audit.log_match_result(r)

    # The bank statement is different: it is the third source, and a merchant
    # who can't download it today still needs their ledger/settlement backbone
    # reconciled. Losing it degrades Stage B, it doesn't kill the batch.
    bank = None
    bank_error = None
    try:
        bank = load_source(f"{data_dir}/bank_statement.csv", "bank",
                           column_mappings.get("bank"), allow_llm_schema)
        bank_check = schema.verify_sources(ledger, settlements, bank)
        if bank.attrs.get("schema_decided_by") == "model" and not bank_check["ok"]:
            raise SourceUnavailable(
                "the columns proposed for the bank statement do not line up "
                "with the settlements: " + "; ".join(bank_check["failures"]),
                source="bank", columns=list(bank.columns),
            )
        source_check = bank_check
    except SourceUnavailable as e:
        bank_error = str(e)

    fuzzy_links, llm_links, llm_exceptions = [], [], []

    if bank is None:
        stage_b_results = _degrade_stage_b(settlements, bank_error)
        audit.log(None, "settlement_bank", "degraded",
                  f"Stage B degraded, all settlements unverified: {bank_error}",
                  confidence=0.0)
    else:
        known_utrs = {row["utr"] for _, row in settlements.iterrows()}

        # --- Tier 1: deterministic narration parse ---
        # Kept whole, not just the unresolved slice: Stage B needs the same
        # partition, and rescanning every narration to rebuild it doubled the
        # dominant O(N) cost of the deterministic pipeline for no verdict change.
        partition = stage("tier1_narration_regex",
                          lambda: link_bank_rows(bank, known_utrs))
        _, unresolved, _ = partition

        # --- Tier 2: deterministic fuzzy recovery, still zero LLM calls ---
        if enable_fuzzy:
            fuzzy_links, still_unresolved = stage(
                "tier2_narration_fuzzy",
                lambda: fuzzy_resolve(unresolved, known_utrs))
        else:
            still_unresolved = unresolved

        # --- Tier 3: LLM proposes on whatever is genuinely free text ---
        if enable_llm:
            llm_links, llm_exceptions = stage(
                "tier3_narration_llm",
                lambda: llm_resolve(still_unresolved, known_utrs))
        else:
            llm_exceptions = [{
                "bank_row": row, "reason_code": "narration_unresolved",
                "basis": "LLM tier disabled for this run",
            } for row in still_unresolved]

        # --- Stage B: one deterministic verification pass over everything ---
        extra_links = {link["txn_id"]: link["utr_candidate"]
                       for link in fuzzy_links + llm_links}
        stage_b_results, _ = stage(
            "stage_b_settlement_bank",
            lambda: match_settlement_to_bank(settlements, bank, extra_links,
                                             partition=partition))

    for r in stage_b_results:
        audit.log_match_result(r)

    for link in fuzzy_links:
        audit.log(None, "narration_fuzzy", "link_proposed", link["basis"],
                  confidence=link["confidence"],
                  detail={"txn_id": link["txn_id"], "utr": link["utr_candidate"]})
    for link in llm_links:
        audit.log(None, "narration_llm", "link_proposed", link["basis"],
                  confidence=link["confidence"],
                  detail={"txn_id": link["txn_id"], "utr": link["utr_candidate"]})
    for e in llm_exceptions:
        audit.log(None, "narration_llm", "exception", e["basis"], confidence=0.0,
                  detail={"narration": e["bank_row"]["narration"],
                          "txn_id": e["bank_row"]["txn_id"]})

    # The order universe is both sides, not just the ledger. A settlement for an
    # order the merchant never booked is a real finding, so it has to be in the
    # denominator too, or reconciled + unreconciled stops equalling total.
    # Distinct ids, not rows: a double-booked order is one order with a problem,
    # not two orders. Computed once, because the report and the throughput block
    # shipping different order counts is exactly what happened when it was not.
    order_count = _order_universe(ledger, settlements)

    report = build_report(
        total_orders=order_count,
        stage_a_results=stage_a_results,
        stage_b_results=stage_b_results,
        fuzzy_links=fuzzy_links,
        llm_links=llm_links,
        llm_exceptions=llm_exceptions,
        bank_error=bank_error,
        source_diagnostics=source_check,
        exposure_index=build_exposure_index(ledger, settlements),
        throughput=_throughput(order_count, bank, timings, t_start,
                               llm_calls=sum(1 for r in llm_links + llm_exceptions
                                             if r.get("llm_invoked"))),
    )

    # The close decision is evaluated last, because one of its conditions is
    # whether the decision log verifies against its own hash chain, and that
    # cannot be known until the log has been written.
    audit_status = None
    if write_outputs:
        audit_path = f"{output_dir}/audit_trail.jsonl"
        audit.save(audit_path)
        audit_status = verify_chain(audit_path)

    report["audit_trail"] = audit_status or {}
    report["close_gate"] = evaluate_close(report, audit_status)

    if write_outputs:
        save_report(report, f"{output_dir}/reconciliation_report.json")

    return report, audit


if __name__ == "__main__":
    report, audit = run_reconciliation()
    print_summary(report)
    print("\nFull audit trail: output/audit_trail.jsonl")
    print("Full report:      output/reconciliation_report.json")
