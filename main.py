"""
Entry point.

    python main.py              run the reconciliation, print the report
    python main.py --evaluate   also score the run against data/ground_truth.csv
    python main.py --alt        run the same code against a batch using an
                                entirely different bank's conventions
    python main.py --prove      demonstrate live that a confidently wrong
                                proposal still cannot become a match
    python main.py --brief      draft plain-English briefs for the top
                                incidents, each verified against its own facts
"""

import sys

from src.reconcile import run_reconciliation
from src.report import print_summary

if __name__ == "__main__":
    if "--prove" in sys.argv:
        from src.prove import prove_boundary

        sys.exit(0 if prove_boundary() else 1)

    # Same pipeline, same thresholds, no retuning -- only the data's
    # conventions differ. See data/generate_alt_format.py.
    alt = "--alt" in sys.argv
    data_dir = "data/alt" if alt else "data"
    if alt:
        print("Running against the ALT-FORMAT batch "
              "(RRN/IMPS refs, flat+pct fees, T+1 cadence) — no code changes.\n")

    report, _ = run_reconciliation(data_dir=data_dir)

    # Kept behind a flag so a default run still makes exactly two model calls
    # regardless of batch size, which is a property worth not muddying.
    if "--brief" in sys.argv and report.get("triage"):
        from src.brief import attach_briefs

        attach_briefs(report["triage"])
        print()
        print("=" * 62)
        print("INCIDENT BRIEFS  (model phrases, deterministic code verifies)")
        print("=" * 62)
        for incident in report["triage"]["incidents"]:
            if "brief" not in incident:
                continue
            print(f"\n[{incident['urgency'].upper()}] {incident['reason_code']}"
                  f"  {incident['value_at_risk']:,.2f}")
            print(f"  {incident['brief']}")
            print(f"  -- source: {incident['brief_source']}")
            if incident.get("brief_rejected_numbers"):
                print(f"  -- draft REJECTED, invented: "
                      f"{incident['brief_rejected_numbers']}")
        print("=" * 62)

    print_summary(report)
    print("\nFull audit trail: output/audit_trail.jsonl")
    print("Full report:      output/reconciliation_report.json")

    if "--evaluate" in sys.argv:
        from src.evaluate import print_evaluation, run_evaluation

        print()
        _, result, ablation_rows = run_evaluation(data_dir=data_dir)
        print_evaluation(result, ablation_rows)
        print("\nFull evaluation:  output/evaluation.json")
