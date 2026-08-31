"""
The web app, tested at its edges.

The app itself is thin on purpose — it saves uploads and calls the same
`run_reconciliation()` that `main.py` calls. So these tests are about the parts
that are genuinely the app's own responsibility, and every one of them is a way
a real person's afternoon gets wasted:

  * a file that is nearly right must say which column is missing, not "invalid"
  * a run must not leave the uploaded ledger sitting in a temp directory
  * a holiday calendar supplied for one run must not change the next one
  * a scorecard must be absent when there is no answer key, not blank
"""

import io
import json
import tempfile
from pathlib import Path

import pytest

import app as webapp
from src import matcher

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def csv_file(path, name=None):
    return (io.BytesIO(Path(path).read_bytes()), name or Path(path).name)


def three_good():
    return {
        "ledger": csv_file(DATA / "internal_ledger.csv"),
        "settlements": csv_file(DATA / "razorpay_settlements.csv"),
        "bank": csv_file(DATA / "bank_statement.csv"),
        "use_llm": "0",
    }


# ------------------------------------------------------------------ serving

def test_the_page_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-slot="ledger"' in body
    assert 'data-slot="settlements"' in body
    assert 'data-slot="bank"' in body


def test_the_page_says_files_stay_local(client):
    """
    A finance person is handing over their whole ledger. If that claim ever
    stops being true, this test should be the thing that breaks.
    """
    assert "Nothing is uploaded anywhere" in client.get("/").get_data(as_text=True)


# ------------------------------------------------------------ the happy path

def test_three_good_files_reconcile(client):
    r = client.post("/api/reconcile", data=three_good(),
                    content_type="multipart/form-data")
    assert r.status_code == 200
    report = r.get_json()["report"]
    assert report["total_orders"] == 57
    assert report["money"]["identity"]["holds"]
    assert report["close_gate"]["conditions_checked"] == 7


def test_the_app_returns_the_pipeline_s_own_numbers(client):
    """
    The app must not compute. Same inputs through the library and through HTTP
    have to agree, or something in the web layer is doing arithmetic it has no
    business doing.
    """
    from src.reconcile import run_reconciliation

    direct, _ = run_reconciliation(
        data_dir=str(DATA), output_dir=tempfile.mkdtemp(), enable_llm=False)
    served = client.post("/api/reconcile", data=three_good(),
                         content_type="multipart/form-data").get_json()["report"]

    assert served["match_rate_pct"] == direct["match_rate_pct"]
    assert served["money"]["at_risk_value"] == direct["money"]["at_risk_value"]
    assert served["triage"]["incident_count"] == direct["triage"]["incident_count"]


def test_a_run_ships_its_audit_trail(client):
    r = client.post("/api/reconcile", data=three_good(),
                    content_type="multipart/form-data")
    trail = r.get_json()["audit_trail"]
    first = json.loads(trail.splitlines()[0])
    assert first["entry_hash"] and first["prev_hash"]


def test_uploads_do_not_outlive_the_request(client, monkeypatch):
    """Someone's ledger must not be left behind in a temp directory."""
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: made.append(real(*a, **k)) or made[-1])

    client.post("/api/reconcile", data=three_good(), content_type="multipart/form-data")

    leftover = [d for d in made if Path(d).exists()]
    assert not leftover, f"temp directories survived the request: {leftover}"


# ----------------------------------------------------------- what goes wrong

