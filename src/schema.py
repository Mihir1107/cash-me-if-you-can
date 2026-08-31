"""
Column resolution: their spreadsheet's names, this pipeline's names.

Nobody's real export has the column names this pipeline wants. A Tally ledger
says "Voucher No" and "Party Name", a Razorpay settlement report says "Settled
Amount" and "UTR Number", an HDFC statement says "Particulars" and "Withdrawal
Amt.". Requiring `order_id` exactly meant the tool worked on the fixture and on
nothing else, which is a strange definition of working.

So this maps their names onto ours. Two rules govern it, and both come from the
same place as the rest of the project:

**Naming is normalised, never guessed.** A column matches a field when its name,
stripped to lowercase alphanumerics, is one this file knows for that field.
"Order ID", "order_id" and "OrderID" are the same string once normalised. There
is no fuzzy matching in the accepted path, because a mis-mapped column is the
worst failure this system has: point `settled_amount` at the gross column and
every downstream check still passes, the footing still foots, and every figure
in the report is wrong with nothing to show for it. That is precisely the
confidently-wrong outcome the whole architecture exists to prevent, and it is
not a place to save the user two clicks.

**What cannot be resolved is asked, not assumed.** Unresolved fields come back
with a ranked suggestion attached, which a human confirms. The suggestion is
allowed to be fuzzy because a person is deciding; the automatic path is not.

Everything here is deterministic. No model is consulted. Reading a column header
is not a language problem -- it is a lookup with a curated table, and a lookup
can be tested.
"""

import re
from difflib import SequenceMatcher

# Fields the pipeline reads, per source. `required` cannot be worked around;
# `optional` degrades in a defined way, described in NOTE_IF_MISSING.
FIELDS = {
    "ledger": {
        "required": ("ledger_id", "order_id", "amount", "date"),
        "optional": ("customer", "status"),
    },
    "settlements": {
        "required": ("settlement_id", "order_id", "gross_amount", "fee", "tax",
                     "settled_amount", "settlement_date", "utr"),
        "optional": ("payment_id", "refund_amount"),
    },
    "bank": {
        "required": ("txn_id", "date", "amount", "narration"),
        "optional": ("type",),
    },
}

# What silence costs, so an optional column is a decision rather than an
# oversight. These are surfaced to the user, not swallowed.
NOTE_IF_MISSING = {
    "status": "Refunds the ledger failed to book cannot be told apart from "
              "refunds it booked correctly, so `refund_not_reflected` will not fire.",
    "refund_amount": "Refunds are treated as zero, so a settlement reduced by a "
                     "refund will look like a fee arithmetic error instead.",
    "type": "Every bank row is treated as a credit, so a chargeback or reversal "
            "will not be recognised as `settlement_reversed`.",
    "customer": "Cosmetic only. Nothing in the reconciliation reads it.",
    "payment_id": "Cosmetic only. Nothing in the reconciliation reads it.",
}

# Names seen in the wild, normalised. Order does not matter; collisions do, and
# `resolve` refuses rather than picking when two fields claim one column.
ALIASES = {
    "ledger_id": ("ledgerid", "id", "entryid", "voucherno", "vouchernumber",
                  "voucher", "journalid", "recordid", "sno", "srno", "serialno"),
    "order_id": ("orderid", "order", "orderno", "ordernumber", "orderref",
                 "orderreference", "ordercode", "receiptid"),
    "amount": ("amount", "amountinr", "amountrs", "value", "invoiceamount",
               "saleamount", "totalamount", "total", "orderamount", "netamount",
               "transactionamount", "amt"),
    "date": ("date", "txndate", "transactiondate", "postingdate", "valuedate",
             "entrydate", "orderdate", "invoicedate", "bookingdate"),
    "customer": ("customer", "customername", "party", "partyname", "buyer",
                 "client", "clientname", "payer"),
    "status": ("status", "paymentstatus", "orderstatus", "state", "txnstatus"),

    "settlement_id": ("settlementid", "settlement", "payoutid", "payout",
                      "settlementref", "settlementreference", "batchid"),
    "payment_id": ("paymentid", "payment", "paymentref", "pgpaymentid"),
    "gross_amount": ("grossamount", "gross", "paymentamount", "capturedamount",
                     "grossvalue", "transactionvalue"),
    "fee": ("fee", "fees", "commission", "mdr", "charges", "razorpayfee",
            "gatewayfee", "servicecharge", "feeamount"),
    "tax": ("tax", "gst", "gstonfee", "gstamount", "servicetax", "taxamount",
            "taxonfee"),
    "refund_amount": ("refundamount", "refund", "refunds", "refundvalue",
                      "amountrefunded"),
    "settled_amount": ("settledamount", "settlementamount", "netsettlement",
                       "netamountsettled", "amountsettled", "payoutamount",
                       "creditamount", "netpayout"),
    "settlement_date": ("settlementdate", "settledon", "payoutdate",
                        "settlementon", "dateofsettlement", "creditdate"),
    "utr": ("utr", "utrnumber", "utrno", "rrn", "rrnnumber", "bankreference",
            "bankref", "payoutreference", "payoutref", "referencenumber",
            "refno", "reference", "transactionreference"),

    "txn_id": ("txnid", "transactionid", "id", "chequeno", "chequenumber",
               "refno", "referenceno", "sno", "srno", "serialno", "slno",
               "entryid"),
    "narration": ("narration", "description", "particulars", "remarks",
                  "transactionremarks", "details", "transactiondetails",
                  "text", "memo"),
    "type": ("type", "drcr", "crdr", "debitcredit", "creditdebit",
             "transactiontype", "txntype", "indicator"),
}

