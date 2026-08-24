"""
Adversarial suite. These are not happy-path tests -- every case here is written
to break the pipeline, on the principle that a reconciliation agent you would
actually trust is one whose failure modes you have already found yourself.

The bar for every case is the same and it is deliberately low:
  * it must not crash the batch
  * it must not silently invent a match
Producing an exception is a pass. Producing a wrong match is a failure.
"""

import csv

import pandas as pd
import pytest

from src.reconcile import SourceUnavailable, run_reconciliation

LEDGER_COLS = ["ledger_id", "order_id", "customer", "amount", "date", "status"]
STL_COLS = ["settlement_id", "payment_id", "order_id", "gross_amount", "fee", "tax",
            "refund_amount", "settled_amount", "settlement_date", "utr"]
BANK_COLS = ["txn_id", "date", "amount", "narration", "type"]


def led(order_id="order_1", amount=1000.0, date="2026-08-01", status="paid", ledger_id=None):
    return {"ledger_id": ledger_id or f"led_{order_id}", "order_id": order_id,
            "customer": "c1", "amount": amount, "date": date, "status": status}


def stl(order_id="order_1", gross=1000.0, fee=20.0, tax=3.6, refund=0.0,
        settled=976.4, date="2026-08-03", utr="UTR900001", settlement_id="stl_1"):
    return {"settlement_id": settlement_id, "payment_id": "pay_1", "order_id": order_id,
            "gross_amount": gross, "fee": fee, "tax": tax, "refund_amount": refund,
            "settled_amount": settled, "settlement_date": date, "utr": utr}