def test_a_missing_file_names_which_one(client):
    r = client.post("/api/reconcile",
                    data={"ledger": csv_file(DATA / "internal_ledger.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    body = r.get_json()
    assert "the Razorpay settlement report" in body["fix"]
    assert "the bank statement" in body["fix"]


def test_a_missing_column_is_named_and_located(client, tmp_path):
    """
    The difference between a five-second fix and a support ticket. The response
    has to say which file, which column, and what to do.
    """
    import csv

    rows = list(csv.DictReader((DATA / "internal_ledger.csv").open()))
    for row in rows:
        row.pop("amount")
    broken = tmp_path / "ledger.csv"
    with broken.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    data = three_good()
    data["ledger"] = csv_file(broken)
    r = client.post("/api/reconcile", data=data, content_type="multipart/form-data")

    assert r.status_code == 400
    body = r.get_json()
    assert body["missing_columns"] == ["amount"]
    assert body["slot"] == "ledger"
    assert "your ledger" in body["detail"]


def test_a_file_that_is_not_a_csv_says_so(client, tmp_path):
    junk = tmp_path / "ledger.csv"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00not a csv at all")

    data = three_good()
    data["ledger"] = csv_file(junk)
    r = client.post("/api/reconcile", data=data, content_type="multipart/form-data")

    assert r.status_code == 400
    assert "could not be parsed" in r.get_json()["detail"]
    assert ".xlsx" in r.get_json()["fix"]      # the actual most likely cause


# -------------------------------------------------------------- the samples

@pytest.mark.parametrize("name", ["primary", "alt"])
def test_the_bundled_samples_run(client, name):
    r = client.post(f"/api/sample/{name}")
    assert r.status_code == 200
    assert r.get_json()["report"]["total_orders"] > 0


def test_a_sample_carries_a_scorecard_because_it_has_an_answer_key(client):
    scoring = client.post("/api/sample/primary").get_json()["report"]["scoring"]
    assert scoring["missed"] == 0
    assert scoring["detected"] == scoring["injected_faults"]


def test_an_upload_carries_no_scorecard_because_it_has_no_answer_key(client):
    """
    Absent, not blank. A merchant who knew which rows were wrong would not need
    this tool, and an empty scorecard reads as "checked, nothing found".
    """
    report = client.post("/api/reconcile", data=three_good(),
                         content_type="multipart/form-data").get_json()["report"]
    assert report["scoring"] is None


def test_an_unknown_sample_lists_the_real_ones(client):
    r = client.post("/api/sample/nope")
    assert r.status_code == 404
    assert "primary" in r.get_json()["fix"]


# ------------------------------------------------ columns that are not ours

def renamed(source_csv, headers, tmp_path, name):
    """Rewrite a fixture's headers into what a real export would call them."""
    import csv

    rows = list(csv.DictReader(Path(source_csv).open()))
    out = [{new: r[old] for old, new in headers.items()} for r in rows]
    path = tmp_path / name
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(headers.values()))
        w.writeheader()
        w.writerows(out)
    return path


TALLY_LEDGER = {"ledger_id": "Voucher No", "order_id": "Order Ref",
                "customer": "Party Name", "amount": "Amount (INR)",
                "date": "Booking Date", "status": "Payment Status"}
DASHBOARD_SETTLEMENTS = {
    "settlement_id": "Settlement Id", "payment_id": "Payment Id",
    "order_id": "Order Ref", "gross_amount": "Gross Amount", "fee": "Commission",
    "tax": "GST", "refund_amount": "Refund", "settled_amount": "Net Settlement",
    "settlement_date": "Settled On", "utr": "UTR Number"}
BANK_EXPORT = {"txn_id": "Sr No", "date": "Value Date", "amount": "Amount",
               "narration": "Particulars", "type": "Dr/Cr"}


def real_world(tmp_path):
    return {
        "ledger": csv_file(renamed(DATA / "internal_ledger.csv", TALLY_LEDGER,
                                   tmp_path, "ledger.csv")),
        "settlements": csv_file(renamed(DATA / "razorpay_settlements.csv",
                                        DASHBOARD_SETTLEMENTS, tmp_path,
                                        "settlements.csv")),
        "bank": csv_file(renamed(DATA / "bank_statement.csv", BANK_EXPORT,
                                 tmp_path, "bank.csv")),
        "use_llm": "0",
    }


def test_an_export_with_ordinary_real_world_headers_just_works(client, tmp_path):
    """
    Nobody's ledger has a column called `ledger_id`. If this tool only ran on
    files that already used its own names, it only ran on its own fixture.
    """
    r = client.post("/api/reconcile", data=real_world(tmp_path),
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["report"]["total_orders"] == 57


def test_renaming_the_columns_does_not_change_a_single_figure(client, tmp_path):
    """The names are packaging. The numbers must not know they changed."""
    canonical = client.post("/api/reconcile", data=three_good(),
                            content_type="multipart/form-data").get_json()["report"]
    theirs = client.post("/api/reconcile", data=real_world(tmp_path),
                         content_type="multipart/form-data").get_json()["report"]

    assert theirs["match_rate_pct"] == canonical["match_rate_pct"]
    assert theirs["money"]["at_risk_value"] == canonical["money"]["at_risk_value"]
    assert theirs["exception_reason_counts"] == canonical["exception_reason_counts"]


def test_inspect_reports_what_it_could_and_could_not_place(client, tmp_path):
    data = real_world(tmp_path)
    data["ledger"] = csv_file(renamed(
        DATA / "internal_ledger.csv",
        {"ledger_id": "Rec Key", "order_id": "Ordr Refrence", "customer": "Buyer",
         "amount": "Sale Val", "date": "Dt", "status": "State"},
        tmp_path, "odd_ledger.csv"))

    r = client.post("/api/inspect", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    ledger = r.get_json()["sources"]["ledger"]

    assert not ledger["ready"]
    unresolved = {u["field"]: u for u in ledger["unresolved"]}
    # offered, and only offered
    assert unresolved["order_id"]["suggestion"] == "Ordr Refrence"
    assert "order_id" not in ledger["mapping"]
    assert "Rec Key" in ledger["columns"]


def test_a_confirmed_mapping_is_honoured(client, tmp_path):
    """What the mapping screen sends back, once a human has chosen."""
    data = real_world(tmp_path)
    data["ledger"] = csv_file(renamed(
        DATA / "internal_ledger.csv",
        {"ledger_id": "Rec Key", "order_id": "Ordr Refrence", "customer": "Buyer",
         "amount": "Sale Val", "date": "Dt", "status": "State"},
        tmp_path, "odd_ledger2.csv"))
    data["mapping_ledger"] = json.dumps({
        "ledger_id": "Rec Key", "order_id": "Ordr Refrence", "amount": "Sale Val",
        "date": "Dt", "status": "State", "customer": "Buyer",
    })

    r = client.post("/api/reconcile", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["report"]["total_orders"] == 57


def test_headers_nothing_recognises_are_refused_not_guessed(client, tmp_path):
    """
    The failure that matters. A column pointed at the wrong field is wrong in
    every figure afterwards and nothing downstream would ever notice, so an
    unplaceable file has to stop the run rather than proceed on a best guess.
    """
    data = real_world(tmp_path)
    data["ledger"] = csv_file(renamed(
        DATA / "internal_ledger.csv",
        {"ledger_id": "aa", "order_id": "bb", "customer": "cc",
         "amount": "dd", "date": "ee", "status": "ff"},
        tmp_path, "opaque.csv"))

    r = client.post("/api/reconcile", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    body = r.get_json()
    assert body["slot"] == "ledger"
    assert set(body["missing_columns"]) >= {"order_id", "amount"}
    assert "aa" in body["found_columns"]      # says what the file actually has


# ------------------------------------------------------- the holiday calendar

def test_a_holiday_calendar_does_not_leak_into_the_next_run(client, tmp_path):
    """
    The calendar is a module global, so one request could silently change how
    the next one counts a settlement window. It is set and put back.
    """
    cal = tmp_path / "holidays.csv"
    cal.write_text("date,name\n2026-08-17,Test Holiday\n")

    before = dict(matcher.BANK_HOLIDAYS)
    data = three_good()
    data["holidays"] = csv_file(cal)
    r = client.post("/api/reconcile", data=data, content_type="multipart/form-data")

    assert r.status_code == 200
    assert matcher.BANK_HOLIDAYS == before


def test_a_holiday_calendar_actually_reaches_the_matcher(client, tmp_path,
                                                          monkeypatch):
    """The restore above would also pass if the calendar were ignored entirely."""
    seen = {}
    real = webapp._run

    def spy(*args, **kwargs):
        seen["holidays"] = dict(matcher.BANK_HOLIDAYS)
        return real(*args, **kwargs)

    monkeypatch.setattr(webapp, "_run", spy)

    cal = tmp_path / "holidays.csv"
    cal.write_text("date,name\n2026-08-17,Test Holiday\n")
    data = three_good()
    data["holidays"] = csv_file(cal)
    client.post("/api/reconcile", data=data, content_type="multipart/form-data")

    assert len(seen["holidays"]) == 1