# A bank statement that splits money across two columns is a different shape,
# not a different spelling, and renaming cannot fix it. Detected so the user is
# told what is wrong rather than handed a silently wrong reconciliation.
SPLIT_AMOUNT_HINTS = {
    "debit": ("debit", "debitamount", "withdrawal", "withdrawalamt",
              "withdrawalamount", "dr", "paidout"),
    "credit": ("credit", "creditamount", "deposit", "depositamt",
               "depositamount", "cr", "paidin"),
}


def normalise(name):
    """Lowercase alphanumerics only: 'Amount (INR)' and 'amount_inr' agree."""
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def _lookup(source):
    """{normalised alias: canonical field}, for the fields this source uses."""
    spec = FIELDS[source]
    wanted = set(spec["required"]) | set(spec["optional"])
    table = {}
    for field in wanted:
        for alias in ALIASES.get(field, ()):
            table.setdefault(alias, set()).add(field)
    return table


def _suggest(column_names, field, taken):
    """
    Best guess for a field the table could not place, for a human to confirm.

    Fuzzy is allowed here and nowhere else, because this is a proposal shown to
    a person, not an attribution applied behind their back -- the same split the
    matcher makes between proposing and confirming.
    """
    # Compare against the field's known spellings, not only its internal name.
    # "Ordr Refrence" is a typo of "order reference", which barely resembles the
    # string "order_id" -- scoring against the alias list is what makes a
    # misspelt real-world header suggestible at all.
    targets = {normalise(field)} | {normalise(a) for a in ALIASES.get(field, ())}
    best, score = None, 0.0
    for name in column_names:
        if name in taken:
            continue
        here = max(SequenceMatcher(None, normalise(name), t).ratio() for t in targets)
        if here > score:
            best, score = name, here
    return (best, round(score, 2)) if score >= 0.55 else (None, 0.0)


def resolve(columns, source):
    """
    Map a file's columns onto the fields this pipeline reads.

    Returns a dict:
      mapping     {canonical field: actual column} -- confidently resolved
      unresolved  [{field, required, suggestion, confidence, note}]
      ambiguous   [{column, fields}] -- one column, two fields, refused
      conflicts   [{field, columns}] -- one field, two columns, refused
      split_amount  bank statements with separate debit/credit columns
      ready       True when every required field resolved
    """
    if source not in FIELDS:
        raise KeyError(f"unknown source {source!r}")

    spec = FIELDS[source]
    table = _lookup(source)
    columns = list(columns)

    claims = {}          # field -> [columns claiming it]
    ambiguous = []
    for column in columns:
        hits = table.get(normalise(column))
        if not hits:
            continue
        if len(hits) > 1:
            # e.g. "Reference No" is a plausible name for both txn_id and utr
            ambiguous.append({"column": column, "fields": sorted(hits)})
            continue
        claims.setdefault(next(iter(hits)), []).append(column)

    mapping, conflicts = {}, []
    for field, found in claims.items():
        if len(found) == 1:
            mapping[field] = found[0]
        else:
            # two columns both normalise to the same field; picking one would be
            # a coin flip on a number, so it goes to the human
            conflicts.append({"field": field, "columns": found})

    taken = set(mapping.values())
    unresolved = []
    for field in spec["required"] + spec["optional"]:
        if field in mapping:
            continue
        suggestion, confidence = _suggest(columns, field, taken)
        unresolved.append({
            "field": field,
            "required": field in spec["required"],
            "suggestion": suggestion,
            "confidence": confidence,
            "note": NOTE_IF_MISSING.get(field),
        })

    split = None
    if source == "bank" and "amount" not in mapping:
        found = {}
        for kind, aliases in SPLIT_AMOUNT_HINTS.items():
            for column in columns:
                if normalise(column) in aliases:
                    found[kind] = column
                    break
        if len(found) == 2:
            split = found

    return {
        "mapping": mapping,
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "conflicts": conflicts,
        "split_amount": split,
        "ready": all(f in mapping for f in spec["required"]),
        "columns": columns,
    }


