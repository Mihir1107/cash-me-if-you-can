"""
The evaluator is a scored artifact in its own right -- if it flatters the
pipeline, every number in the README is worthless. These tests check it reports
failure correctly, not just success.
"""

import shutil

import pandas as pd
import pytest

from src.evaluate import predicted_labels, run_evaluation, score


def truth(**labels):
    return pd.DataFrame([{"order_id": oid, "expected_reason_code": code, "note": ""}
                         for oid, code in labels.items()])


def report(exceptions):
    return {"exceptions": [
        {"order_id": oid, "stage": stage, "reason_code": code, "basis": "", "detail": {}}
        for oid, stage, code in exceptions]}


@pytest.fixture
def workspace(tmp_path):
    data = tmp_path / "data"
    out = tmp_path / "output"
    shutil.copytree("data", data)
    out.mkdir()
    return data, out


def test_perfect_run_scores_perfectly():
    result = score(
        truth(order_1="matched", order_2="fee_footing_mismatch"),
        report([("order_2", "ledger_settlement", "fee_footing_mismatch")]))
    assert result["exact_label_accuracy"] == 1.0
    assert result["fault_detection"] == {
        "injected_faults": 1, "detected": 1, "false_positives": 0, "missed": 0,
        "precision": 1.0, "recall": 1.0,
    }
    assert result["misclassified"] == []


def test_a_missed_fault_is_reported_as_a_false_negative():
    result = score(
        truth(order_1="fee_footing_mismatch"),
        report([]))  # pipeline saw nothing wrong
    assert result["fault_detection"]["missed"] == 1
    assert result["fault_detection"]["recall"] == 0.0
    assert result["misclassified"][0]["expected"] == "fee_footing_mismatch"
    assert result["misclassified"][0]["predicted"] == "matched"


def test_flagging_a_healthy_order_is_reported_as_a_false_positive():
    result = score(
        truth(order_1="matched"),
        report([("order_1", "settlement_bank", "settlement_not_credited")]))
    assert result["fault_detection"]["false_positives"] == 1
    assert result["fault_detection"]["precision"] == 0.0
    assert result["per_reason_code"]["settlement_not_credited"]["false_positives"] == 1


def test_right_detection_wrong_reason_code_is_not_scored_as_correct():
    """Catching a problem but mislabelling its cause must cost accuracy."""
    result = score(
        truth(order_1="refund_not_reflected"),
        report([("order_1", "ledger_settlement", "fee_footing_mismatch")]))
    assert result["fault_detection"]["detected"] == 1      # noticed something
    assert result["exact_label_accuracy"] == 0.0           # but named it wrong
    assert result["per_reason_code"]["refund_not_reflected"]["recall"] == 0.0


def test_root_cause_wins_when_an_order_fails_both_legs():
    labels = predicted_labels(report([
        ("order_1", "settlement_bank", "bank_amount_mismatch"),
        ("order_1", "ledger_settlement", "fee_footing_mismatch"),
    ]))
    assert labels["order_1"] == "fee_footing_mismatch"


def test_evaluation_of_the_real_batch_misses_nothing(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    payload, result, ablation_rows = run_evaluation(str(data), str(out))

    # every injected fault is caught; the only errors are the two free-text
    # credits the LLM tier would resolve, and they are reported as such
    assert result["fault_detection"]["missed"] == 0
    assert result["fault_detection"]["recall"] == 1.0
    assert result["fault_detection"]["false_positives"] == 3
    assert len(result["misclassified"]) == 3
    assert all(m["expected"] == "matched" for m in result["misclassified"])

    assert (out / "evaluation.json").exists()
    assert payload["match_rate_pct"] == 41.82  # keyless: LLM tier resolves nothing


def test_ablation_shows_the_fuzzy_tier_earning_its_place(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    _, _, rows = run_evaluation(str(data), str(out))

    regex_only = rows[0]
    with_fuzzy = rows[1]
    assert with_fuzzy["match_rate_pct"] > regex_only["match_rate_pct"]
    assert with_fuzzy["resolved_by_fuzzy"] == 5
    assert with_fuzzy["resolved_by_llm"] == 0        # zero API calls to get there
    assert with_fuzzy["unresolved_narrations"] < regex_only["unresolved_narrations"]
    assert with_fuzzy["unresolved_narrations"] == 3
    assert rows[2]["skipped"]                        # LLM row honestly skipped
