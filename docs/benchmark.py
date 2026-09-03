"""
Prints the throughput table for README.md, to be pasted in.

The scale figures are the one set of numbers in this project that no committed
script produced, so they drifted: the false-positive column was measured before
the ambiguity filter was tightened and nothing re-checked it. This exists so
that table is reproducible rather than remembered.

    python docs/benchmark.py

Deterministic: the generator seeds at import, and this re-seeds before each
batch, so a fresh run reproduces the table exactly. Timings are best-of-three
wall clock and will differ by machine; the fault, missed and false-positive
columns will not.

No API key is used. Tier 3 is not exercised here -- its cost is three calls on
this fixture at any size, so it would swamp the deterministic measurement this
table exists to report.
"""

import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("OPENAI_API_KEY", None)  # deterministic tiers only

import pandas as pd  # noqa: E402

import data.generate_synthetic as gen  # noqa: E402
from src.evaluate import score  # noqa: E402
from src.reconcile import run_reconciliation  # noqa: E402

SIZES = (55, 500, 5000)  # booked orders; the report's order universe can exceed this
REPEATS = 3

COLUMNS = {
    "internal_ledger.csv": ["ledger_id", "order_id", "customer", "amount", "date",
                            "status"],
    "razorpay_settlements.csv": ["settlement_id", "payment_id", "order_id",
                                 "gross_amount", "fee", "tax", "refund_amount",
                                 "settled_amount", "settlement_date", "utr"],
    "bank_statement.csv": ["txn_id", "date", "amount", "narration", "type"],
    "ground_truth.csv": ["order_id", "expected_reason_code", "note"],
}


def build_batch(n_orders, directory):
    """A batch of n_orders, written where the pipeline can read it."""
    random.seed(42)  # same state a fresh import gets, so sizes are comparable
    gen.N_ORDERS = n_orders
    ledger, settlements, bank, truth = gen.make_dataset()

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(directory)
    try:
        for name, rows in (("internal_ledger.csv", ledger),
                           ("razorpay_settlements.csv", settlements),
                           ("bank_statement.csv", bank),
                           ("ground_truth.csv", truth)):
            gen.write_csv(name, rows, COLUMNS[name])
    finally:
        os.chdir(cwd)
    return directory


def measure(n_orders):
    directory = build_batch(n_orders, tempfile.mkdtemp(prefix=f"bench-{n_orders}-"))
    fastest = None
    for _ in range(REPEATS):
        report, _ = run_reconciliation(data_dir=str(directory), write_outputs=False)
        if (fastest is None or report["throughput"]["wall_clock_ms"]
                < fastest["throughput"]["wall_clock_ms"]):
            fastest = report

    result = score(pd.read_csv(directory / "ground_truth.csv"), fastest)
    detection = result["fault_detection"]
    ambiguous = sum(1 for e in fastest["exceptions"]
                    if e["reason_code"] == "attribution_ambiguous")
    return {
        "orders": fastest["total_orders"],
        "wall_clock_ms": fastest["throughput"]["wall_clock_ms"],
        "records_per_second": fastest["throughput"]["records_per_second"],
        "injected": detection["injected_faults"],
        "detected": detection["detected"],
        "missed": detection["missed"],
        "false_positives": detection["false_positives"],
        "attribution_ambiguous": ambiguous,
    }


def main():
    rows = [measure(n) for n in SIZES]

    print("| Orders | Wall clock | Records/sec | Faults caught | Missed | "
          "False positives |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['orders']:,} | {r['wall_clock_ms']:,.1f} ms | "
              f"{r['records_per_second']:,.0f} | {r['detected']:,} / "
              f"{r['injected']:,} | **{r['missed']}** | {r['false_positives']} |")

    print()
    for r in rows:
        print(f"{r['orders']:>5,} orders: {r['attribution_ambiguous']} of "
              f"{r['false_positives']} false positives are attribution_ambiguous")


if __name__ == "__main__":
    main()