def merge_split_amount(df, debit_column, credit_column):
    """
    Fold a statement's separate debit and credit columns into one signed amount
    and a type, which is the shape the pipeline reads.

    Indian bank exports overwhelmingly come this way -- HDFC's "Withdrawal Amt."
    and "Deposit Amt.", ICICI's "Debit"/"Credit". Renaming cannot fix it, so it
    is converted: a row with a deposit is a credit for that amount, a row with a
    withdrawal is a debit. That distinction is load-bearing -- it is how a
    chargeback is told apart from a payout.

    A row carrying both is left as a credit for the net, and a row carrying
    neither becomes NaN, which the pipeline already reports as
    `source_value_missing` rather than treating as zero.
    """
    import pandas as pd

    debit = pd.to_numeric(df[debit_column], errors="coerce").fillna(0.0)
    credit = pd.to_numeric(df[credit_column], errors="coerce").fillna(0.0)
    both_blank = (debit == 0) & (credit == 0)

    out = df.copy()
    out["amount"] = (credit - debit).abs().where(~both_blank)
    out["type"] = ["credit" if c >= d else "debit" for c, d in zip(credit, debit)]
    return out


# ---------------------------------------------------------------------------
# Verifying a proposed mapping against the data it claims to describe.
# ---------------------------------------------------------------------------
#
# A mapping is a claim about what each column MEANS, and a wrong one is the
# quietest failure in this system: swap gross_amount and settled_amount and the
# footing still foots, the identity still balances, and every figure in the
# report is wrong with nothing anywhere to show for it.
#
# The rest of this project answers that shape of problem the same way every
# time: let something propose, then have deterministic code check the proposal
# against the data before it counts. That is what these do. The checks are
# ordinary arithmetic on the file itself -- do the amount columns contain
# amounts, do the date columns contain dates, does the settlement actually foot
# -- and they are strong enough to catch every swap that matters.

NUMERIC_FIELDS = {"amount", "gross_amount", "fee", "tax", "settled_amount",
                  "refund_amount"}
DATE_FIELDS = {"date", "settlement_date"}
UNIQUE_FIELDS = {"ledger_id", "settlement_id", "txn_id"}

# Injected faults are real, so a check that demanded perfection would fail on
# honest data. These are the fractions below which a mapping is not plausible.
PARSE_RATE = 0.90       # of non-blank values
UNIQUE_RATE = 0.95
FOOTING_RATE = 0.60     # settlements genuinely mis-foot; most should not
FEE_RATIO_MAX = 0.25    # a fee column that is a quarter of gross is not a fee
BALANCE_RATIO_MAX = 10  # measured: ~2 for amount columns, ~70 for a balance
CATEGORICAL_RATIO_MAX = 0.3   # a status repeats itself; a name does not


def _numeric(series):
    import pandas as pd

    return pd.to_numeric(series, errors="coerce")


def _check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


