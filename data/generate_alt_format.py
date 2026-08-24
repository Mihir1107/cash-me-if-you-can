"""
A second synthetic batch under an entirely different set of conventions, used
to answer one question: is the matcher tuned to the batch it was developed on?

Nothing about the pipeline is changed to run this. Same code, same thresholds,
same reason codes. Only the data's conventions differ, and they differ in every
way a second payment processor or bank plausibly would:

    reference format    RRN4471829 / IMPSP20293 / AXIS8827361 -- no "UTR"
                        prefix anywhere, mixed lengths, some purely numeric
    fee model           flat ₹3.00 + 1.75% (not a clean 2%), tax 18% on fee
    settlement cadence  T+1 (not T+2), delayed cases at T+9
    narration style     HDFC/ICICI/AXIS-style templates, none matching the
                        primary batch's phrasing
    id scheme           INV-prefixed order ids, not order_000001

If the matcher were quietly relying on "UTR" appearing in narrations, on a 2%
fee, or on a T+2 cadence, detection would collapse here. The reference matcher
works by normalised substring against references it already holds, so format is
not something it can depend on.

    python data/generate_alt_format.py     # writes data/alt/
"""

import csv
import os
import random
from datetime import datetime, timedelta

random.seed(7)  # different seed as well as different conventions

N_ORDERS = 60
START = datetime(2026, 9, 1)
OUT_DIR = "data/alt"

FLAT_FEE = 3.00
FEE_PCT = 0.0175
TAX_PCT = 0.18

REF_STYLES = [
    lambda i: f"RRN{4400000 + i * 7}",
    lambda i: f"IMPSP{20000 + i * 3}",
    lambda i: f"AXIS{8800000 + i * 11}",
    lambda i: f"{9100000 + i * 13}",          # purely numeric, no prefix at all
]

NARRATION_STYLES = [
    lambda ref, cust: f"NEFT CR-HDFC0000123-{cust}-{ref}-SETTLEMENT",
    lambda ref, cust: f"ICICI/MMT/IMPS/{ref}/PG PAYOUT/{cust}",
    lambda ref, cust: f"AXIS NET STLMT {ref} CR",
    lambda ref, cust: f"BY TRANSFER-NEFT*{ref}*PAYMENT GATEWAY",
]


def make_dataset():
    ledger, settlements, bank, truth = [], [], [], []

    for i in range(1, N_ORDERS + 1):
        oid = f"INV-{2026}-{i:05d}"
        customer = f"MERCHANT{i:03d}"
        gross = round(random.uniform(300, 40000), 2)
        fee = round(FLAT_FEE + gross * FEE_PCT, 2)
        tax = round(fee * TAX_PCT, 2)
        refund = 0.0
        settled = round(gross - fee - tax, 2)
        order_date = START + timedelta(days=random.randint(0, 25))
        ref = REF_STYLES[i % len(REF_STYLES)](i)

        status = "captured"
        ledger_amount = gross
        credit_lag = 1                      # this processor settles T+1
        expected = "matched"
        emit_settlement = True
        emit_bank = True

        case = i % 24  # ~33% fault density, closer to a plausible month
        if case == 0:
            refund = round(gross * 0.25, 2)
            settled = round(gross - fee - tax - refund, 2)
            expected = "refund_not_reflected"
        elif case == 1:
            settlements.append({
                "settlement_id": f"S{i:05d}A", "payment_id": f"P{i:05d}",
                "order_id": oid, "gross_amount": gross, "fee": fee, "tax": tax,
                "refund_amount": refund, "settled_amount": settled,
                "settlement_date": (order_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                "utr": ref,
            })
            expected = "duplicate_settlement"
        elif case == 2:
            emit_settlement = emit_bank = False
            expected = "no_settlement_found"
        elif case == 3:
            settled = round(settled - random.uniform(10, 90), 2)
            expected = "fee_footing_mismatch"
        elif case == 4:
            credit_lag = 9                  # well past the window
            expected = "bank_credit_delayed"
        elif case == 5:
            emit_bank = False
            expected = "settlement_not_credited"
        elif case == 6:
            ledger_amount = round(gross + 500.0, 2)
            expected = "ledger_gross_amount_mismatch"

        ledger.append({
            "ledger_id": f"L{i:05d}", "order_id": oid, "customer": customer,
            "amount": ledger_amount, "date": order_date.strftime("%Y-%m-%d"),
            "status": status,
        })
        truth.append({"order_id": oid, "expected_reason_code": expected,
                      "note": f"alt-format batch, case {case}"})

        if not emit_settlement:
            continue

        settlements.append({
            "settlement_id": f"S{i:05d}", "payment_id": f"P{i:05d}",
            "order_id": oid, "gross_amount": gross, "fee": fee, "tax": tax,
            "refund_amount": refund, "settled_amount": settled,
            "settlement_date": (order_date + timedelta(days=1)).strftime("%Y-%m-%d"),
            "utr": ref,
        })

        if not emit_bank:
            continue

        bank_amount = settled
        if case == 7:
            bank_amount = round(settled - 175.0, 2)
            truth[-1]["expected_reason_code"] = "bank_amount_mismatch"

        bank.append({
            "txn_id": f"T{i:06d}",
            "date": (order_date + timedelta(days=credit_lag)).strftime("%Y-%m-%d"),
            "amount": bank_amount,
            "narration": NARRATION_STYLES[i % len(NARRATION_STYLES)](ref, customer),
            "type": "credit",
        })

    return ledger, settlements, bank, truth


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    ledger, settlements, bank, truth = make_dataset()
    write_csv(f"{OUT_DIR}/internal_ledger.csv", ledger,
              ["ledger_id", "order_id", "customer", "amount", "date", "status"])
    write_csv(f"{OUT_DIR}/razorpay_settlements.csv", settlements,
              ["settlement_id", "payment_id", "order_id", "gross_amount", "fee",
               "tax", "refund_amount", "settled_amount", "settlement_date", "utr"])
    write_csv(f"{OUT_DIR}/bank_statement.csv", bank,
              ["txn_id", "date", "amount", "narration", "type"])
    write_csv(f"{OUT_DIR}/ground_truth.csv", truth,
              ["order_id", "expected_reason_code", "note"])
    print(f"alt-format batch written to {OUT_DIR}/  "
          f"({len(ledger)} orders, {len(bank)} bank rows)")
