"""
End-to-end tests on the orchestrator and the report.

Two properties are pinned here because getting either wrong makes the headline
number dishonest rather than merely wrong:
  * an order counts as matched only if BOTH legs confirmed it
  * losing the bank statement degrades Stage B without inflating the match rate
"""

import json
import shutil

import pandas as pd
import pytest

from src.reconcile import SourceUnavailable, run_reconciliation
from src.report import build_report
from src.matcher import MatchResult


@pytest.fixture
def workspace(tmp_path):
    """A private copy of the real synthetic batch, so tests can mutate sources."""
    data = tmp_path / "data"
    out = tmp_path / "output"
    shutil.copytree("data", data)
    out.mkdir()
    return data, out


# ------------------------------------------------------- report arithmetic

def _results(stage, verdicts):
    return [MatchResult(order_id=oid, stage=stage, status=status,
                        reason_code=("" if status == "matched" else "some_reason"))
            for oid, status in verdicts]


def test_match_rate_counts_only_orders_confirmed_on_both_legs():
    """
    order_2 passes the ledger leg but the bank never confirmed it. It must not
    count toward the match rate -- the bug this report was rewritten to fix.
    """
    report = build_report(
        total_orders=3,
        stage_a_results=_results("ledger_settlement", [
            ("order_1", "matched"), ("order_2", "matched"), ("order_3", "exception")]),
        stage_b_results=_results("settlement_bank", [
            ("order_1", "matched"), ("order_2", "exception")]),
        fuzzy_links=[], llm_links=[], llm_exceptions=[],
    )
    assert report["reconciled_orders"] == 1
    assert report["match_rate_pct"] == round(100 / 3, 2)
    assert report["unreconciled_orders"] == 2


def test_reconciled_plus_unreconciled_always_equals_total():
    report = build_report(
        total_orders=3,
        stage_a_results=_results("ledger_settlement", [
            ("order_1", "matched"), ("order_2", "matched"), ("order_3", "exception")]),
        stage_b_results=_results("settlement_bank", [
            ("order_1", "matched"), ("order_2", "exception")]),
        fuzzy_links=[], llm_links=[], llm_exceptions=[],
    )
    assert report["reconciled_orders"] + report["unreconciled_orders"] == 3


def test_an_order_failing_only_the_bank_leg_is_marked_as_such():
    report = build_report(
        total_orders=1,
        stage_a_results=_results("ledger_settlement", [("order_1", "matched")]),
        stage_b_results=_results("settlement_bank", [("order_1", "exception")]),
        fuzzy_links=[], llm_links=[], llm_exceptions=[],
    )
    assert report["exceptions"][0]["also_matched_in_stage_a"] is True


def test_unattributed_credits_are_not_counted_as_order_exceptions():
    report = build_report(
        total_orders=1,
        stage_a_results=_results("ledger_settlement", [("order_1", "matched")]),
        stage_b_results=_results("settlement_bank", [("order_1", "matched")]),
        fuzzy_links=[], llm_links=[],
        llm_exceptions=[{
            "bank_row": pd.Series({"txn_id": "bnk_9", "amount": 100.0,
                                   "narration": "CR/ONLINE TRF"}),
            "reason_code": "narration_unresolved", "basis": "no reference",
        }],
    )
    assert report["exception_count"] == 0
    assert report["match_rate_pct"] == 100.0
    assert len(report["unattributed_bank_credits"]) == 1


# ------------------------------------------------------------ end to end

