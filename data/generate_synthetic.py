"""
Generates 3 synthetic sources that a real merchant would have:
  1. razorpay_settlements.csv  - what Razorpay says it settled
  2. bank_statement.csv        - what actually hit the bank (free-text narration, like real banks)
  3. internal_ledger.csv       - what the merchant's own system thinks happened

Deliberately injects the mismatches reconciliation is supposed to catch. Each
injected case maps to exactly one reason code the pipeline should emit:

  case 0   refund processed by Razorpay, ledger never updated  -> refund_not_reflected
  case 1   duplicate settlement row (Razorpay-side glitch)     -> duplicate_settlement
  case 2   order in ledger, never settled (stuck/orphan)       -> no_settlement_found
  case 3   fee/tax arithmetic wrong on the settlement          -> fee_footing_mismatch
  case 4   bank credit lands beyond the normal window          -> bank_credit_delayed
  case 5   mangled narration, reference digits still present   -> recovered by the fuzzy tier
  case 6   noisy narration, clean reference present            -> recovered by the regex tier
  cases 7-11                                                   -> clean, should match

Plus targeted cases placed on otherwise-clean orders:
  FREETEXT_ORDERS         narration with no recoverable reference -> LLM tier, else narration_unresolved
  NOT_CREDITED_ORDERS     Razorpay settled, money never arrived   -> settlement_not_credited
  REFUND_REFLECTED_ORDERS refund processed AND booked correctly   -> control, must MATCH
  BATCH_GROUPS            N settlements paid out as 1 bank credit -> many-to-one, must match as a batch

The REFUND_REFLECTED control matters: it proves refund_not_reflected is
detecting a genuine ledger gap, not just flagging every order with a refund.
"""

import csv
import random
from datetime import datetime, timedelta

random.seed(42)

N_ORDERS = 55  # >50 per the brief
START = datetime(2026, 8, 1)

FEE_PCT = 0.02
TAX_PCT = 0.18  # GST on fee

# Targeted cases, placed on order numbers whose modulo class is otherwise clean.
FREETEXT_ORDERS = {9, 33}
NOT_CREDITED_ORDERS = {21, 45}
REFUND_REFLECTED_ORDERS = {11, 35}
BATCH_GROUPS = [[7, 19, 31], [43, 55]]

# Razorpay batches several settlements into one payout; the whole batch shares a UTR.
BATCH_UTR = {}
for _group in BATCH_GROUPS:
    _shared = f"UTR{700000 + _group[0]}"
    for _member in _group:
        BATCH_UTR[_member] = _shared


def rid(prefix, n):
    return f"{prefix}_{n:06d}"


def make_dataset():
    ledger_rows = []
    settlement_rows = []
    bank_rows = []
    # what SHOULD happen to each order, recorded at injection time. This is the
    # answer key the evaluator scores against -- it is never read by the pipeline.
    truth_rows = []

    # bank rows are emitted after the loop so batched settlements can be summed
    pending_bank = []

    settlement_id_counter = 1

    for i in range(1, N_ORDERS + 1):
        order_id = rid("order", i)
        payment_id = rid("pay", i)
        customer = f"cust_{i:04d}"
        amount = round(random.uniform(500, 25000), 2)
        fee = round(amount * FEE_PCT, 2)
        tax = round(fee * TAX_PCT, 2)
        refund_amount = 0.0
        settled_amount = round(amount - fee - tax, 2)
        order_date = START + timedelta(days=random.randint(0, 20))
        utr = BATCH_UTR.get(i, f"UTR{100000 + i}")

        status = "paid"
        ledger_amount = amount
        bank_date_offset = 2  # normal T+2 credit
        expected = "matched"
        note = "clean order, should reconcile on both legs"

        case = i % 12

        if case == 0:
            # Razorpay processed a partial refund; the merchant's ledger still
            # books the full sale. Settlement foots correctly once the refund
            # is accounted for -- the gap is on the ledger side.
            refund_amount = round(amount * 0.3, 2)
            settled_amount = round(amount - fee - tax - refund_amount, 2)
            expected = "refund_not_reflected"
            note = "Razorpay refunded; merchant ledger still books the full sale"

        elif case == 1:
            # duplicate settlement row (Razorpay side glitch)
            settlement_rows.append({
                "settlement_id": rid("stl", settlement_id_counter),
                "payment_id": payment_id,
                "order_id": order_id,
                "gross_amount": amount,
                "fee": fee,
                "tax": tax,
                "refund_amount": refund_amount,
                "settled_amount": settled_amount,
                "settlement_date": (order_date + timedelta(days=2)).strftime("%Y-%m-%d"),
                "utr": utr,
            })
            settlement_id_counter += 1
            expected = "duplicate_settlement"
            note = "two settlement rows exist for one order_id"

        elif case == 2:
            # orphan: order exists in ledger, never settled (still processing / stuck)
            ledger_rows.append({
                "ledger_id": rid("led", i),
                "order_id": order_id,
                "customer": customer,
                "amount": ledger_amount,
                "date": order_date.strftime("%Y-%m-%d"),
                "status": status,
            })
            truth_rows.append({
                "order_id": order_id, "expected_reason_code": "no_settlement_found",
                "note": "order never settled (stuck/orphan)",
            })
            continue  # no settlement, no bank row

        elif case == 3:
            # fee miscalculated on Razorpay's side (settled_amount doesn't foot)
            settled_amount = round(settled_amount - random.uniform(5, 40), 2)
            expected = "fee_footing_mismatch"
            note = "settled_amount does not foot against gross - fee - tax"

        elif case == 4:
            # credit lands 8 days after settlement -- well beyond the 5-day window
            bank_date_offset = 10
            expected = "bank_credit_delayed"
            note = "bank credit lands 8 days after settlement, past the window"

        if i in REFUND_REFLECTED_ORDERS:
            # control: same refund shape as case 0, but the ledger DID book it
            refund_amount = round(amount * 0.25, 2)
            settled_amount = round(amount - fee - tax - refund_amount, 2)
            ledger_amount = round(amount - refund_amount, 2)
            status = "partially_refunded"
            note = "control: refund processed AND booked correctly, must match"

        settlement_date = order_date + timedelta(days=2)
        bank_date = order_date + timedelta(days=bank_date_offset)

        settlement_rows.append({
            "settlement_id": rid("stl", settlement_id_counter),
            "payment_id": payment_id,
            "order_id": order_id,
            "gross_amount": amount,
            "fee": fee,
            "tax": tax,
            "refund_amount": refund_amount,
            "settled_amount": settled_amount,
            "settlement_date": settlement_date.strftime("%Y-%m-%d"),
            "utr": utr,
        })
        settlement_id_counter += 1

        ledger_rows.append({
            "ledger_id": rid("led", i),
            "order_id": order_id,
            "customer": customer,
            "amount": ledger_amount,
            "date": order_date.strftime("%Y-%m-%d"),
            "status": status,
        })

        if i in FREETEXT_ORDERS:
            # The money is genuinely fine here -- correct settlement, correct
            # credit. Only the narration is unreadable. Ground truth stays
            # "matched", so a run that cannot resolve the narration scores this
            # as a FALSE POSITIVE. That is the honest cost of having no LLM tier.
            note = ("settled and credited correctly, but the narration quotes no "
                    "reference — resolvable only by the LLM tier")

        if i in NOT_CREDITED_ORDERS:
            expected = "settlement_not_credited"
            note = "Razorpay reports a settlement that never reached the bank"
            truth_rows.append({"order_id": order_id,
                               "expected_reason_code": expected, "note": note})
            continue  # Razorpay says settled; nothing ever hit the bank

        truth_rows.append({"order_id": order_id,
                           "expected_reason_code": expected, "note": note})

        pending_bank.append({
            "order_num": i,
            "customer": customer,
            "utr": utr,
            "amount": settled_amount,
            "date": bank_date,
            "case": case,
        })

    bank_rows = _emit_bank_rows(pending_bank)
    truth_rows.sort(key=lambda r: r["order_id"])
    return ledger_rows, settlement_rows, bank_rows, truth_rows


