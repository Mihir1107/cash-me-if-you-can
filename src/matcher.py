"""
Deterministic matcher. Does the easy 70-80% with zero LLM calls:
  Stage A: ledger <-> settlement, keyed on order_id, amount footing checked
  Stage B: settlement <-> bank, keyed on the payment reference quoted in the
           bank narration -- matched by normalised substring against references
           we already hold, never by a format-specific regex, so it is not tied
           to any one bank's narration conventions (see tests/test_generalize.py)

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
    """Returns None rather than raising. A date we cannot read is an exception
    for that row, never a reason to abort the batch."""
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _num(row, name):
    """
    Strict numeric read: returns None for missing, blank, NaN or non-numeric.

    This exists because NaN silently defeats every comparison in this file --
    `abs(nan - x) > tolerance` is False, so a blank amount passed every check
    and came out MATCHED. A value we cannot read is never evidence of agreement.
    """
    try:
        value = row[name]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN


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

    # One verdict per distinct order_id. A merchant whose export lists the same
    # order twice has a double-booking problem of its own, and collapsing the
    # rows here keeps "orders reconciled + orders excepted == orders" true.
    ledger_by_order = {}
    for _, row in ledger_df.iterrows():
        ledger_by_order.setdefault(row["order_id"], []).append(row)

    # Razorpay can settle an order the merchant never booked. Iterating the
    # ledger alone made that money invisible: no Stage A verdict, so it never
    # reached the report at all, and a batch containing one still read as 100%
    # reconciled. It is the mirror image of no_settlement_found, and it is the
    # more dangerous of the two, because the failure is silent and flattering.
    for oid in settlement_by_order:
        if oid in ledger_by_order:
            continue
        rows = settlement_by_order[oid]
        total = sum(_num(r, "settled_amount") or 0.0 for r in rows)
        results.append(MatchResult(
            order_id=oid, stage="ledger_settlement", status="exception",
            reason_code="no_ledger_entry",
            basis=(f"Razorpay settled {round(total, 2)} for this order but the "
                   f"merchant's ledger has no entry for it"),
            detail={"settlement_ids": [str(r["settlement_id"]) for r in rows],
                    "settled_amount": round(total, 2)},
        ))

    for oid, ledger_rows in ledger_by_order.items():
        if len(ledger_rows) > 1:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="duplicate_ledger_entry",
                basis=f"{len(ledger_rows)} ledger rows book the same order_id",
                detail={"ledger_ids": [str(r["ledger_id"]) for r in ledger_rows]},
            ))
            continue

        led = ledger_rows[0]
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
        gross = _num(stl, "gross_amount")
        fee = _num(stl, "fee")
        tax = _num(stl, "tax")
        actual = _num(stl, "settled_amount")
        ledger_amount = _num(led, "amount")
        refund = _num(stl, "refund_amount") or 0.0

        missing = [name for name, value in (
            ("ledger.amount", ledger_amount), ("gross_amount", gross),
            ("fee", fee), ("tax", tax), ("settled_amount", actual),
        ) if value is None]
        if missing:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="source_value_missing",
                basis=f"cannot verify: missing or non-numeric {', '.join(missing)}",
                detail={"missing_fields": missing},
            ))
            continue

        expected_settled = round(gross - fee - tax - refund, 2)
        actual_settled = round(actual, 2)

        # Fees and tax cannot exceed the transaction they are charged on. Such a
        # settlement foots perfectly -- gross - fee - tax really does equal the
        # negative settled_amount -- so every arithmetic check passes it, and it
        # reaches the bank stage looking valid. It is still nonsense: a processor
        # does not charge more to process a sale than the sale was worth.
        # Found by the property suite, not by a case I thought to write.
        if fee + tax > gross + AMOUNT_TOLERANCE:
            results.append(MatchResult(
                order_id=oid, stage="ledger_settlement", status="exception",
                reason_code="fee_exceeds_gross",
                basis=(f"fee {fee} + tax {tax} exceed gross {gross} — "
                       f"settlement nets {actual_settled}"),
                detail={"fee": fee, "tax": tax, "gross_amount": gross},
            ))
            continue

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


def scan_non_overlapping(normalized, index):
    """
    Find every distinct reference quoted in a narration, longest first, where a
    reference nested inside a longer one does not count as a second reference.

    `index` is {length: {key: value}}. Values are returned in the order their
    spans were claimed, de-duplicated.

    This is shared with the fuzzy tier deliberately. Both tiers scan a narration
    for known references, and when the two had their own copies of this loop
    they drifted: the matcher gained position-claiming and the fuzzy tier did
    not, so a narration quoting UTR100005 in a book that also held UTR10000 was
    resolved by one and refused by the other. One implementation, one behaviour.
    """
    hits = []
    claimed = []  # character spans already accounted for by a longer match

    for length in sorted(index, reverse=True):
        bucket = index[length]
        for start in range(len(normalized) - length + 1):
            found = bucket.get(normalized[start:start + length])
            if found is None:
                continue
            end = start + length
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue  # sits inside a reference we already read
            claimed.append((start, end))
            for value in (found if isinstance(found, list) else [found]):
                if value not in hits:
                    hits.append(value)
    return hits


def normalize_reference_text(text):
    """Strip the separators banks sprinkle through a narration."""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def extract_utr_from_narration(narration, known_utrs=None, index=None):
    """
    Regex/normalisation tier. Strips the separators banks sprinkle through a
    narration, then looks for a UTR we already know about. It can only ever
    return a UTR that exists in the settlement data -- it never invents one.
    """
    if index is None:
        index = build_utr_index(known_utrs or ())

    hits = scan_non_overlapping(normalize_reference_text(narration), index)

    # A narration quoting two known UTRs -- "reversal of X, credit for Y" -- is
    # ambiguous, and taking whichever appears first would confidently pick the
    # reversal. Deterministic code cannot tell which reference is the credit
    # without reading the words around it, so it refuses and hands off.
    if len(hits) == 1:
        return hits[0]
    return None


def apply_link_proposals(partition, extra_links, known_utrs):
    """
    Fold the later tiers' proposals into an existing partition.

    Scanning every narration again just to place a handful of proposals would
    redo the whole batch's O(N) work for an O(proposals) change, so this moves
    only the rows a tier actually spoke for. A proposal naming a settlement we
    do not hold is discarded here, exactly as it is on the scanning path: the
    verification gate does not move just because the arithmetic got cheaper.
    """
    by_utr, unresolved, non_credits = partition
    if not extra_links:
        return by_utr, unresolved, non_credits

    linked = {utr: list(rows) for utr, rows in by_utr.items()}
    still_unresolved = []
    for row in unresolved:
        proposed = extra_links.get(row["txn_id"])
        if proposed in known_utrs:
            linked.setdefault(proposed, []).append(row)
        else:
            still_unresolved.append(row)
    return linked, still_unresolved, non_credits


def link_bank_rows(bank_df, known_utrs, extra_links=None, index=None):
    """
    Returns (utr -> [bank rows], unresolved bank rows, non-credit rows).

    extra_links maps txn_id -> utr and carries proposals from the later tiers
    (fuzzy, then LLM). A proposal only decides which settlement a credit is
    *compared against*; it never decides whether the credit matches. Every link
    in here still goes through the amount and date checks below.
    """
    by_utr = {}
    unresolved = []
    non_credits = []
    if index is None:
        index = build_utr_index(known_utrs)  # built once, not once per bank row

    for _, bank_row in bank_df.iterrows():
        # A real statement carries debits too. A chargeback or reversal can
        # quote the very UTR of the settlement it is clawing back -- money
        # leaving must never be read as money arriving.
        kind = str(_field(bank_row, "type", "credit")).strip().lower()
        if kind and not kind.startswith("credit"):
            non_credits.append(bank_row)
            continue

        utr = extract_utr_from_narration(bank_row["narration"], index=index)
        if utr is None:
            unresolved.append(bank_row)
        else:
            by_utr.setdefault(utr, []).append(bank_row)

    # Proposals are folded in afterwards rather than inline, so the scanning
    # pass and the proposal pass are separable and the scan can be reused.
    return apply_link_proposals((by_utr, unresolved, non_credits),
                                extra_links, known_utrs)


def tolerance_for(legs):
    """Rounding tolerance accumulates across the legs of a batched payout."""
    return AMOUNT_TOLERANCE * max(1, len(legs))


def match_settlement_to_bank(settlement_df, bank_df, extra_links=None,
                             partition=None):
    """
    Stage B. Settlement-driven, so every settlement gets a verdict.
    Returns (results, unresolved_bank_rows) -- unresolved go to the later tiers.

    `partition` is the (by_utr, unresolved, non_credits) split Tier 1 already
    computed. Passing it in avoids scanning every narration a second time; the
    proposals in `extra_links` are folded onto it rather than triggering a
    rescan. Omit it and Stage B scans for itself, which is what the tests do.
    """
    results = []

    known_utrs = {row["utr"] for _, row in settlement_df.iterrows()}
    utr_index = build_utr_index(known_utrs)
    if partition is None:
        partition = link_bank_rows(bank_df, known_utrs, index=utr_index)
    bank_by_utr, unresolved, non_credits = apply_link_proposals(
        partition, extra_links, known_utrs)

    # A debit quoting a settlement's own reference is that settlement being
    # clawed back: a chargeback, a reversal, a recall. Filtering non-credits out
    # of matching is right, but discarding them was not. Without this, a
    # settlement could match cleanly on Monday, be reversed on Tuesday, and
    # still be reported as reconciled money the merchant does not have.
    reversals_by_utr = {}
    for row in non_credits:
        utr = extract_utr_from_narration(row["narration"], index=utr_index)
        if utr is not None:
            reversals_by_utr.setdefault(utr, []).append(row)

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
        leg_amounts = [_num(row, "settled_amount") for row in legs]
        if any(a is None for a in leg_amounts):
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="source_value_missing",
                    basis=f"settlement {utr} has a missing or non-numeric settled_amount",
                    detail={"utr": utr},
                ))
            continue

        expected = round(sum(leg_amounts), 2)
        credits = bank_by_utr.get(utr, [])
        reversals = reversals_by_utr.get(utr, [])

        if reversals:
            reversed_total = round(
                sum(_num(r, "amount") or 0.0 for r in reversals), 2)
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="settlement_reversed",
                    basis=(f"{len(reversals)} debit(s) totalling {reversed_total} "
                           f"quote settlement UTR {utr} — money was credited and "
                           f"then taken back"),
                    detail={"utr": utr, "reversed_amount": reversed_total,
                            "reversal_txn_ids": [str(r["txn_id"]) for r in reversals],
                            "settled_total": expected},
                ))
            continue

        if not credits:
            # Two very different problems wear the same face here, and a
            # controller triages them oppositely: money that never arrived is
            # escalated to Razorpay, money that arrived under an unreadable
            # narration is a parsing problem. Distinguish them -- but note that
            # NEITHER becomes a match. An amount lining up is a triage hint, not
            # evidence, and both stay exceptions.
            candidates = [r for r in unresolved
                          if (_num(r, "amount") is not None
                              and abs(_num(r, "amount") - expected) <= tolerance_for(legs))]
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

        credit_amounts = [_num(c, "amount") for c in credits]
        if any(a is None for a in credit_amounts):
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="source_value_missing",
                    basis=(f"a bank credit for UTR {utr} has a missing or "
                           f"non-numeric amount — cannot verify"),
                    detail={"utr": utr},
                ))
            continue

        actual = round(sum(credit_amounts), 2)
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

        settlement_dates = [_parse_date(row["settlement_date"]) for row in legs]
        credit_dates = [_parse_date(c["date"]) for c in credits]
        if any(d is None for d in settlement_dates + credit_dates):
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="date_unparseable",
                    basis=(f"UTR {utr}: a settlement or credit date could not be "
                           f"read, so the settlement window cannot be verified"),
                    detail={"utr": utr},
                ))
            continue

        latest_settlement = max(settlement_dates)
        latest_credit = max(credit_dates)
        date_gap = (latest_credit - latest_settlement).days

        if date_gap < 0:
            # The bank cannot pay out a settlement that had not been made yet.
            # This is a data-integrity problem, not an unusually fast payment,
            # and it was passing as a match because the window only ever
            # checked the late side.
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="bank_credit_predates_settlement",
                    basis=(f"bank credit is {abs(date_gap)} days BEFORE the "
                           f"settlement date — impossible ordering"),
                    detail={"utr": utr, "date_gap_days": date_gap},
                ))
            continue

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
