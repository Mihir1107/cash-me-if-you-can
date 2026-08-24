"""
Tests for the printed output.

This is the least glamorous file here and close to the most important. The
summary and evaluation printers were the least-covered code in the project
while being the only code guaranteed to run during a live demo. A traceback in
print_summary would not corrupt a single number, and would still be the worst
possible failure, because it happens in front of an audience.

So: every printer runs against the shapes that actually occur, including the
degraded ones, and the figures a viewer reads off the screen are asserted to be
the figures in the report.
"""

import io
import json
from contextlib import redirect_stdout

import pandas as pd
import pytest

from src.evaluate import print_evaluation, score
from src.matcher import MatchResult
from src.report import build_report, print_summary
from src.money import build_exposure_index


def render(report):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print_summary(report)
    return buffer.getvalue()


def results(stage, verdicts):
    return [MatchResult(order_id=oid, stage=stage, status=status,
                        reason_code=("" if status == "matched" else reason))
            for oid, status, reason in verdicts]


def make_report(verdicts_a, verdicts_b, total=None, exposure=None, **kwargs):
    a = results("ledger_settlement", verdicts_a)
    b = results("settlement_bank", verdicts_b)
    return build_report(
        total_orders=total if total is not None else len(verdicts_a),
        stage_a_results=a, stage_b_results=b,
        fuzzy_links=[], llm_links=[], llm_exceptions=[],
        exposure_index=exposure, **kwargs)


# --------------------------------------------------------------- summary

def test_summary_renders_a_normal_run():
    report = make_report(
        [("order_1", "matched", ""), ("order_2", "exception", "fee_footing_mismatch")],
        [("order_1", "matched", "")],
        exposure={"order_1": {"exposure": 900.0, "basis": "settled_amount"},
                  "order_2": {"exposure": 100.0, "basis": "settled_amount"}})
    out = render(report)

    assert "MATCH RATE: 50.0%" in out
    assert "fee_footing_mismatch" in out
    assert "MONEY RECONCILED" in out
    assert "[OK]" in out


def test_summary_survives_an_empty_batch():
    """No orders means no division, and certainly no traceback."""
    out = render(make_report([], [], total=0, exposure={}))
    assert "MATCH RATE: 0.0%" in out
    assert "0/0" in out


def test_summary_survives_a_run_with_no_money_section():
    """exposure_index is optional; the printer must not assume it."""
    out = render(make_report([("order_1", "matched", "")],
                             [("order_1", "matched", "")]))
    assert "MATCH RATE: 100.0%" in out
    assert "MONEY RECONCILED" not in out


def test_summary_announces_a_degraded_bank_source():
    """The degradation demo prints this. It must be unmissable and must render."""
    report = make_report(
        [("order_1", "matched", "")],
        [("order_1", "exception", "bank_source_unavailable")],
        bank_error="bank_statement.csv could not be read")
    out = render(report)

    assert "DEGRADED" in out
    assert "bank_statement.csv could not be read" in out
    assert "Stage A results below are still valid" in out


def test_summary_flags_when_an_order_fails_both_legs():
    """
    One order, two exception records. The numbers legitimately differ and a
    viewer will notice, so the printer says why instead of leaving it dangling.
    """
    report = make_report(
        [("order_1", "exception", "fee_footing_mismatch")],
        [("order_1", "exception", "bank_source_unavailable")])
    out = render(report)

    assert "UNRECONCILED ORDERS: 1" in out
    assert "failed both legs" in out


def test_summary_never_claims_a_broken_identity_holds():
    """
    If the money arithmetic does not balance, the report is lying about where
    money went. That has to be loud, not a quietly wrong [OK].
    """
    report = make_report(
        [("order_1", "matched", "")], [("order_1", "matched", "")],
        exposure={"order_1": {"exposure": 100.0, "basis": "x"}})
    report["money"]["identity"] = {"holds": False, "residual": -42.5,
                                   "statement": "total == confirmed + at_risk"}
    out = render(report)

    assert "IDENTITY BROKEN" in out
    assert "-42.50" in out
    assert "cannot be trusted" in out