def bnk(txn_id="bnk_1", date="2026-08-05", amount=976.4, narration=None,
        type_="credit", utr="UTR900001"):
    return {"txn_id": txn_id, "date": date, "amount": amount,
            "narration": narration if narration is not None
            else f"RAZORPAY SETTLEMENT UTR:{utr}", "type": type_}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """Writes an arbitrary three-source batch and runs the pipeline over it."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = tmp_path / "data"
    out = tmp_path / "output"
    data.mkdir()
    out.mkdir()

    def _run(ledger_rows, stl_rows, bank_rows):
        for name, rows, cols in [
            ("internal_ledger", ledger_rows, LEDGER_COLS),
            ("razorpay_settlements", stl_rows, STL_COLS),
            ("bank_statement", bank_rows, BANK_COLS),
        ]:
            with open(data / f"{name}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        return run_reconciliation(data_dir=str(data), output_dir=str(out))[0]

    return _run


def matched_orders(report):
    unreconciled = {e["order_id"] for e in report["exceptions"]}
    return report["reconciled_orders"], unreconciled


# ------------------------------------------------------------ degenerate shapes

def test_empty_batch_does_not_divide_by_zero(build):
    report = build([], [], [])
    assert report["total_orders"] == 0
    assert report["match_rate_pct"] == 0.0
    assert report["reconciled_orders"] == 0


def test_ledger_with_no_settlements_at_all(build):
    report = build([led(f"order_{i}") for i in range(20)], [], [])
    assert report["match_rate_pct"] == 0.0
    assert report["exception_count"] == 20
    assert all(e["reason_code"] == "no_settlement_found" for e in report["exceptions"])


def test_settlements_with_no_ledger_at_all(build):
    """Razorpay reports payouts for orders the merchant has never heard of."""
    report = build([], [stl(order_id=f"order_{i}", settlement_id=f"stl_{i}",
                            utr=f"UTR90000{i}") for i in range(5)], [])
    assert report["total_orders"] == 0
    assert report["match_rate_pct"] == 0.0


# --------------------------------------------------------------- dirty values

def test_malformed_date_does_not_kill_the_batch(build):
    report = build([led()], [stl()], [bnk(date="2026-13-45")])
    assert report["reconciled_orders"] == 0


def test_empty_narration(build):
    report = build([led()], [stl()], [bnk(narration="")])
    assert report["reconciled_orders"] == 0


def test_missing_amount_is_not_read_as_zero_and_matched(build):
    report = build([led()], [stl()], [bnk(amount="")])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled


def test_negative_bank_amount_never_matches_a_positive_settlement(build):
    report = build([led()], [stl()], [bnk(amount=-976.4)])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled


def test_zero_amounts_everywhere(build):
    report = build([led(amount=0.0)],
                   [stl(gross=0.0, fee=0.0, tax=0.0, settled=0.0)],
                   [bnk(amount=0.0)])
    assert report["total_orders"] == 1


def test_enormous_amount_keeps_precision(build):
    big = 999_999_999.99
    report = build([led(amount=big)],
                   [stl(gross=big, fee=0.0, tax=0.0, settled=big)],
                   [bnk(amount=big)])
    assert report["reconciled_orders"] == 1


def test_whitespace_and_case_in_identifiers(build):
    report = build([led(order_id=" order_1 ")], [stl(order_id="order_1")], [bnk()])
    # it may or may not match, but it must never crash or double-count
    assert report["reconciled_orders"] + report["unreconciled_orders"] == \
        report["total_orders"]


def test_lowercase_utr_in_narration_still_resolves(build):
    report = build([led()], [stl()],
                   [bnk(narration="razorpay settlement utr:utr900001")])
    assert report["reconciled_orders"] == 1


# ------------------------------------------------------------- hostile strings

def test_ten_kilobyte_narration(build):
    noise = "PAYMENT REF NOISE " * 600
    report = build([led()], [stl()], [bnk(narration=f"{noise} UTR:UTR900001 {noise}")])
    assert report["reconciled_orders"] == 1


def test_unicode_and_emoji_narration(build):
    report = build([led()], [stl()],
                   [bnk(narration="बैंक जमा 💸 UTR:UTR900001 ✅ /תשלום")])
    assert report["reconciled_orders"] == 1


def test_narration_containing_commas_and_quotes_survives_csv(build):
    report = build([led()], [stl()],
                   [bnk(narration='CR,"RAZORPAY",UTR:UTR900001,"NET, STLMT"')])
    assert report["reconciled_orders"] == 1


def test_narration_that_looks_like_a_formula(build):
    """A narration is data, never something to evaluate."""
    report = build([led()], [stl()], [bnk(narration="=1+1 UTR:UTR900001")])
    assert report["reconciled_orders"] == 1


# ------------------------------------------------------- duplication & identity

def test_duplicate_order_id_in_the_ledger(build):
    report = build([led(ledger_id="led_a"), led(ledger_id="led_b")], [stl()], [bnk()])
    # two ledger rows for one order: whatever the verdict, the arithmetic holds
    assert report["reconciled_orders"] + report["unreconciled_orders"] == \
        report["total_orders"]


def test_duplicate_txn_id_in_the_bank_statement(build):
    """The same credit imported twice must not satisfy a settlement twice over."""
    report = build([led()], [stl()],
                   [bnk(txn_id="bnk_1"), bnk(txn_id="bnk_1")])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled  # 2x the money is not a match


def test_a_debit_row_must_not_satisfy_a_settlement(build):
    """
    A real bank statement has debits. A chargeback quoting the settlement's UTR
    is money leaving, and must never be read as the settlement arriving.
    """
    report = build([led()], [stl()], [bnk(type_="debit")])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled


def test_two_unrelated_settlements_sharing_a_utr(build):
    """Not a batch -- a data integrity problem. Must not silently half-match."""
    report = build(
        [led("order_1"), led("order_2")],
        [stl(order_id="order_1", settlement_id="stl_1", utr="UTRSAME"),
         stl(order_id="order_2", settlement_id="stl_2", utr="UTRSAME")],
        [bnk(amount=976.4, narration="RAZORPAY UTR:UTRSAME")])
    reconciled, _ = matched_orders(report)
    assert reconciled == 0  # the credit covers one of them, not both


# --------------------------------------------------------------- source damage

def test_ledger_missing_required_column_fails_loudly(build, tmp_path):
    data = tmp_path / "data2"
    data.mkdir()
    (data / "internal_ledger.csv").write_text("order_id\norder_1\n")
    (data / "razorpay_settlements.csv").write_text("settlement_id\nstl_1\n")
    with pytest.raises(SourceUnavailable):
        run_reconciliation(data_dir=str(data), output_dir=str(tmp_path))


def test_completely_empty_csv_files(build, tmp_path):
    data = tmp_path / "data3"
    data.mkdir()
    for name in ("internal_ledger", "razorpay_settlements", "bank_statement"):
        (data / f"{name}.csv").write_text("")
    with pytest.raises(SourceUnavailable):
        run_reconciliation(data_dir=str(data), output_dir=str(tmp_path))


# ------------------------------------------------------- round two: harder

def test_a_credit_dated_before_the_settlement_is_impossible(build):
    """
    The bank cannot pay out a settlement before Razorpay made it. A negative
    gap is a data-integrity problem, not an unusually fast payment.
    """
    report = build([led()], [stl(date="2026-08-20")], [bnk(date="2026-08-01")])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled


def test_every_settlement_sharing_one_utr(build):
    """Pathological: 50 unrelated settlements all quoting the same reference."""
    ledger = [led(f"order_{i}") for i in range(50)]
    settlements = [stl(order_id=f"order_{i}", settlement_id=f"stl_{i}", utr="UTRSAME")
                   for i in range(50)]
    report = build(ledger, settlements, [bnk(narration="RAZORPAY UTR:UTRSAME")])
    assert report["total_orders"] == 50
    assert report["reconciled_orders"] + report["unreconciled_orders"] == 50


def test_absurdly_long_utr(build):
    long_utr = "UTR" + "9" * 1000
    report = build([led()], [stl(utr=long_utr)],
                   [bnk(narration=f"RAZORPAY {long_utr}")])
    assert report["reconciled_orders"] == 1


def test_narration_is_nothing_but_the_utr(build):
    report = build([led()], [stl()], [bnk(narration="UTR900001")])
    assert report["reconciled_orders"] == 1


def test_utr_that_is_purely_numeric(build):
    report = build([led()], [stl(utr="900001")], [bnk(narration="NEFT CR 900001 AUG")])
    assert report["reconciled_orders"] == 1


def test_settlement_amount_as_a_string_with_currency_symbol(build):
    """A CSV exported by a spreadsheet may carry formatted numbers."""
    report = build([led()], [stl(settled="₹976.40")], [bnk()])
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled  # unreadable, so an exception -- not a match


def test_ten_thousand_orders_stays_linear(build):
    ledger = [led(f"order_{i}") for i in range(10_000)]
    settlements = [stl(order_id=f"order_{i}", settlement_id=f"stl_{i}",
                       utr=f"UTR{i:07d}") for i in range(10_000)]
    bank_rows = [bnk(txn_id=f"bnk_{i}", narration=f"RAZORPAY UTR:UTR{i:07d}")
                 for i in range(10_000)]
    report = build(ledger, settlements, bank_rows)
    assert report["reconciled_orders"] == 10_000
    assert report["throughput"]["records_per_second"] > 1_000


def test_bank_statement_of_pure_noise(build):
    """500 credits, none of which relate to anything we settled."""
    noise = [bnk(txn_id=f"bnk_{i}", amount=100.0 + i,
                 narration=f"UPI/P2P/random {i}") for i in range(500)]
    report = build([led()], [stl()], noise)
    _, unreconciled = matched_orders(report)
    assert "order_1" in unreconciled
    assert report["reconciled_orders"] == 0
