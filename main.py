"""
Entry point.

    python main.py              run the reconciliation, print the report
    python main.py --evaluate   also score the run against data/ground_truth.csv
    python main.py --alt        run the same code against a batch using an
                                entirely different bank's conventions
    python main.py --data DIR   run against any batch directory, e.g. the
                                realistic-density one from data/make_realistic.py
    python main.py --prove      demonstrate live that a confidently wrong
                                proposal still cannot become a match
    python main.py --brief      draft plain-English briefs for the top
                                incidents, each verified against its own facts
"""

import sys

# Loaded here, at the entry point, and deliberately NOT in src/. Tests import
# src directly and must never pick up a real key: a suite that quietly starts
# making network calls is slow, costs money, and stops giving the same answer
# twice. Keeping the load out of the package is what guarantees that.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # optional; the pipeline degrades honestly without a key

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
    if "--data" in sys.argv:
        # An explicit directory wins over --alt; the two together is a typo, not
        # a request, and silently honouring one of them hides it.
        try:
            data_dir = sys.argv[sys.argv.index("--data") + 1]
        except IndexError:
            # `--data` with nothing after it is a typo, and falling back to the
            # default batch would run happily against data the user did not ask
            # for -- the one outcome worse than stopping.
            sys.exit("--data needs a directory, e.g. --data data/realistic")
        if data_dir.startswith("--"):
            sys.exit(f"--data needs a directory, got the flag {data_dir!r}")
        alt = False
        print(f"Running against {data_dir} — no code changes.\n")
    if alt:
        print("Running against the ALT-FORMAT batch "
              "(RRN/IMPS refs, flat+pct fees, T+1 cadence) — no code changes.\n")

    report, _ = run_reconciliation(data_dir=data_dir)

    # Kept behind a flag so a default run still makes the same three narration
    # calls regardless of batch size, which is a property worth not muddying.
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

    print_summary(report)
    print("\nFull audit trail: output/audit_trail.jsonl")
    print("Full report:      output/reconciliation_report.json")

    if "--evaluate" in sys.argv:
        from src.evaluate import print_evaluation, run_evaluation

        print()
        _, result, ablation_rows = run_evaluation(data_dir=data_dir)
        print_evaluation(result, ablation_rows)
        print("\nFull evaluation:  output/evaluation.json")
