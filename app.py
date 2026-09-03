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

from flask import Flask, jsonify, render_template, request, send_file

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import pandas as pd  # noqa: E402

from src import matcher, schema  # noqa: E402
from src.evaluate import run_evaluation  # noqa: E402
from src.reconcile import SourceUnavailable, run_reconciliation  # noqa: E402
from webapp import pdf_export  # noqa: E402

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


def _problem(status, title, detail, fix=None, slot=None, missing=None,
             columns=None):
    payload = {"title": title, "detail": detail}
    if fix:
        payload["fix"] = fix
    if slot:
        payload["slot"] = slot
    if missing:
        payload["missing_columns"] = missing
    if columns:
        payload["found_columns"] = columns
    return jsonify(payload), status


def _explain_source_error(error):
    """
    Turn a SourceUnavailable into something a finance person can act on.

    The pipeline's own message names a temp path and a field set: precise, and
    useless to the person holding the spreadsheet. The facts come off the
    exception's attributes rather than out of its prose -- parsing the wording
    back out was fragile and broke the first time the wording changed.
    """
    slot = getattr(error, "source", None)
    missing = getattr(error, "missing", None) or []
    columns = getattr(error, "columns", None) or []

    if missing:
        return _problem(
            400,
            "Some columns could not be matched",
            f"The file you gave as {HUMAN.get(slot, slot or 'a source')} has "
            f"columns this reconciliation could not place. Column names do not "
            f"have to match exactly — spacing, case and common alternatives are "
            f"handled — but these had no recognisable equivalent.",
            fix="Pick the right column for each one on the next screen, or "
                "re-export with clearer headers.",
            slot=slot, missing=missing, columns=columns,
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


def _run(data_dir, label, use_llm=True, mappings=None, llm_schema=False):
    """Run the real pipeline and package what the page needs."""
    out = Path(tempfile.mkdtemp())
    try:
        report, _ = run_reconciliation(
            data_dir=str(data_dir), output_dir=str(out), enable_llm=use_llm,
            column_mappings=mappings, allow_llm_schema=llm_schema)
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


@app.post("/api/inspect")
def inspect():
    """
    Read the headers of the three files and report what could be placed.

    Called before reconciling so the page can put a mapping step in front of the
    user when their column names are not ones the alias table knows -- which is
    the normal case for a real export. Reads the header row only; no
    reconciliation happens here.
    """
    missing = [s for s in SLOTS if s not in request.files or not request.files[s].filename]
    if missing:
        return _problem(
            400, "Some files are missing",
            "Reconciliation needs all three sources — one on its own cannot be "
            "checked against anything.",
            fix="Add: " + ", ".join(HUMAN[s] for s in missing) + ".",
            slot=missing[0],
        )

    out = {}
    for slot in SLOTS:
        upload = request.files[slot]
        try:
            head = pd.read_csv(upload.stream, nrows=0)
        except Exception:
            return _problem(
                400, "That file could not be read",
                f"{HUMAN[slot].capitalize()} could not be parsed as CSV.",
                fix="Check it is a plain CSV export rather than an .xlsx renamed "
                    "to .csv, and that it has a header row.",
                slot=slot,
            )
        finally:
            upload.stream.seek(0)

        found = schema.resolve(head.columns, slot)
        if not found["ready"] and request.form.get("llm_schema") == "1":
            # header names only -- see src/schema_llm.py. The proposal is
            # verified against the file inside load_source before it is used;
            # here it only pre-fills the mapping screen, which a human confirms.
            from src import schema_llm

            proposal = schema_llm.propose_mapping(
                head.columns, slot, filename=upload.filename)
            for u in found["unresolved"]:
                guess = proposal["mapping"].get(u["field"])
                if guess and guess in found["columns"]:
                    u["suggestion"] = guess
                    u["from_model"] = True
        found["filename"] = upload.filename
        found["label"] = HUMAN[slot]
        out[slot] = found

    return jsonify({
        "sources": out,
        "ready": all(v["ready"] or v["split_amount"] for v in out.values()),
    })


def _mapping_from_request(slot):
    """An explicit, human-confirmed mapping for one source, if the page sent one."""
    raw = request.form.get(f"mapping_{slot}")
    if not raw:
        return None
    try:
        chosen = json.loads(raw)
    except json.JSONDecodeError:
        return None
    # blank means "not present"; only real column names travel onward
    return {field: column for field, column in chosen.items() if column} or None


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
        mappings = {slot: _mapping_from_request(slot) for slot in SLOTS}
        llm_schema = request.form.get("llm_schema") == "1"

        # A holiday calendar applies to this run only, and is put back after,
        # so one request cannot change how the next one counts a window.
        previous = matcher.BANK_HOLIDAYS
        try:
            if holiday_path:
                matcher.BANK_HOLIDAYS = matcher.load_bank_holidays(str(holiday_path))
            return jsonify(_run(work, names, use_llm=use_llm, mappings=mappings,
                                llm_schema=llm_schema))
        finally:
            matcher.BANK_HOLIDAYS = previous

    except SourceUnavailable as e:
        return _explain_source_error(e)
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


@app.post("/api/report.pdf")
def report_pdf():
    """
    The close pack, as paper.

    The page posts back the run it is already holding rather than the server
    re-reconciling: a second run would produce a second set of numbers, and the
    document has to be the one on screen. Nothing here computes -- see
    webapp/pdf_export.py.
    """
    return _pdf(pdf_export.build_report_pdf, "reconciliation_close_pack.pdf")


@app.post("/api/audit.pdf")
def audit_pdf():
    """The hash-chained trail, re-verified against the bytes being printed."""
    return _pdf(pdf_export.build_audit_pdf, "audit_trail.pdf")


def _pdf(builder, filename):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload.get("report") and not payload.get("audit_trail"):
        return _problem(
            400, "Nothing to put in a PDF",
            "The download needs the result of a reconciliation run, and none "
            "was sent with the request.",
            fix="Run a reconciliation first, then download from the results page.",
        )

    work = Path(tempfile.mkdtemp())
    try:
        path = builder(payload, work / filename)
        return send_file(path, mimetype="application/pdf",
                         as_attachment=True, download_name=filename,
                         max_age=0)
    except Exception:
        traceback.print_exc()
        return _problem(
            500, "The PDF could not be rendered",
            "The run itself is fine — this failed while typesetting it.",
            fix="The JSON download still works, and the traceback is in the "
                "terminal running this server.",
        )
    finally:
        # send_file has read the bytes by the time Flask returns, but the
        # directory must outlive the response object, so it is cleaned on the
        # next request rather than here.
        app.config.setdefault("_pdf_temp", []).append(work)
        _sweep_pdf_temp(keep=work)


def _sweep_pdf_temp(keep=None):
    """Delete the previous request's PDF scratch directory."""
    pending = app.config.get("_pdf_temp") or []
    remaining = []
    for path in pending:
        if path == keep:
            remaining.append(path)
        else:
            shutil.rmtree(path, ignore_errors=True)
    app.config["_pdf_temp"] = remaining


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
        return _explain_source_error(e)


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
