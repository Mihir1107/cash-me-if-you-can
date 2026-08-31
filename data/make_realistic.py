"""
Build a realistic-density batch: 2,000 orders at roughly a 3% fault rate.

The primary batch runs at ~56% faults, which is a deliberate test-fixture
choice -- ten reason codes cannot be exercised across 57 orders at realistic
rates. But it distorts everything downstream. A controller reading that run is
looking at a work queue where half the book is broken, and the honest question
a judge should ask is: what does this look like on a normal Tuesday?

So this generates the same faults, injected by the same rules, thinned out. Not
one injection rule changes -- only FAULT_MODULUS, which controls how often the
modulo-driven cases fire. The targeted cases (the ambiguous narrations, the
reversal, the unbooked settlements) still fire exactly once each, because they
are pinned to specific order numbers, which is itself realistic: a merchant sees
one chargeback a month, not one per hundred orders.

    python data/make_realistic.py

Writes to data/realistic/. Run the pipeline against it with:

    python main.py --data data/realistic --evaluate
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.generate_synthetic as gen  # noqa: E402

ORDERS = 2000
MODULUS = 222   # 5 fault cases per modulus -> ~45 modulo faults across 2,000
OUT = Path(__file__).resolve().parent / "realistic"


def main():
    gen.N_ORDERS = ORDERS
    gen.FAULT_MODULUS = MODULUS
    ledger, settlements, bank, truth = gen.make_dataset()

    OUT.mkdir(exist_ok=True)
    gen.write_csv(OUT / "internal_ledger.csv", ledger,
                  ["ledger_id", "order_id", "customer", "amount", "date", "status"])
    gen.write_csv(OUT / "razorpay_settlements.csv", settlements,
                  ["settlement_id", "payment_id", "order_id", "gross_amount",
                   "fee", "tax", "refund_amount", "settled_amount",
                   "settlement_date", "utr"])
    gen.write_csv(OUT / "bank_statement.csv", bank,
                  ["txn_id", "date", "amount", "narration", "type"])
    gen.write_csv(OUT / "ground_truth.csv", truth,
                  ["order_id", "expected_reason_code", "note"])

    faults = sum(1 for row in truth if row["expected_reason_code"] != "matched")
    density = 100 * faults / len(truth)
    print(f"wrote {OUT}")
    print(f"  orders:        {len(truth)}")
    print(f"  faults:        {faults}")
    print(f"  fault density: {density:.2f}%   (primary batch is ~56%)")


if __name__ == "__main__":
    main()
