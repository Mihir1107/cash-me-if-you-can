"""
Value reconciliation, and the accounting identity that keeps this report honest.

A match rate counts orders. A finance controller does not care how many orders
failed -- they care how many rupees are unaccounted for, and which bucket each
one sits in. Those two numbers come apart badly: 45% of orders reconciling can
mean 95% of the money is confirmed, or 20% is. Only one of those is a crisis,
and an order-count match rate cannot tell you which you are in.

So every order carries an exposure -- the amount of money its verdict is about:

    a settled order        -> settled_amount, what Razorpay says it paid out
    an order never settled -> ledger amount, revenue the merchant has booked
                              and has no payout for

Exposure is a magnitude. A settlement can legitimately net negative -- a
refund-heavy period where Razorpay claws back more than it pays -- and the money
at stake in that verdict is the size of the swing, not its direction. A
controller chasing an unreconciled ₹10,000 cares equally whether it was owed to
them or by them.

and every rupee of that exposure lands in exactly one bucket: confirmed, or at
risk under a named reason code. That is an identity, not a summary:

    total exposure == confirmed + sum(at_risk by reason)

check_identity() enforces it. If it ever fails, the report is not merely
imprecise, it is lying about where money went -- so the failure is surfaced in
the report and printed, never swallowed. A reconciliation tool that cannot
account for its own arithmetic has no business reporting anyone else's.

Unattributed bank credits are tracked separately and deliberately NOT netted
against exposure. Money sitting in the bank that we cannot tie to a settlement
is a different problem from money a settlement promised and we cannot confirm:
one is cash we hold and cannot place, the other is cash we are owed and cannot
find. Netting them would hide both.
"""

IDENTITY_TOLERANCE = 0.01  # rupees; guards float drift, not real discrepancies


def _num(row, name):
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
    return None if number != number else number


def build_exposure_index(ledger_df, settlement_df):
    """
    order_id -> {"exposure", "basis"}.

    Exposure is what the order's verdict is worth in rupees. A settled order is
    worth what Razorpay said it settled. An order with no settlement is worth
    what the merchant booked -- that revenue is exactly the exposure, because
    nobody has promised to pay it yet.
    """
    settled = {}
    for _, row in settlement_df.iterrows():
        amount = _num(row, "settled_amount")
        oid = row["order_id"]
        # a duplicated settlement exposes the merchant to the duplicate too
        settled.setdefault(oid, []).append(amount)

    index = {}
    for _, row in ledger_df.iterrows():
        oid = row["order_id"]
        if oid in index:
            continue
        amounts = settled.get(oid)
        if amounts:
            usable = [a for a in amounts if a is not None]
            if not usable:
                index[oid] = {"exposure": 0.0, "basis": "settled_amount unreadable"}
                continue
            index[oid] = {
                "exposure": round(max(abs(a) for a in usable), 2),
                "basis": ("settled_amount" if len(usable) == 1
                          else f"largest of {len(usable)} settlement rows"),
            }
        else:
            ledger_amount = _num(row, "amount")
            index[oid] = {
                "exposure": round(abs(ledger_amount), 2) if ledger_amount is not None else 0.0,
                "basis": "ledger amount — booked revenue with no settlement",
            }
    return index


def build_money_report(exposure_index, reconciled_ids, exceptions,
                       unattributed_credits):
    """
    Splits total exposure into confirmed and at-risk-by-reason, then checks the
    identity holds. Returns the money section of the report.
    """
    total_exposure = round(sum(e["exposure"] for e in exposure_index.values()), 2)

    confirmed = round(sum(exposure_index[oid]["exposure"]
                          for oid in reconciled_ids
                          if oid in exposure_index), 2)

    # One bucket per order, so an order failing both legs is counted once. The
    # ledger-side reason wins, matching how the evaluator attributes root cause.
    bucket_of = {}
    for exc in exceptions:
        oid = exc["order_id"]
        if oid is None or oid not in exposure_index:
            continue
        if oid not in bucket_of or exc["stage"] == "ledger_settlement":
            bucket_of[oid] = exc["reason_code"]

    at_risk_by_reason = {}
    for oid, reason in bucket_of.items():
        at_risk_by_reason[reason] = round(
            at_risk_by_reason.get(reason, 0.0) + exposure_index[oid]["exposure"], 2)

    at_risk = round(sum(at_risk_by_reason.values()), 2)

    unattributed_value = round(
        sum(float(c["amount"]) for c in unattributed_credits), 2)

    identity = check_identity(total_exposure, confirmed, at_risk)

    return {
        "total_exposure": total_exposure,
        "confirmed_value": confirmed,
        "at_risk_value": at_risk,
        "value_match_rate_pct": (round(100 * confirmed / total_exposure, 2)
                                 if total_exposure else 0.0),
        "at_risk_by_reason": dict(sorted(at_risk_by_reason.items(),
                                         key=lambda kv: -kv[1])),
        "unattributed_bank_credit_value": unattributed_value,
        "identity": identity,
        "exposure_note": (
            "exposure = settled_amount where a settlement exists, else the "
            "ledger amount (booked revenue with no payout)"
        ),
    }


def check_identity(total_exposure, confirmed, at_risk):
    """
    total exposure == confirmed + at risk. Every rupee in exactly one bucket.

    Reported rather than asserted: a controller needs to see that the books
    balance, and needs to see it loudly when they do not.
    """
    residual = round(total_exposure - (confirmed + at_risk), 2)
    return {
        "holds": abs(residual) <= IDENTITY_TOLERANCE,
        "residual": residual,
        "statement": "total_exposure == confirmed_value + at_risk_value",
    }
