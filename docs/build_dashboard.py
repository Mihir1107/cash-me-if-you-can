"""
Build the close desk: a self-contained HTML view of a real reconciliation run.

The pipeline is a command line tool, and the person who actually has to work
its output is a finance controller who does not have Python. So the pipeline
emits JSON, and this builds a page from it. The separation matters: no figure
on the page is typed, and nothing in the UI can compute a number the pipeline
did not already stand behind.

Two runs are embedded so the page can switch between them:

  primary    57 orders at ~56% fault density -- the test fixture, dense on
             purpose so every reason code fires
  realistic  2,000 orders at ~3% -- what a normal month looks like

Same code, same thresholds, both. That switch is the honest answer to "your
match rate is only 42%".

    python docs/build_dashboard.py            # writes docs/close-desk.html

Run with OPENAI_API_KEY set to capture the full three-tier pipeline, or without
to capture the degraded configuration.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
WEBAPP = ROOT / "webapp"
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from src.audit import verify_chain  # noqa: E402
from src.evaluate import run_evaluation  # noqa: E402
from src.reconcile import run_reconciliation  # noqa: E402

BATCHES = {
    "primary": ROOT / "data",
    "realistic": ROOT / "data" / "realistic",
}

# A third batch, generated here rather than committed, because it exists to show
# one specific behaviour: the ambiguity filter only starts refusing at volume.
# At 57 and 2,000 orders it never fires -- no two outstanding settlements collide
# on both amount and timing -- so a page built from those alone would show the
# project's central mechanism sitting at zero. At 5,000 it fires, and the cost of
# the guarantee becomes visible instead of asserted.
STRESS_ORDERS = 5000


def wilson_text(result, field):
    """Recall/precision with the interval, formatted the way the CLI prints it."""
    fd = result["fault_detection"]
    value = fd.get(field)
    lo, hi = fd.get(field + "_ci95", (None, None))
    if value is None:
        return "—"
    text = f"{value * 100:.2f}%"
    if lo is not None:
        text += f"  [{lo * 100:.1f}–{hi * 100:.1f}]"
    return text


def build_stress_batch():
    """Generate a 5,000-order batch into a temp dir. Same generator, same seed."""
    import data.generate_synthetic as gen

    original = gen.N_ORDERS
    gen.N_ORDERS = STRESS_ORDERS
    try:
        ledger, settlements, bank, truth = gen.make_dataset()
    finally:
        gen.N_ORDERS = original

    out = Path(tempfile.mkdtemp())
    gen.write_csv(out / "internal_ledger.csv", ledger,
                  ["ledger_id", "order_id", "customer", "amount", "date", "status"])
    gen.write_csv(out / "razorpay_settlements.csv", settlements,
                  ["settlement_id", "payment_id", "order_id", "gross_amount", "fee",
                   "tax", "refund_amount", "settled_amount", "settlement_date", "utr"])
    gen.write_csv(out / "bank_statement.csv", bank,
                  ["txn_id", "date", "amount", "narration", "type"])
    gen.write_csv(out / "ground_truth.csv", truth,
                  ["order_id", "expected_reason_code", "note"])
    return out


def capture(name, data_dir, label=None):
    out = Path(tempfile.mkdtemp())
    report, _ = run_reconciliation(data_dir=str(data_dir), output_dir=str(out))
    _, result, _ = run_evaluation(data_dir=str(data_dir), output_dir=str(tempfile.mkdtemp()))
    fd = result["fault_detection"]

    # a few real digests off the log, so the chain is visible rather than asserted
    chain_sample = []
    trail = out / "audit_trail.jsonl"
    if trail.exists():
        for line in trail.read_text().splitlines()[:3]:
            if not line.strip():
                continue
            entry = json.loads(line)
            chain_sample.append(
                f"{entry.get('entry_hash', '')[:20]}…  ←  {entry.get('prev_hash', '')[:12]}…")
        report["audit_trail"] = verify_chain(str(trail))

    try:
        report["source_dir"] = str(data_dir.relative_to(ROOT))
    except ValueError:
        report["source_dir"] = label or "generated batch"

    # The page never reads the per-order exception list and it is by far the
    # largest thing in the payload, so it does not travel.
    report.pop("exceptions", None)
    report["chain_sample"] = chain_sample
    report["scoring"] = {
        "injected_faults": fd["injected_faults"],
        "detected": fd["detected"],
        "missed": fd["missed"],
        "false_positives": fd["false_positives"],
        "recall": wilson_text(result, "recall"),
        "accuracy": f"{result['exact_label_accuracy'] * 100:.2f}%",
    }
    print(f"  {name:<10} {report['total_orders']:>5} orders  "
          f"match {report['match_rate_pct']:>6.2f}%  "
          f"{fd['detected']}/{fd['injected_faults']} faults  "
          f"{report['triage']['incident_count']} incidents")
    return report


def main():
    if not BATCHES["realistic"].exists():
        sys.exit("data/realistic is missing — run `python data/make_realistic.py` first")

    print("capturing runs:")
    runs = {name: capture(name, path) for name, path in BATCHES.items()}
    runs["stress"] = capture(
        "stress", build_stress_batch(),
        label=f"generated, {STRESS_ORDERS:,} orders at fixture density")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runs": runs,
    }

    # The design and the render logic live in webapp/static, one copy, shared
    # with the live tool. This inlines them so the artifact stays self-contained.
    css = (WEBAPP / "static" / "desk.css").read_text()
    js = (WEBAPP / "static" / "desk.js").read_text()
    desk = (WEBAPP / "templates" / "_desk.html").read_text()

    # </script> inside embedded JSON would close the tag early
    blob = json.dumps(payload, default=str).replace("</", "<\\/")

    html = (DOCS / "dashboard_shell.html").read_text()
    html = (html
            .replace("/*__CSS__*/", css)
            .replace("<!--__DESK__-->", desk)
            .replace("/*__DESK_JS__*/", js)
            .replace("/*__DATA__*/", blob))

    target = DOCS / "close-desk.html"
    target.write_text(html)
    size = target.stat().st_size / 1024
    print(f"\nwrote {target.relative_to(ROOT)}  ({size:.0f} KB, self-contained)")
    if os.getenv("OPENAI_API_KEY"):
        print("captured WITH an API key: all three narration tiers ran")
    else:
        print("captured with NO API key: tier 3 made zero calls, degraded honestly")


if __name__ == "__main__":
    main()
