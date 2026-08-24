"""
Deterministic matcher. Does the easy 70-80% with zero LLM calls:
  Stage A: ledger <-> settlement, keyed on order_id, amount footing checked
  Stage B: settlement <-> bank, keyed on UTR quoted in the bank narration

Stage B is settlement-driven, not bank-driven: it walks the settlements and
gives every one a verdict, so "Razorpay says it paid out and nothing ever hit
the bank" is a reported exception rather than an absence of evidence. A payout
may cover several settlements at once (Razorpay batches them under one UTR), so
the amount check is against the batch total, not a single order.

Anything that fails deterministic matching is handed onward to the fuzzy tier
and then llm_resolver.py -- never silently dropped, never force-matched.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

AMOUNT_TOLERANCE = 1.0  # paise-level rounding tolerance, in rupees
BANK_DATE_WINDOW_DAYS = 5  # settlement_date .. settlement_date + N is "normal"

UTR_RE = re.compile(r"UTR[:\-]?\s*([A-Z0-9]{6,})", re.IGNORECASE)

# Ledger statuses that show the merchant knows a refund happened.
REFUND_AWARE_STATUSES = {"refunded", "partially_refunded", "refund_processed"}


@dataclass
class MatchResult:
    order_id: str
    stage: str                 # "ledger_settlement" | "settlement_bank"
    status: str                # "matched" | "exception" | "needs_llm"
    reason_code: str = ""
    basis: str = ""
    confidence: float = 1.0
    detail: dict = field(default_factory=dict)


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d")


def _field(row, name, default=0.0):
    """Tolerates sources that predate a column (e.g. refund_amount)."""
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return default
    return value


# --------------------------------------------------------------------------
# Stage A: internal ledger <-> Razorpay settlement
# --------------------------------------------------------------------------

def _ledger_reflects_refund(led, gross, refund):
    """
    A merchant has booked a refund if their ledger either carries the net amount
    or carries a refund-aware status. Booking the full gross with a plain 'paid'
    status means the refund never made it into their books.
    """
    if abs(float(led["amount"]) - (gross - refund)) <= AMOUNT_TOLERANCE:
        return True
    return str(_field(led, "status", "")).lower() in REFUND_AWARE_STATUSES


def match_ledger_to_settlement(ledger_df, settlement_df):
    """Stage A. Returns (results: list[MatchResult], settled_order_ids: set)."""
    results = []
    settled_ids = set()

    settlement_by_order = {}
    for _, row in settlement_df.iterrows():
        settlement_by_order.setdefault(row["order_id"], []).append(row)

    for _, led in ledger_df.iterrows():
        oid = led["order_id"]
        candidates = settlement_by_order.get(oid, [])

        if not candidates:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="no_settlement_found",
                basis="no settlement row exists for this order_id",
            ))
            continue

        if len(candidates) > 1:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="duplicate_settlement",
                basis=f"{len(candidates)} settlement rows found for one order_id",
                detail={"settlement_ids": [c["settlement_id"] for c in candidates]},
            ))
            continue

        stl = candidates[0]
        gross = float(stl["gross_amount"])
        refund = float(_field(stl, "refund_amount", 0.0))
        expected_settled = round(gross - float(stl["fee"]) - float(stl["tax"]) - refund, 2)
        actual_settled = round(float(stl["settled_amount"]), 2)
        ledger_amount = float(led["amount"])

        # The ledger may legitimately carry gross (refund not yet booked) or net
        # (refund booked). Anything else is a genuine amount disagreement.
        books_gross = abs(ledger_amount - gross) <= AMOUNT_TOLERANCE
        books_net = abs(ledger_amount - (gross - refund)) <= AMOUNT_TOLERANCE
        if not (books_gross or books_net):
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="ledger_gross_amount_mismatch",
                basis=f"ledger={ledger_amount} vs settlement_gross={gross}",
            ))
            continue

        if abs(expected_settled - actual_settled) > AMOUNT_TOLERANCE:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="fee_footing_mismatch",
                basis=(f"gross-fee-tax-refund={expected_settled} vs "
                       f"actual_settled={actual_settled}"),
                detail={"fee": float(stl["fee"]), "tax": float(stl["tax"]),
                        "refund_amount": refund},
            ))
            continue

        # Footing is correct, so Razorpay did the arithmetic right. If a refund
        # was part of that arithmetic and the merchant's books never took it,
        # the merchant is overstating revenue -- its own reason code.
        if refund > 0 and not _ledger_reflects_refund(led, gross, refund):
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="refund_not_reflected",
                basis=(f"settlement nets a refund of {refund} but ledger still "
                       f"books {ledger_amount} with status="
                       f"'{_field(led, 'status', '')}'"),
                detail={"refund_amount": refund, "ledger_amount": ledger_amount},
            ))
            continue

        results.append(MatchResult(
            order_id=oid, stage="ledger_settlement", status="matched",
            basis="order_id + gross_amount + fee/tax/refund footing all agree",
        ))
        settled_ids.add(oid)

    return results, settled_ids


# --------------------------------------------------------------------------
# Stage B: Razorpay settlement <-> bank statement
# --------------------------------------------------------------------------

def build_utr_index(known_utrs):
    """
    Bucket the known UTRs by length: {length: {normalised_utr: original_utr}}.

    The naive version of the lookup below scanned every settlement for every
    bank row, which is quadratic and cost ~70% of throughput on a 5k batch.
    Indexing makes the per-row cost depend on narration length instead of on
    how many settlements exist.
    """
    by_len = {}
    for utr in known_utrs:
        normalised = str(utr).upper()
        by_len.setdefault(len(normalised), {})[normalised] = utr
    return by_len


def extract_utr_from_narration(narration, known_utrs=None, index=None):
    """
    Regex/normalisation tier. Strips the separators banks sprinkle through a
    narration, then looks for a UTR we already know about. It can only ever
    return a UTR that exists in the settlement data -- it never invents one.
    Longest-first so a short UTR can't shadow a longer one containing it.
    """
    if index is None:
        index = build_utr_index(known_utrs or ())

    normalized = re.sub(r"[^A-Z0-9]", "", str(narration).upper())
    hits = []
    claimed = []  # character spans already accounted for by a longer match

    # Longest first, so a genuine reference claims its span before any shorter
    # UTR nested inside it can be counted as a second, separate reference.
    for length in sorted(index, reverse=True):
        bucket = index[length]
        for start in range(len(normalized) - length + 1):
            hit = bucket.get(normalized[start:start + length])
            if hit is None:
                continue
            end = start + length
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue  # sits inside a reference we already read
            claimed.append((start, end))
            if hit not in hits:
                hits.append(hit)

    # A narration quoting two known UTRs -- "reversal of X, credit for Y" -- is
    # ambiguous, and taking whichever appears first would confidently pick the
    # reversal. Deterministic code cannot tell which reference is the credit
    # without reading the words around it, so it refuses and hands off.
    if len(hits) == 1:
        return hits[0]
    return None


def link_bank_rows(bank_df, known_utrs, extra_links=None):
    """
    Returns (utr -> [bank rows], unresolved bank rows).

    extra_links maps txn_id -> utr and carries proposals from the later tiers
    (fuzzy, then LLM). A proposal only decides which settlement a credit is
    *compared against*; it never decides whether the credit matches. Every link
    in here still goes through the amount and date checks below.
    """
    extra_links = extra_links or {}
    by_utr = {}
    unresolved = []
    index = build_utr_index(known_utrs)  # built once, not once per bank row

    for _, bank_row in bank_df.iterrows():
        utr = extract_utr_from_narration(bank_row["narration"], index=index)
        if utr is None:
            proposed = extra_links.get(bank_row["txn_id"])
            # a proposal is only usable if it names a settlement we actually hold
            utr = proposed if proposed in known_utrs else None
        if utr is None:
            unresolved.append(bank_row)
        else:
            by_utr.setdefault(utr, []).append(bank_row)

    return by_utr, unresolved


def tolerance_for(legs):
    """Rounding tolerance accumulates across the legs of a batched payout."""
    return AMOUNT_TOLERANCE * max(1, len(legs))


def match_settlement_to_bank(settlement_df, bank_df, extra_links=None):
    """
    Stage B. Settlement-driven, so every settlement gets a verdict.
    Returns (results, unresolved_bank_rows) -- unresolved go to the later tiers.
    """
    results = []

    known_utrs = {row["utr"] for _, row in settlement_df.iterrows()}
    bank_by_utr, unresolved = link_bank_rows(bank_df, known_utrs, extra_links)

    # Razorpay pays out a batch of settlements under one UTR; group accordingly.
    batches = {}
    for _, row in settlement_df.iterrows():
        batches.setdefault(row["utr"], []).append(row)

    for utr, batch in batches.items():
        # A UTR covering the same order_id more than once is a duplicated
        # settlement row, not a batch of several orders. Stage A already reports
        # that as duplicate_settlement; Stage B must not let the resulting
        # double-count masquerade as the bank paying the wrong amount.
        legs_by_order = {}
        for row in batch:
            legs_by_order.setdefault(row["order_id"], row)
        legs = list(legs_by_order.values())
        has_duplicates = len(legs) < len(batch)

        order_ids = list(legs_by_order.keys())
        expected = round(sum(float(row["settled_amount"]) for row in legs), 2)
        credits = bank_by_utr.get(utr, [])

        if not credits:
            # Two very different problems wear the same face here, and a
            # controller triages them oppositely: money that never arrived is
            # escalated to Razorpay, money that arrived under an unreadable
            # narration is a parsing problem. Distinguish them -- but note that
            # NEITHER becomes a match. An amount lining up is a triage hint, not
            # evidence, and both stay exceptions.
            candidates = [r for r in unresolved
                          if abs(float(r["amount"]) - expected) <= tolerance_for(legs)]
            if candidates:
                reason = "credit_unattributed"
                basis = (f"settlement UTR {utr} has no readable bank credit, but "
                         f"{len(candidates)} unattributed credit(s) match the expected "
                         f"{expected} — narration unreadable, NOT matched on amount")
                detail = {"utr": utr, "expected_amount": expected,
                          "candidate_txn_ids": [r["txn_id"] for r in candidates]}
            else:
                reason = "settlement_not_credited"
                basis = (f"settlement UTR {utr} has no matching bank credit and no "
                         f"unattributed credit for {expected} — money never arrived")
                detail = {"utr": utr, "expected_amount": expected}

            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code=reason, basis=basis, detail=detail,
                ))
            continue

        actual = round(sum(float(c["amount"]) for c in credits), 2)
        tolerance = tolerance_for(legs)
        batch_note = (f"batch of {len(legs)} settlements under one payout"
                      if len(legs) > 1 else "single settlement")
        if has_duplicates:
            batch_note += (f" ({len(batch) - len(legs)} duplicate settlement row(s) "
                           f"ignored here, reported by Stage A)")

        if abs(actual - expected) > tolerance:
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="bank_amount_mismatch",
                    basis=f"bank credited {actual} vs settlement total {expected} ({batch_note})",
                    detail={"utr": utr, "bank_amount": actual, "settled_total": expected},
                ))
            continue

        latest_settlement = max(_parse_date(row["settlement_date"]) for row in legs)
        latest_credit = max(_parse_date(c["date"]) for c in credits)
        date_gap = (latest_credit - latest_settlement).days

        if date_gap > BANK_DATE_WINDOW_DAYS:
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="bank_credit_delayed",
                    basis=(f"bank credit {date_gap} days after settlement "
                           f"(window={BANK_DATE_WINDOW_DAYS})"),
                    detail={"utr": utr, "date_gap_days": date_gap},
                ))
            continue

        for oid in order_ids:
            results.append(MatchResult(
                order_id=oid, stage="settlement_bank", status="matched",
                basis=(f"UTR {utr} match, amount agrees ({batch_note}), "
                       f"{date_gap}d gap within window"),
                detail={"utr": utr},
            ))

    return results, unresolved