def test_printed_figures_match_the_report():
    """A viewer reads these off the screen; they must be the real numbers."""
    report = make_report(
        [("order_1", "matched", ""), ("order_2", "exception", "no_settlement_found")],
        [("order_1", "matched", "")],
        exposure={"order_1": {"exposure": 750.0, "basis": "settled_amount"},
                  "order_2": {"exposure": 250.0, "basis": "ledger amount"}})
    out = render(report)

    assert str(report["match_rate_pct"]) in out
    assert f"{report['money']['total_exposure']:,.2f}" in out
    assert f"{report['money']['confirmed_value']:,.2f}" in out


# ------------------------------------------------------------ evaluation

def truth(**labels):
    return pd.DataFrame([{"order_id": oid, "expected_reason_code": code, "note": ""}
                         for oid, code in labels.items()])


def render_evaluation(result, ablation):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print_evaluation(result, ablation)
    return buffer.getvalue()


def test_evaluation_renders_and_names_every_misclassified_order():
    """No filtering, no 'and 3 more'. Every miss is on screen."""
    report = {"exceptions": [
        {"order_id": "order_2", "stage": "ledger_settlement",
         "reason_code": "fee_footing_mismatch", "basis": "", "detail": {}}]}
    result = score(truth(order_1="matched", order_2="refund_not_reflected"), report)

    out = render_evaluation(result, [{"config": "regex only", "match_rate_pct": 50.0,
                                      "reconciled_orders": 1, "resolved_by_fuzzy": 0,
                                      "resolved_by_llm": 0, "unresolved_narrations": 0}])
    assert "order_2" in out
    assert "refund_not_reflected" in out
    assert "fee_footing_mismatch" in out


def test_evaluation_renders_a_skipped_ablation_row_honestly():
    """The printed table must never imply a tier that did not run."""
    result = score(truth(order_1="matched"), {"exceptions": []})
    out = render_evaluation(result, [
        {"config": "regex only", "match_rate_pct": 100.0, "reconciled_orders": 1,
         "resolved_by_fuzzy": 0, "resolved_by_llm": 0, "unresolved_narrations": 0},
        {"config": "+ LLM tier", "skipped": "no OPENAI_API_KEY set"},
    ])
    assert "no OPENAI_API_KEY set" in out
    assert "100.00%" in out


def test_evaluation_renders_a_perfect_run_without_a_misclassified_section():
    result = score(truth(order_1="matched"), {"exceptions": []})
    out = render_evaluation(result, [])
    assert "Misclassified" not in out
    assert "100.00%" in out


def test_summary_renders_throughput_and_the_slowest_stages():
    """The throughput line answers a third of the judging bar. It must render."""
    report = make_report([("order_1", "matched", "")], [("order_1", "matched", "")])
    report["throughput"] = {
        "wall_clock_ms": 11.2, "records_per_second": 9091.0,
        "records_processed": 102, "llm_calls": 2, "llm_calls_per_100_orders": 3.64,
        "stage_ms": {"stage_b_settlement_bank": 2.4, "load_sources": 1.1,
                     "stage_a_ledger_settlement": 1.6, "tier1_narration_regex": 0.8},
    }
    out = render(report)

    assert "11.2 ms" in out
    assert "9,091 records/sec" in out
    assert "LLM calls made:                    2" in out
    # only the three slowest, highest first
    assert "stage_b_settlement_bank 2.4ms" in out
    assert "tier1_narration_regex" not in out


def test_summary_lists_unattributed_credits_with_their_narrations():
    """
    Cash the pipeline holds but cannot place. A controller needs the txn id and
    the narration to go find it, so both are printed, not just a count.
    """
    report = make_report(
        [("order_1", "matched", "")], [("order_1", "matched", "")],
        exposure={"order_1": {"exposure": 500.0, "basis": "settled_amount"}})
    credit = {"txn_id": "bnk_000020", "amount": 21669.80,
              "narration": "CR/ONLINE TRF/paymnt gateway aug batch/no ref quoted",
              "reason_code": "narration_unresolved", "basis": "no reference"}
    report["unattributed_bank_credits"] = [credit]
    report["money"]["unattributed_bank_credit_value"] = 21669.80
    out = render(report)

    assert "Unattributed bank credits: 1" in out
    assert "bnk_000020" in out
    assert "21,669.80" in out
    assert "no ref quoted" in out
    assert "cash held, not placeable" in out
