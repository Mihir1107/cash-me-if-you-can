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