def verify_mapping(df, mapping, source):
    """
    Does this mapping describe the data, or merely fit the headers?

    Returns {"ok", "checks": [...], "failures": [...]}. A failure means the
    mapping is refused -- not softened, not accepted with a warning -- because
    the whole value of the check is that it is not negotiable.
    """
    import pandas as pd

    checks = []
    frame = apply_mapping(df, mapping)
    rows = len(frame)
    if not rows:
        return {"ok": False, "checks": [], "failures": ["the file has no rows"]}

    for field in sorted(NUMERIC_FIELDS & set(frame.columns)):
        present = frame[field].astype(str).str.strip().replace("", pd.NA).dropna()
        if present.empty:
            continue
        parsed = _numeric(present).notna().mean()
        checks.append(_check(
            f"{field} contains numbers", parsed >= PARSE_RATE,
            f"{parsed:.0%} of values parse as a number "
            f"(column {mapping[field]!r})"))

    for field in sorted(DATE_FIELDS & set(frame.columns)):
        present = frame[field].astype(str).str.strip().replace("", pd.NA).dropna()
        if present.empty:
            continue
        parsed = pd.to_datetime(present, errors="coerce", format="mixed").notna().mean()
        checks.append(_check(
            f"{field} contains dates", parsed >= PARSE_RATE,
            f"{parsed:.0%} of values parse as a date (column {mapping[field]!r})"))

    for field in sorted(UNIQUE_FIELDS & set(frame.columns)):
        ratio = frame[field].nunique(dropna=True) / rows
        checks.append(_check(
            f"{field} identifies rows", ratio >= UNIQUE_RATE,
            f"{ratio:.0%} of values are distinct (column {mapping[field]!r})"))

    # The strongest check available, and the one that catches a gross/settled
    # swap: a settlement is supposed to foot against its own parts.
    if source == "settlements" and {"gross_amount", "fee", "tax",
                                    "settled_amount"} <= set(frame.columns):
        gross = _numeric(frame["gross_amount"])
        fee = _numeric(frame["fee"])
        tax = _numeric(frame["tax"])
        refund = (_numeric(frame["refund_amount"]) if "refund_amount" in frame
                  else 0.0)
        settled = _numeric(frame["settled_amount"])
        expected = gross - fee - tax - refund
        foots = ((settled - expected).abs() <= 1.0).mean()
        checks.append(_check(
            "settlements foot against gross, fee, tax and refund",
            foots >= FOOTING_RATE,
            f"{foots:.0%} of rows foot — a gross/settled swap shows up here"))

        median_gross = gross.median()
        median_fee = fee.median()

        # Footing is SYMMETRIC in fee and tax -- settled = gross - fee - tax
        # holds just as well with those two swapped -- so the identity cannot
        # see that mistake at all. The consequence is milder than a gross/settled
        # swap (the payout total stays right, the split is wrong) but it still
        # misreports what the gateway charged, and `fee_rate_error` clustering
        # in triage reads the wrong column.
        #
        # What separates them is domain, not arithmetic: Indian gateways charge
        # GST *on the commission*, so tax is a fraction of fee and is always the
        # smaller of the two. Stated as an assumption because it is one -- a
        # gateway that taxed differently would trip this honestly, and the
        # mapping would go to a human rather than through.
        if "tax" in frame.columns:
            median_tax = tax.median()
            if pd.notna(median_tax) and pd.notna(median_fee) and median_fee:
                checks.append(_check(
                    "tax is smaller than the fee it is charged on",
                    abs(median_tax) <= abs(median_fee),
                    f"median tax {median_tax:,.2f} vs median fee {median_fee:,.2f} "
                    f"— tax is charged on the fee, so it should be the smaller"))

        # A fee the size of the sale is not a fee. Catches fee<->gross directly,
        # which footing alone can miss when the arithmetic stays symmetric.
        if median_gross and median_gross > 0:
            ratio = abs(median_fee / median_gross)
            checks.append(_check(
                "the fee column is fee-sized", ratio <= FEE_RATIO_MAX,
                f"median fee is {ratio:.0%} of median gross"))

    # A status column is a small vocabulary -- paid, refunded, captured -- and a
    # name column is not. Pointing `status` at a customer name passes every other
    # check here (both are populated strings) and then quietly breaks refund
    # detection, because no customer is ever spelled "refunded".
    if "status" in frame.columns:
        ratio = frame["status"].nunique(dropna=True) / rows
        checks.append(_check(
            "status is a small set of values, not free text",
            ratio <= CATEGORICAL_RATIO_MAX,
            f"{ratio:.0%} of values are distinct — a status column repeats "
            f"itself, a name column does not"))

    # A bank statement's most likely mis-map, and one nothing above can see: a
    # running balance column is numeric, well populated and perfectly plausible.
    # It is separable from a transaction amount by scale rather than by type --
    # a balance is large and moves in small steps, an amount IS the step. The
    # ratio is ~2 for real amount columns and ~70 for a balance, so the line sits
    # far from both.
    if source == "bank" and "amount" in frame.columns:
        values = _numeric(frame["amount"]).dropna()
        steps = values.diff().abs().dropna()
        if len(steps) > 3 and steps.median():
            ratio = abs(values.median()) / steps.median()
            checks.append(_check(
                "the amount column holds amounts, not a running balance",
                ratio <= BALANCE_RATIO_MAX,
                f"values are {ratio:.0f}x their own step size "
                f"(a transaction amount is ~2x, a running balance far more)"))

    failures = [c["check"] + ": " + c["detail"] for c in checks if not c["ok"]]
    return {"ok": not failures, "checks": checks, "failures": failures}


