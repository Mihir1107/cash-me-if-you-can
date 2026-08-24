"""
Property-based tests. Hand-written cases prove the pipeline handles the
situations I thought of; these prove invariants hold across situations I did
not.

Hypothesis generates whole three-source batches -- arbitrary amounts, fees,
dates, narrations, duplicates, orphans, missing rows -- and each test asserts a
property that must hold for *every* batch. When one fails, hypothesis shrinks
the input to the smallest batch that still breaks it.

The four invariants here are the ones that make the report trustworthy:

  1. every order gets exactly one verdict          (nothing vanishes)
  2. reconciled + unreconciled == total            (the count balances)
  3. total exposure == confirmed + at risk         (the money balances)
  4. a match always has bank confirmation          (nothing is invented)

Number 4 is the one worth the whole file. It is the property the entire
architecture exists to guarantee, and it is stated here as something the
pipeline must satisfy for any input at all -- not something I checked on the
batch I happened to write.
"""

import csv

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.reconcile import run_reconciliation

LEDGER_COLS = ["ledger_id", "order_id", "customer", "amount", "date", "status"]
STL_COLS = ["settlement_id", "payment_id", "order_id", "gross_amount", "fee", "tax",
            "refund_amount", "settled_amount", "settlement_date", "utr"]
BANK_COLS = ["txn_id", "date", "amount", "narration", "type"]

money = st.floats(min_value=0.0, max_value=5_000_000.0, allow_nan=False,
                  allow_infinity=False).map(lambda v: round(v, 2))
dates = st.dates(min_value=__import__("datetime").date(2026, 1, 1),
                 max_value=__import__("datetime").date(2026, 12, 31)
                 ).map(lambda d: d.isoformat())


@st.composite
def batches(draw):
    """
    A whole synthetic batch: n orders, each independently given (or denied) a
    settlement and a bank credit, with amounts free to disagree.
    """
    n = draw(st.integers(min_value=0, max_value=12))
    ledger, settlements, bank = [], [], []

    for i in range(n):
        oid = f"order_{i:04d}"
        gross = draw(money)
        fee = draw(st.floats(min_value=0.0, max_value=gross or 1.0,
                             allow_nan=False, allow_infinity=False).map(
                                 lambda v: round(v, 2)))
        tax = round(fee * 0.18, 2)
        refund = draw(st.sampled_from([0.0, round(gross * 0.3, 2)]))
        order_date = draw(dates)

        ledger.append({
            "ledger_id": f"led_{i}", "order_id": oid, "customer": f"c{i}",
            "amount": draw(st.sampled_from([gross, round(gross - refund, 2)])),
            "date": order_date,
            "status": draw(st.sampled_from(["paid", "partially_refunded"])),
        })

        if not draw(st.booleans()):
            continue  # orphan: booked but never settled

        utr = f"UTR{900000 + i}"
        settled = draw(st.sampled_from([
            round(gross - fee - tax - refund, 2),   # correct footing
            round(gross - fee - tax - refund - draw(money) / 1000, 2),  # wrong
        ]))
        # duplicated settlement rows are a real Razorpay-side glitch
        for copy in range(draw(st.integers(min_value=1, max_value=2))):
            settlements.append({
                "settlement_id": f"stl_{i}_{copy}", "payment_id": f"pay_{i}",
                "order_id": oid, "gross_amount": gross, "fee": fee, "tax": tax,
                "refund_amount": refund, "settled_amount": settled,
                "settlement_date": order_date, "utr": utr,
            })

        if not draw(st.booleans()):
            continue  # settled, but nothing ever hit the bank

        bank.append({
            "txn_id": f"bnk_{i}", "date": draw(dates),
            "amount": draw(st.sampled_from([settled, draw(money)])),
            "narration": draw(st.sampled_from([
                f"RAZORPAY SETTLEMENT UTR:{utr}",
                f"NEFT-RZPY-{utr[-6:]}/settlemnt",
                "CR/ONLINE TRF/no ref quoted",
            ])),
            "type": draw(st.sampled_from(["credit", "credit", "debit"])),
        })

    return ledger, settlements, bank


def write_batch(tmp_path, ledger, settlements, bank):
    data = tmp_path / "data"
    out = tmp_path / "output"
    data.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    for name, rows, cols in [("internal_ledger", ledger, LEDGER_COLS),
                             ("razorpay_settlements", settlements, STL_COLS),
                             ("bank_statement", bank, BANK_COLS)]:
        with open(data / f"{name}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return data, out


SETTINGS = settings(max_examples=150, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Properties are about the deterministic core; never call an API here."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@SETTINGS
@given(batch=batches())
def test_every_order_gets_exactly_one_verdict(tmp_path_factory, batch):
    data, out = write_batch(tmp_path_factory.mktemp("b"), *batch)
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    ordered = {r["order_id"] for r in batch[0]}
    excepted = {e["order_id"] for e in report["exceptions"]}

    # nothing is invented, and nothing vanishes
    assert excepted <= ordered
    assert report["reconciled_orders"] + report["unreconciled_orders"] == len(ordered)
    assert report["total_orders"] == len(ordered)


@SETTINGS
@given(batch=batches())
def test_money_identity_always_holds(tmp_path_factory, batch):
    """Every rupee of exposure sits in exactly one bucket, for any batch."""
    data, out = write_batch(tmp_path_factory.mktemp("b"), *batch)
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    money = report["money"]
    assert money["identity"]["holds"], money["identity"]
    assert money["confirmed_value"] >= 0
    assert money["at_risk_value"] >= 0
    assert money["confirmed_value"] <= money["total_exposure"] + 0.01


@SETTINGS
@given(batch=batches())
def test_a_match_always_has_bank_confirmation(tmp_path_factory, batch):
    """
    THE invariant. An order may only be counted as reconciled if Stage B
    confirmed a bank credit for it. No amount of coincidence in the generated
    data may produce a match that no bank row supports.
    """
    data, out = write_batch(tmp_path_factory.mktemp("b"), *batch)
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    if report["reconciled_orders"] == 0:
        return

    # a reconciled order is never also in the exception list
    excepted = {e["order_id"] for e in report["exceptions"]}
    assert report["reconciled_orders"] == report["total_orders"] - len(excepted)

    # and both stages must have signed off
    assert report["stages"]["settlement_bank"]["matched"] >= report["reconciled_orders"]
    assert report["stages"]["ledger_settlement"]["matched"] >= report["reconciled_orders"]


@SETTINGS
@given(batch=batches())
def test_no_api_key_means_the_model_resolves_nothing(tmp_path_factory, batch):
    data, out = write_batch(tmp_path_factory.mktemp("b"), *batch)
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert report["narration_resolution"]["resolved_by_llm"] == 0
    assert report["throughput"]["llm_calls"] == 0


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(batch=batches())
def test_reconciliation_is_deterministic(tmp_path_factory, batch):
    """Same inputs, same verdicts. A finance control that drifts is worthless."""
    data, out = write_batch(tmp_path_factory.mktemp("b"), *batch)

    first, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    second, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert first["match_rate_pct"] == second["match_rate_pct"]
    assert first["exception_reason_counts"] == second["exception_reason_counts"]
    assert first["money"]["at_risk_by_reason"] == second["money"]["at_risk_by_reason"]
    assert ([(e["order_id"], e["reason_code"]) for e in first["exceptions"]]
            == [(e["order_id"], e["reason_code"]) for e in second["exceptions"]])
