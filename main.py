"""
Entry point.

    python main.py              run the reconciliation, print the report
    python main.py --evaluate   also score the run against data/ground_truth.csv
"""

import sys

from src.reconcile import run_reconciliation
from src.report import print_summary

if __name__ == "__main__":
    report, _ = run_reconciliation()
    print_summary(report)
    print("\nFull audit trail: output/audit_trail.jsonl")
    print("Full report:      output/reconciliation_report.json")

    if "--evaluate" in sys.argv:
        from src.evaluate import print_evaluation, run_evaluation

        print()
        _, result, ablation_rows = run_evaluation()
        print_evaluation(result, ablation_rows)
        print("\nFull evaluation:  output/evaluation.json")
