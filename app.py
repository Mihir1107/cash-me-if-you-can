"""
The close desk, as a local web app.

    pip install -r requirements.txt
    python app.py           # then open http://127.0.0.1:5051

Why this exists: the pipeline is a command line tool and the person whose job
this is does not have Python. This wraps it in a browser, so three CSVs go in
and a close decision comes out.

Two properties worth stating, because both are load-bearing:

**It computes nothing.** Every route here saves uploads to a temp directory and
calls `run_reconciliation()` -- the same function `main.py` calls, unchanged.
The browser renders the report it gets back. There is no second implementation
of the matching logic in JavaScript, and there must never be one: three bugs in
this project came from shared logic having a copy that drifted, and the copy
that must never drift is the one deciding whether money matched.

**Nothing leaves the machine.** It binds to 127.0.0.1, uploads go to a temp
directory that is deleted when the request finishes, and the only outbound call
the process can make is Tier 3's narration lookup -- which sends one bank
narration string and no amounts, and only if a key is configured. A finance
person's ledger is not something to be casual about, so the default is local and
the option to turn the model off is on the page.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from src import matcher  # noqa: E402
from src.evaluate import run_evaluation  # noqa: E402
from src.reconcile import SourceUnavailable, run_reconciliation  # noqa: E402

app = Flask(__name__, template_folder="webapp/templates", static_folder="webapp/static")

# Generous for a reconciliation batch, small enough that a misdropped video
# fails fast with a readable message rather than eating memory.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

SLOTS = {
    "ledger": "internal_ledger.csv",
    "settlements": "razorpay_settlements.csv",
    "bank": "bank_statement.csv",
}

SAMPLES = {
    "primary": ROOT / "data",
    "realistic": ROOT / "data" / "realistic",
    "alt": ROOT / "data" / "alt",
}

# What each file is for, in the words used on the page, so an error can say
# which of the three the user needs to go and re-export.
HUMAN = {
    "ledger": "your ledger",
    "settlements": "the Razorpay settlement report",
    "bank": "the bank statement",
}


def _problem(status, title, detail, fix=None, slot=None, missing=None):
    payload = {"title": title, "detail": detail}
    if fix:
        payload["fix"] = fix
    if slot:
        payload["slot"] = slot
    if missing:
        payload["missing_columns"] = missing
    return jsonify(payload), status


def _explain_source_error(message):
    """
    Turn the pipeline's own SourceUnavailable text into something a finance
    person can act on. The pipeline's message is precise and names a temp path,
    which is precise and useless to the person reading it.
    """
    slot = next((s for s in SLOTS if s in message), None)
    missing = []
    if "missing required columns" in message:
        raw = message.split("missing required columns:", 1)[1].strip()
        missing = [c.strip(" '\"[]") for c in raw.split(",") if c.strip(" '\"[]")]
        return _problem(
            400,
            "A column is missing",
            f"The file you gave as {HUMAN.get(slot, slot or 'a source')} does not "
            f"have everything the reconciliation needs.",
            fix="Re-export it with those columns included, or rename the existing "
                "ones to match. Column names are case-sensitive.",
            slot=slot, missing=missing,
        )
    return _problem(
        400,
        "That file could not be read",
        f"{HUMAN.get(slot, 'One of the files').capitalize()} could not be parsed as CSV.",
        fix="Check it is a plain CSV export rather than an .xlsx renamed to .csv, "
            "and that it has a header row.",
        slot=slot,
    )


def _score_if_possible(data_dir):
    """
    Score the run, but only where an answer key genuinely exists.

    The bundled samples ship a `ground_truth.csv` written by the generator at
    injection time. A real merchant's upload has nothing of the kind -- if they
    knew which rows were wrong they would not need this. So the scorecard is
    populated for samples and honestly absent for uploads, rather than rendering
    empty and implying the pipeline checked itself and found nothing.
    """
    if not (Path(data_dir) / "ground_truth.csv").exists():
        return None

    out = Path(tempfile.mkdtemp())
    try:
        _, result, _ = run_evaluation(data_dir=str(data_dir), output_dir=str(out))
    except Exception:
        traceback.print_exc()
        return None          # a scorecard is a nicety; never fail a run over it
    finally:
        shutil.rmtree(out, ignore_errors=True)

    fd = result["fault_detection"]
    lo, hi = fd.get("recall_ci95", (None, None))
    recall = f"{fd['recall'] * 100:.2f}%"
    if lo is not None:
        recall += f"  [{lo * 100:.1f}–{hi * 100:.1f}]"
    return {
        "injected_faults": fd["injected_faults"],
        "detected": fd["detected"],
        "missed": fd["missed"],
        "false_positives": fd["false_positives"],
        "recall": recall,
        "accuracy": f"{result['exact_label_accuracy'] * 100:.2f}%",
    }


def _run(data_dir, label, use_llm=True):
    """Run the real pipeline and package what the page needs."""
    out = Path(tempfile.mkdtemp())
    try:
        report, _ = run_reconciliation(
            data_dir=str(data_dir), output_dir=str(out), enable_llm=use_llm)
        report["source_dir"] = label
        report.pop("exceptions", None)   # the page does not read it; it is large

        trail = out / "audit_trail.jsonl"
        audit_text = trail.read_text() if trail.exists() else ""
        chain = []
        for line in audit_text.splitlines()[:3]:
            if line.strip():
                entry = json.loads(line)
                chain.append(f"{entry.get('entry_hash', '')[:20]}…  ←  "
                             f"{entry.get('prev_hash', '')[:12]}…")
        report["chain_sample"] = chain
        report["scoring"] = _score_if_possible(data_dir)
        return {"report": report, "audit_trail": audit_text}
    finally:
        shutil.rmtree(out, ignore_errors=True)


@app.get("/")
def index():
    return render_template("app.html")


@app.post("/api/reconcile")
def reconcile():
    missing = [s for s in SLOTS if s not in request.files or not request.files[s].filename]
    if missing:
        return _problem(
            400, "Some files are missing",
            "Reconciliation needs all three sources — one on its own cannot be "
            "checked against anything.",
            fix="Add: " + ", ".join(HUMAN[s] for s in missing) + ".",
            slot=missing[0],
        )

    work = Path(tempfile.mkdtemp())
    holiday_path = None
    try:
        for slot, filename in SLOTS.items():
            request.files[slot].save(work / filename)

        holidays = request.files.get("holidays")
        if holidays and holidays.filename:
            holiday_path = work / "holidays.csv"
            holidays.save(holiday_path)

        use_llm = request.form.get("use_llm", "1") == "1"
        names = ", ".join(request.files[s].filename for s in SLOTS)

        # A holiday calendar applies to this run only, and is put back after,
        # so one request cannot change how the next one counts a window.
        previous = matcher.BANK_HOLIDAYS
        try:
            if holiday_path:
                matcher.BANK_HOLIDAYS = matcher.load_bank_holidays(str(holiday_path))
            return jsonify(_run(work, names, use_llm=use_llm))
        finally:
            matcher.BANK_HOLIDAYS = previous

    except SourceUnavailable as e:
        return _explain_source_error(str(e))
    except Exception:
        traceback.print_exc()
        return _problem(
            500, "The reconciliation failed",
            "Something in the pipeline raised an error partway through, so there "
            "is no result to show. Nothing was written and nothing was changed.",
            fix="The full traceback is in the terminal running this server.",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/api/sample/<name>")
def sample(name):
    path = SAMPLES.get(name)
    if path is None or not path.exists():
        return _problem(
            404, "No such sample",
            f"There is no bundled sample called {name!r}.",
            fix="Available: " + ", ".join(sorted(SAMPLES)) + ". The realistic one "
                "needs `python data/make_realistic.py` first.",
        )
    try:
        return jsonify(_run(path, f"sample: {name}"))
    except SourceUnavailable as e:
        return _explain_source_error(str(e))


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    # Not 5000. On macOS that is AirPlay Receiver, which answers with a 403 and
    # makes it look like the app is broken rather than absent -- it cost an hour
    # here before the port was the suspect.
    port = int(os.getenv("PORT", "5051"))

    print(f"\n  Close desk running at http://{host}:{port}")
    print("  Files stay on this machine. Ctrl-C to stop.\n")
    try:
        app.run(host=host, port=port, debug=False)
    except OSError as e:
        print(f"\n  Could not start on port {port}: {e}")
        print(f"  Something else is already using it. Try:  PORT=5152 python app.py\n")
        raise SystemExit(1)