def _narration(entry):
    """Bank narrations vary in how parseable they are, exactly like real statements."""
    i, utr, customer, case = entry["order_num"], entry["utr"], entry["customer"], entry["case"]

    if i in FREETEXT_ORDERS:
        # no recoverable reference id at all -- only the LLM tier can propose anything
        return "CR/ONLINE TRF/paymnt gateway aug batch/no ref quoted"
    if case == 5:
        return f"NEFT-RZPY-{utr[-6:]}/settlemnt"       # mangled, digits survive
    if case == 6:
        return f"IMPS/{customer}/razorpay payout ref {utr}"  # noisy, ref intact
    return f"RAZORPAY SETTLEMENT UTR:{utr} ORD:{rid('order', i)}"


def _emit_bank_rows(pending_bank):
    """One bank row per payout. Batched settlements collapse into a single credit."""
    rows = []
    counter = 1
    by_utr = {}
    for entry in pending_bank:
        by_utr.setdefault(entry["utr"], []).append(entry)

    for utr, entries in by_utr.items():
        if len(entries) > 1:
            # many-to-one: one credit for the whole batch, summed, dated off the last leg
            total = round(sum(e["amount"] for e in entries), 2)
            date = max(e["date"] for e in entries)
            ids = ",".join(rid("order", e["order_num"]) for e in entries)
            rows.append({
                "txn_id": rid("bnk", counter),
                "date": date.strftime("%Y-%m-%d"),
                "amount": total,
                "narration": f"RAZORPAY SETTLEMENT UTR:{utr} BATCH OF {len(entries)} [{ids}]",
                "type": "credit",
            })
        else:
            entry = entries[0]
            rows.append({
                "txn_id": rid("bnk", counter),
                "date": entry["date"].strftime("%Y-%m-%d"),
                "amount": entry["amount"],
                "narration": _narration(entry),
                "type": "credit",
            })
        counter += 1

    rows.sort(key=lambda r: r["date"])
    return rows


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ledger, settlements, bank, truth = make_dataset()

    write_csv(
        "data/internal_ledger.csv", ledger,
        ["ledger_id", "order_id", "customer", "amount", "date", "status"],
    )
    write_csv(
        "data/razorpay_settlements.csv", settlements,
        ["settlement_id", "payment_id", "order_id", "gross_amount", "fee", "tax",
         "refund_amount", "settled_amount", "settlement_date", "utr"],
    )
    write_csv(
        "data/bank_statement.csv", bank,
        ["txn_id", "date", "amount", "narration", "type"],
    )

    write_csv(
        "data/ground_truth.csv", truth,
        ["order_id", "expected_reason_code", "note"],
    )

    print(f"ledger rows: {len(ledger)}")
    print(f"settlement rows: {len(settlements)}")
    print(f"bank rows: {len(bank)}")
    print(f"ground truth rows: {len(truth)}")
