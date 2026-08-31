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
from datetime import datetime, timedelta

AMOUNT_TOLERANCE = 1.0  # paise-level rounding tolerance, in rupees

# Razorpay's documented cycle is T+2 *working* days, and settlements are only
# processed on bank working days: not Sundays, not the second and fourth
# Saturday, not public holidays. A flat calendar-day window is therefore not
# equivalent -- a Friday settlement before a long weekend can credit five or six
# calendar days later and still be perfectly on time. Counting working days
# removes that false positive.
#
# Public holidays are modelled but not populated: see BANK_HOLIDAYS below. With
# no calendar supplied this under-counts the window around a holiday and can
# still flag a late-but-legitimate credit.
BANK_WINDOW_WORKING_DAYS = 3

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


# Bank holidays, as {date: name}. Empty by default, and deliberately so.
#
# The holiday calendar is a *data* problem, not a logic one: RBI publishes it
# per year and per state, it moves with lunar dates, and a wrong entry silently
# changes whether a real payout is reported late. Shipping a half-remembered
# list would be worse than shipping none, because a wrong date is invisible
# where a missing one is at least a stated limitation.
#
# So the mechanism is here and the data is yours to supply, from the RBI holiday
# notification for the period you are reconciling:
#
#     from datetime import date
#     from src import matcher
#     matcher.BANK_HOLIDAYS = {date(2026, 8, 15): "Independence Day"}
#
# `load_bank_holidays()` reads the same thing from a CSV so it does not have to
# live in code at all.
BANK_HOLIDAYS = {}


def load_bank_holidays(path):
    """
    Read a bank holiday calendar from a two-column CSV: `date,name`.

    Returns {date: name}. Unreadable rows are skipped rather than raising -- a
    malformed holiday file should narrow the calendar, not kill the batch -- and
    the count of what was loaded is returned to the caller's eye via the dict
    length, so a silently empty file is visible.
    """
    import csv

    holidays = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            when = _parse_date(row.get("date"))
            if when is not None:
                holidays[_as_date(when)] = (
                    (row.get("name") or "").strip() or "bank holiday")
    return holidays


def is_bank_working_day(day, holidays=None):
    """
    Indian bank working day: not Sunday, not the second or fourth Saturday, and
    not a published bank holiday.

    Holidays default to the module-level BANK_HOLIDAYS, which is empty unless a
    calendar has been supplied. With none supplied this under-counts the window
    around a holiday and can still flag a legitimate credit as delayed -- the
    behaviour is unchanged from before the calendar existed, and stated as a
    limitation rather than papered over with invented dates.
    """
    if day.weekday() == 6:              # Sunday
        return False
    if day.weekday() == 5:              # Saturday
        nth = (day.day - 1) // 7 + 1    # which Saturday of the month
        if nth in (2, 4):
            return False
    calendar = BANK_HOLIDAYS if holidays is None else holidays
    if not calendar:
        return True
    # The pipeline parses dates into datetimes, while a calendar is naturally
    # written as plain dates. Comparing the two directly never matches, and it
    # fails *silently* -- every holiday simply stops counting. So normalise both
    # sides to a plain date before the lookup.
    return _as_date(day) not in {_as_date(d) for d in calendar}


def working_days_between(start, end, holidays=None):
    """
    Bank working days strictly after `start`, up to and including `end`.
    Negative when end precedes start, so an impossible ordering stays visible.
    """
    if end < start:
        return -working_days_between(end, start, holidays)
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if is_bank_working_day(cursor, holidays):
            days += 1
    return days


def _as_date(value):
    """A datetime and the date it falls on are the same day to a bank calendar."""
    return value.date() if hasattr(value, "date") else value


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


def _could_have_produced(settlement_date, credit_date):
    """
    Could a credit dated `credit_date` have come from a settlement dated
    `settlement_date`? This is the *same* rule Stage B enforces after a link is
    made -- not before the settlement, not later than the payout window -- asked
    earlier, to work out which settlements are genuine candidates for a credit.

    A date we cannot read returns True. Being unable to rule a candidate out is
    not evidence that it is the right one, and treating an unparseable date as a
    disqualification would let bad data narrow an attribution.
    """
    if settlement_date is None or credit_date is None:
        return True
    gap = working_days_between(settlement_date, credit_date)
    return 0 <= gap <= BANK_WINDOW_WORKING_DAYS