# ---------------------------------------------------------------------------
# Cross-source verification.
# ---------------------------------------------------------------------------
#
# Everything above checks one file against itself, and that is only as strong as
# the file's own internal arithmetic. Settlements have a real identity to test --
# gross minus fee minus tax has to equal settled -- so a swap there is caught
# cold. The ledger and the bank statement have no such identity, and it shows:
# `narration` pointed at a branch-name column and `order_id` swapped with
# `ledger_id` both pass every single-file check there is.
#
# But these three files are three views of the SAME money, and that is itself a
# testable claim. If the ledger and the settlement report share no order ids,
# one of those columns is not the order id. If no bank narration contains any
# reference the settlements carry, `narration` is probably not the narration.
# Nothing else in the pipeline gets to assume the three files relate -- this is
# where that assumption is checked.

ORDER_OVERLAP_MIN = 0.10    # catastrophic mis-map gives ~0; bad data still gives more
NARRATION_HIT_MIN = 0.05    # a statement can legitimately quote no references


def verify_sources(ledger, settlements, bank=None):
    """
    Do these three files describe the same money?

    Returns {"ok", "checks", "failures"} in the same shape as verify_mapping.
    Thresholds are set low on purpose: they exist to catch a column pointed at
    the wrong thing, not to grade the merchant's bookkeeping. A genuinely messy
    month still clears them; a mis-mapped join key does not.
    """
    checks = []

    if ledger is not None and settlements is not None \
            and "order_id" in ledger and "order_id" in settlements:
        left = set(ledger["order_id"].dropna().astype(str))
        right = set(settlements["order_id"].dropna().astype(str))
        if left and right:
            overlap = len(left & right) / min(len(left), len(right))
            checks.append(_check(
                "the ledger and the settlement report share order ids",
                overlap >= ORDER_OVERLAP_MIN,
                f"{overlap:.0%} of the smaller side's order ids appear on both "
                f"— near zero usually means one of them is not the order id"))

    if bank is not None and settlements is not None \
            and "narration" in bank and "utr" in settlements:
        refs = {normalise(u) for u in settlements["utr"].dropna().astype(str)}
        refs = {r for r in refs if len(r) >= 5}
        text = [normalise(t) for t in bank["narration"].dropna().astype(str)]
        text = [t for t in text if t]
        # A batch with no references to look for, or no narrations to look in,
        # has nothing to say either way. Silence is not a failing grade.
        if refs and text:
            hits = sum(any(r in t for r in refs) for t in text) / len(text)
            checks.append(_check(
                "bank narrations quote references the settlements carry",
                hits >= NARRATION_HIT_MIN,
                f"{hits:.0%} of narrations contain a known reference — zero can "
                f"mean the column is not the narration, or that this bank simply "
                f"does not quote them"))

    failures = [c["check"] + ": " + c["detail"] for c in checks if not c["ok"]]
    return {"ok": not failures, "checks": checks, "failures": failures}


def apply_mapping(df, mapping):
    """
    Rename a frame's columns to the canonical names, keeping nothing else.

    Dropping the unmapped columns is deliberate: a stray column called `amount`
    that the user mapped somewhere else must not be sitting there for the
    pipeline to pick up by name.
    """
    # Select first, then rename. Renaming first leaves any unmapped column that
    # already carries a canonical name sitting alongside the renamed one --
    # pandas allows duplicate column labels, so `df["amount"]` then returns a
    # frame rather than a series and the pipeline reads the wrong number with no
    # error anywhere. Selecting first makes the collision impossible.
    wanted = [actual for actual in mapping.values() if actual in df.columns]
    picked = df.loc[:, wanted].copy()
    picked.columns = [field for field, actual in mapping.items()
                      if actual in df.columns]
    return picked