def test_full_batch_runs_and_reports_honestly(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, out = workspace
    report, audit = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert report["total_orders"] == 55  # the brief asks for a 50+ record batch
    assert report["reconciled_orders"] + report["unreconciled_orders"] == 55
    assert 0 < report["match_rate_pct"] < 100  # neither a fake 100% nor a dead run

    # every injected failure mode reaches the exception list under its own code
    assert set(report["exception_reason_counts"]) == {
        "refund_not_reflected", "duplicate_settlement", "no_settlement_found",
        "fee_footing_mismatch", "bank_credit_delayed", "settlement_not_credited",
        "credit_unattributed",
    }

    # the two genuinely missing payouts and the two unreadable-narration credits
    # must not be collapsed into one reason code
    assert report["exception_reason_counts"]["settlement_not_credited"] == 2
    assert report["exception_reason_counts"]["credit_unattributed"] == 2

    # the fuzzy tier does real work, and with no key the LLM tier resolves nothing
    assert report["narration_resolution"]["resolved_by_fuzzy_no_llm"] > 0
    assert report["narration_resolution"]["resolved_by_llm"] == 0
    assert report["narration_resolution"]["unresolved"] > 0

    assert (out / "reconciliation_report.json").exists()
    lines = (out / "audit_trail.jsonl").read_text().strip().splitlines()
    assert len(lines) == len(audit.entries)
    for line in lines:  # every entry is a real, timestamped, attributed decision
        entry = json.loads(line)
        assert entry["timestamp"] and entry["stage"] and entry["basis"]


def test_every_order_gets_a_verdict(workspace, monkeypatch):
    """No order may vanish: matched or excepted, never silently dropped."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    ledger = pd.read_csv(data / "internal_ledger.csv")
    excepted = {e["order_id"] for e in report["exceptions"]}
    assert len(excepted) == report["unreconciled_orders"]
    assert excepted <= set(ledger["order_id"])


def test_missing_bank_statement_degrades_instead_of_crashing(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, out = workspace
    (data / "bank_statement.csv").unlink()

    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert report["bank_source_error"] is not None
    assert report["stages"]["ledger_settlement"]["matched"] > 0   # Stage A survives
    assert report["stages"]["settlement_bank"]["matched"] == 0
    # nothing may be called reconciled when the bank could not be read
    assert report["match_rate_pct"] == 0.0
    assert all(e["reason_code"] == "bank_source_unavailable"
               for e in report["exceptions"]
               if e["stage"] == "settlement_bank")


def test_structurally_broken_bank_statement_also_degrades(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, out = workspace
    (data / "bank_statement.csv").write_text("nonsense,columns\n1,2\n")

    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    assert "missing required columns" in report["bank_source_error"]
    assert report["match_rate_pct"] == 0.0


def test_missing_backbone_source_fails_loudly(workspace):
    """
    The ledger is not degradable: without it there is nothing to reconcile, and
    a match rate computed over an empty batch would be a lie, not a degradation.
    """
    data, out = workspace
    (data / "internal_ledger.csv").unlink()
    with pytest.raises(SourceUnavailable):
        run_reconciliation(data_dir=str(data), output_dir=str(out))


def test_llm_tier_recovers_the_free_text_narrations(workspace, monkeypatch):
    """
    Stubs the model so the LLM tier's contribution is a measured number rather
    than an assumption. The two free-text credits are genuinely fine money --
    resolving them should remove both false positives and raise the match rate.
    """
    import json as _json
    from types import SimpleNamespace

    from src import llm_resolver

    data, out = workspace
    bank = pd.read_csv(data / "bank_statement.csv")
    settlements = pd.read_csv(data / "razorpay_settlements.csv")

    # map each free-text credit back to the settlement it really belongs to,
    # then have the stubbed model "read" that reference out of the narration
    by_amount = {round(float(r["settled_amount"]), 2): r["utr"]
                 for _, r in settlements.iterrows()}
    free_text = bank[bank["narration"].str.contains("no ref quoted")]
    answers = {row["narration"] + str(row["txn_id"]): by_amount[round(float(row["amount"]), 2)]
               for _, row in free_text.iterrows()}
    assert len(answers) == 2

    pending = list(answers.values())

    class FakeMessages:
        def create(self, **kwargs):
            utr = pending.pop(0)
            return SimpleNamespace(content=[SimpleNamespace(
                type="text",
                text=_json.dumps({"utr_candidate": utr, "order_id_candidate": None,
                                  "confidence": 0.9}))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.anthropic, "Anthropic", FakeClient)

    baseline, _ = run_reconciliation(data_dir=str(data), output_dir=str(out),
                                     enable_llm=False)
    with_llm, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert with_llm["narration_resolution"]["resolved_by_llm"] == 2
    assert with_llm["narration_resolution"]["unresolved"] == 0
    assert with_llm["reconciled_orders"] == baseline["reconciled_orders"] + 2
    assert with_llm["match_rate_pct"] > baseline["match_rate_pct"]
    assert with_llm["unattributed_bank_credits"] == []


def test_throughput_is_measured_and_reports_zero_llm_calls_without_a_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    tp = report["throughput"]
    assert tp["orders"] == 55
    assert tp["records_processed"] == tp["orders"] + tp["bank_rows"]
    assert tp["wall_clock_ms"] > 0
    assert tp["records_per_second"] > 0
    # rows were offered to the LLM tier, but with no key nothing was ever called
    assert report["narration_resolution"]["unresolved"] == 2
    assert tp["llm_calls"] == 0
    assert tp["llm_calls_per_100_orders"] == 0.0
    assert set(tp["stage_ms"]) >= {"stage_a_ledger_settlement", "stage_b_settlement_bank"}


def test_llm_calls_are_counted_when_the_tier_actually_runs(workspace, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from src import llm_resolver

    data, out = workspace

    class FakeMessages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(
                type="text",
                text=_json.dumps({"utr_candidate": None, "order_id_candidate": None,
                                  "confidence": 0.0}))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.anthropic, "Anthropic", FakeClient)

    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    # the model resolved nothing, but it was genuinely called twice
    assert report["throughput"]["llm_calls"] == 2
    assert report["narration_resolution"]["resolved_by_llm"] == 0