def _filter_ambiguous_proposals(settlement_df, partition, extra_links):
    """
    Drop proposals whose credit could belong to more than one settlement.

    Stage B verifies amount and date. Those checks catch a wrong proposal only
    when the wrong settlement expects a *different* amount. Two settlements
    expecting the same amount on the same day defeat them entirely: the credit
    passes every check against whichever one it was pointed at, and the other is
    reported as never credited. The result is a confident, verified, wrong match.

    Amount equality is not attribution. When a proposal names one of several
    settlements that could equally claim a credit, the honest outcome is that
    nothing is attributed, so the proposal is refused here and the credit stays
    unresolved. A reference read directly out of the narration is unaffected --
    that is real evidence, not a guess, and it survives an amount collision.

    **A rival has to be a real rival.** Amount alone over-counts them badly: at
    5,000 orders it refused 20 healthy attributions, 16 of which had only one
    candidate that could actually have produced the credit. So a settlement is
    only counted as competing if the credit is date-feasible against it as well
    -- same window Stage B applies, asked earlier. This narrows *who is
    competing*; it never picks between competitors. Two settlements that are
    both feasible are still refused, however confident the proposal, because at
    that point there is genuinely nothing left to tell them apart.

    Deliberately NOT used as a tie-break: payer strings, narration similarity to
    other credits, or position in the statement. Those would let a proposal win
    on evidence weaker than the verification it is bypassing, which is the exact
    shape of the bug this filter exists to close.

    Returns (accepted, refused) as {txn_id: utr} dicts.
    """
    if not extra_links:
        return {}, {}

    already_linked, unresolved, _ = partition

    # what each settlement group is still waiting for, and when it was paid out
    expected_by_utr, dates_by_utr = {}, {}
    for _, row in settlement_df.iterrows():
        amount = _num(row, "settled_amount")
        if amount is None:
            continue
        expected_by_utr.setdefault(row["utr"], []).append(amount)
        dates_by_utr.setdefault(row["utr"], []).append(
            _parse_date(_field(row, "settlement_date", None)))

    # Stage B compares against the latest leg of a settlement, so the candidacy
    # window has to start from the same date, or the two would disagree.
    def latest_date(utr):
        seen = [d for d in dates_by_utr.get(utr, []) if d is not None]
        return max(seen) if seen else None

    uncredited = {utr: round(sum(amounts), 2)
                  for utr, amounts in expected_by_utr.items()
                  if utr not in already_linked}

    by_txn = {row["txn_id"]: row for row in unresolved}
    accepted, refused = {}, {}

    for txn_id, utr in extra_links.items():
        row = by_txn.get(txn_id)
        amount = _num(row, "amount") if row is not None else None
        if amount is None:
            accepted[txn_id] = utr
            continue

        credit_date = _parse_date(_field(row, "date", None))
        rivals = [candidate for candidate, expected in uncredited.items()
                  if abs(expected - amount) <= AMOUNT_TOLERANCE
                  and _could_have_produced(latest_date(candidate), credit_date)]

        if len(rivals) > 1:
            refused[txn_id] = utr
        else:
            accepted[txn_id] = utr

    return accepted, refused


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

    # A proposal from the fuzzy or LLM tier is weaker evidence than a reference
    # read straight out of the narration, so it is only allowed to resolve an
    # attribution that is already unambiguous. Filtering happens before the
    # proposals are folded in, so an ambiguous one never reaches verification.
    safe_links, refused_links = _filter_ambiguous_proposals(
        settlement_df, partition, extra_links)
    bank_by_utr, unresolved, non_credits = apply_link_proposals(
        partition, safe_links, known_utrs)

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

        if not credits and any(u == utr for u in refused_links.values()):
            # A tier proposed this settlement for a credit, and the credit could
            # equally have belonged to another settlement expecting the same
            # amount. Refusing is the whole point, but it is also a finding: a
            # controller needs to know money is sitting there unattributable
            # rather than believing nothing arrived.
            rival_count = sum(1 for u in refused_links.values() if u != utr) + 1
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="attribution_ambiguous",
                    basis=(f"a credit was proposed for settlement {utr}, but the "
                           f"amount alone cannot distinguish it from other "
                           f"settlements expecting the same total — attribution "
                           f"refused rather than guessed"),
                    detail={"utr": utr, "expected_amount": expected,
                            "ambiguous_candidates": rival_count},
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
        # Razorpay gives each settlement one UTR and settles many payments under
        # it, so orders sharing a reference are payments inside one settlement,
        # not several settlements sharing a reference. The distinction matters:
        # the first is documented behaviour, the second would be an anomaly.
        batch_note = (f"{len(legs)} payments in one settlement"
                      if len(legs) > 1 else "single payment settlement")
        if has_duplicates:
            batch_note += (f" ({len(batch) - len(legs)} duplicate row(s) ignored "
                           f"here, reported by Stage A)")

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
        calendar_gap = (latest_credit - latest_settlement).days
        date_gap = working_days_between(latest_settlement, latest_credit)

        if date_gap < 0:
            # The bank cannot pay out a settlement that had not been made yet.
            # This is a data-integrity problem, not an unusually fast payment,
            # and it was passing as a match because the window only ever
            # checked the late side.
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="bank_credit_predates_settlement",
                    basis=(f"bank credit is {abs(calendar_gap)} days BEFORE the "
                           f"settlement date — impossible ordering"),
                    detail={"utr": utr, "date_gap_days": date_gap,
                            "calendar_gap_days": calendar_gap},
                ))
            continue

        if date_gap > BANK_WINDOW_WORKING_DAYS:
            for oid in order_ids:
                results.append(MatchResult(
                    order_id=oid, stage="settlement_bank", status="exception",
                    reason_code="bank_credit_delayed",
                    basis=(f"bank credit {date_gap} working days after settlement "
                           f"({calendar_gap} calendar), window="
                           f"{BANK_WINDOW_WORKING_DAYS} working days"),
                    detail={"utr": utr, "date_gap_days": date_gap,
                            "calendar_gap_days": calendar_gap},
                ))
            continue

        for oid in order_ids:
            results.append(MatchResult(
                order_id=oid, stage="settlement_bank", status="matched",
                basis=(f"UTR {utr} match, amount agrees ({batch_note}), "
                       f"{date_gap} working-day gap within window"),
                detail={"utr": utr, "date_gap_days": date_gap,
                        "calendar_gap_days": calendar_gap},
            ))

    return results, unresolved
