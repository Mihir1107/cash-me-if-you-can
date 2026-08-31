"""
The primary batch runs at ~56% faults, which is a fixture choice and distorts
everything a controller would read off it. The question that choice invites is:
what does this look like on a normal week?

So this runs the same code, same injection rules, at a realistic ~3% density,
and pins the two things that would sink the product if they were false:

  * nothing gets missed when faults are rare (the easy failure: a detector
    tuned on dense data losing its grip on sparse data)
  * the controller is not drowned (60 exception rows must not arrive as 60
    separate things to do)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.generate_synthetic as gen
from src.evaluate import run_evaluation
from src.reconcile import run_reconciliation

ORDERS = 2000
MODULUS = 222


@pytest.fixture(scope="module")
def realistic(tmp_path_factory):
    out = tmp_path_factory.mktemp("realistic")
    original = (gen.N_ORDERS, gen.FAULT_MODULUS)
    gen.N_ORDERS, gen.FAULT_MODULUS = ORDERS, MODULUS
    try:
        ledger, settlements, bank, truth = gen.make_dataset()
    finally:
        gen.N_ORDERS, gen.FAULT_MODULUS = original

    gen.write_csv(out / "internal_ledger.csv", ledger,
                  ["ledger_id", "order_id", "customer", "amount", "date", "status"])
    gen.write_csv(out / "razorpay_settlements.csv", settlements,
                  ["settlement_id", "payment_id", "order_id", "gross_amount", "fee",
                   "tax", "refund_amount", "settled_amount", "settlement_date", "utr"])
    gen.write_csv(out / "bank_statement.csv", bank,
                  ["txn_id", "date", "amount", "narration", "type"])
    gen.write_csv(out / "ground_truth.csv", truth,
                  ["order_id", "expected_reason_code", "note"])

    # run_evaluation returns the evaluation payload, not the run report, so the
    # report is fetched separately. Same inputs, same deterministic pipeline.
    _, result, _ = run_evaluation(
        data_dir=str(out), output_dir=str(tmp_path_factory.mktemp("out")))
    report, _ = run_reconciliation(
        data_dir=str(out), output_dir=str(tmp_path_factory.mktemp("run")))
    return report, result, truth


def test_the_batch_is_actually_sparse(realistic):
    """If this drifts back toward the dense fixture, the rest proves nothing."""
    _, _, truth = realistic
    faults = sum(1 for row in truth if row["expected_reason_code"] != "matched")
    density = faults / len(truth)
    assert 0.02 <= density <= 0.05, f"density {density:.1%} is not realistic"


def test_nothing_is_missed_when_faults_are_rare(realistic):
    """
    The failure mode worth fearing: a detector that looks perfect on a batch
    where half the orders are broken, and loses faults in the noise when they
    are rare. Recall must not depend on density.
    """
    _, result, _ = realistic
    fd = result["fault_detection"]
    assert fd["missed"] == 0
    assert fd["detected"] == fd["injected_faults"]


def test_the_controller_is_not_drowned(realistic):
    """
    2,000 orders is a real month. If that arrives as dozens of separate things
    to do, the triage layer has not earned its place.
    """
    report, _, _ = realistic
    triage = report["triage"]
    assert triage["exception_rows"] > 20, "too clean to be testing anything"
    assert triage["incident_count"] <= 15
    assert triage["material_incident_count"] <= 5


def test_the_money_identity_holds_at_realistic_density(realistic):
    report, _, _ = realistic
    assert report["money"]["identity"]["holds"]
