"""
Entry point.

    python main.py              run the reconciliation, print the report
    python main.py --evaluate   also score the run against data/ground_truth.csv
    python main.py --alt        run the same code against a batch using an
                                entirely different bank's conventions
"""

import sys

from src.reconcile import run_reconciliation
from src.report import print_summary

if __name__ == "__main__":
    # Same pipeline, same thresholds, no retuning -- only the data's
    # conventions differ. See data/generate_alt_format.py.
    alt = "--alt" in sys.argv
    data_dir = "data/alt" if alt else "data"
    if alt:
        print("Running against the ALT-FORMAT batch "
              "(RRN/IMPS refs, flat+pct fees, T+1 cadence) — no code changes.\n")

    report, _ = run_reconciliation(data_dir=data_dir)
    print_summary(report)
    print("\nFull audit trail: output/audit_trail.jsonl")
    print("Full report:      output/reconciliation_report.json")

    if "--evaluate" in sys.argv:
        from src.evaluate import print_evaluation, run_evaluation

        print()
        _, result, ablation_rows = run_evaluation(data_dir=data_dir)
        print_evaluation(result, ablation_rows)
        print("\nFull evaluation:  output/evaluation.json")
