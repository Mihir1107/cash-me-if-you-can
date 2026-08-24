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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    report, audit = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert report["total_orders"] == 55  # the brief asks for a 50+ record batch
    assert report["reconciled_orders"] + report["unreconciled_orders"] == 55
    assert 0 < report["match_rate_pct"] < 100  # neither a fake 100% nor a dead run

    # every injected failure mode reaches the exception list under its own code
    # every reason code the pipeline can emit for an order is exercised by the
    # batch -- no code exists that the demo cannot demonstrate
    assert set(report["exception_reason_counts"]) == {
        "refund_not_reflected", "duplicate_settlement", "no_settlement_found",
        "fee_footing_mismatch", "bank_credit_delayed", "settlement_not_credited",
        "credit_unattributed", "bank_amount_mismatch", "ledger_gross_amount_mismatch",
    }

    # the two genuinely missing payouts and the two unreadable-narration credits
    # must not be collapsed into one reason code
    assert report["exception_reason_counts"]["settlement_not_credited"] == 2
    # two ambiguous-reference narrations plus one with no reference at all
    assert report["exception_reason_counts"]["credit_unattributed"] == 3

    # the fuzzy tier does real work, and with no key the LLM tier resolves nothing
    assert report["narration_resolution"]["resolved_by_fuzzy_no_llm"] > 0
    assert report["narration_resolution"]["resolved_by_llm"] == 0
    assert report["narration_resolution"]["unresolved"] > 0

    assert (out / "reconciliation_report.json").exists()
    mine = audit.entries_for_run(out / "audit_trail.jsonl")
    assert len(mine) == len(audit.entries)
    for entry in mine:  # every entry is a real, timestamped, attributed decision
        assert entry["timestamp"] and entry["stage"] and entry["basis"]
        assert entry["run_id"] == audit.run_id


def test_every_order_gets_a_verdict(workspace, monkeypatch):
    """No order may vanish: matched or excepted, never silently dropped."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    ledger = pd.read_csv(data / "internal_ledger.csv")
    excepted = {e["order_id"] for e in report["exceptions"]}
    assert len(excepted) == report["unreconciled_orders"]
    assert excepted <= set(ledger["order_id"])


def test_missing_bank_statement_degrades_instead_of_crashing(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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


def test_llm_tier_recovers_the_ambiguous_reference_narrations(workspace, monkeypatch):
    """
    Measures the LLM tier's contribution with a stub that reads the SAME text
    the real model gets, and nothing else.

    An earlier version of this test looked the correct UTR up in the settlement
    table and handed it to the fake model. That passed while asserting a
    capability the real model could not have — the narration it was fed quoted
    no reference at all, so live it correctly returned nulls and resolved zero.
    A stub may only use information present in its own input.
    """
    import re
    from types import SimpleNamespace

    from src import llm_resolver
    from src.llm_resolver import NarrationReading

    data, out = workspace

    class FakeResponses:
        def parse(self, **kwargs):
            narration = kwargs["input"][-1]["content"]
            # exactly what a competent model does here: read the credit ref and
            # ignore the reversal ref. Nothing else is in scope.
            match = re.search(r"CR REF (\S+)", narration)
            return SimpleNamespace(output_parsed=NarrationReading(
                utr_candidate=match.group(1) if match else None,
                order_id_candidate=None,
                confidence=0.9 if match else 0.0))

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.openai, "OpenAI", FakeClient)

    baseline, _ = run_reconciliation(data_dir=str(data), output_dir=str(out),
                                     enable_llm=False)
    with_llm, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert with_llm["narration_resolution"]["resolved_by_llm"] == 2
    assert with_llm["reconciled_orders"] == baseline["reconciled_orders"] + 2
    assert with_llm["match_rate_pct"] > baseline["match_rate_pct"]

    # the narration quoting no reference at all stays unresolved in every
    # configuration -- no tier can invent what the bank never wrote down
    assert with_llm["narration_resolution"]["unresolved"] == 1
    assert len(with_llm["unattributed_bank_credits"]) == 1
    assert "no ref quoted" in with_llm["unattributed_bank_credits"][0]["narration"]


def test_throughput_is_measured_and_reports_zero_llm_calls_without_a_key(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    tp = report["throughput"]
    assert tp["orders"] == 55
    assert tp["records_processed"] == tp["orders"] + tp["bank_rows"]
    assert tp["wall_clock_ms"] > 0
    assert tp["records_per_second"] > 0
    # rows were offered to the LLM tier, but with no key nothing was ever called
    assert report["narration_resolution"]["unresolved"] == 3
    assert tp["llm_calls"] == 0
    assert tp["llm_calls_per_100_orders"] == 0.0
    assert set(tp["stage_ms"]) >= {"stage_a_ledger_settlement", "stage_b_settlement_bank"}


def test_llm_calls_are_counted_when_the_tier_actually_runs(workspace, monkeypatch):
    from types import SimpleNamespace

    from src import llm_resolver
    from src.llm_resolver import NarrationReading

    data, out = workspace

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(output_parsed=NarrationReading(
                utr_candidate=None, order_id_candidate=None, confidence=0.0))

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.openai, "OpenAI", FakeClient)

    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    # the model resolved nothing, but it was genuinely called for every row
    assert report["throughput"]["llm_calls"] == 3
    assert report["narration_resolution"]["resolved_by_llm"] == 0


def test_a_wrong_llm_proposal_cannot_book_money_to_the_wrong_settlement(workspace, monkeypatch):
    """
    The consolidated narrations quote a reversal reference alongside the credit.
    If the model reads the wrong one, its proposal names a real UTR and passes
    verify_candidate -- so the only thing standing between a plausible mistake
    and money booked against the wrong settlement is the deterministic amount
    check. Prove it holds: nothing may become matched off a wrong proposal.
    """
    from types import SimpleNamespace

    from src import llm_resolver
    from src.llm_resolver import NarrationReading

    data, out = workspace

    # deliberately read the DR RVSL reference instead of the CR one
    def wrong_reading(narration):
        reversal = narration.split("DR RVSL REF ")[1].split("/")[0]
        return NarrationReading(utr_candidate=reversal, order_id_candidate=None,
                                confidence=0.95)

    class FakeResponses:
        def parse(self, **kwargs):
            narration = kwargs["input"][1]["content"]
            if "DR RVSL REF " not in narration:
                return SimpleNamespace(output_parsed=NarrationReading(
                    utr_candidate=None, order_id_candidate=None, confidence=0.0))
            return SimpleNamespace(output_parsed=wrong_reading(narration))

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.openai, "OpenAI", FakeClient)

    baseline, _ = run_reconciliation(data_dir=str(data), output_dir=str(out),
                                     enable_llm=False)
    misled, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    # the proposals were accepted as links -- they name real UTRs
    assert misled["narration_resolution"]["resolved_by_llm"] == 2

    # ...and still nothing extra got reconciled, because the amounts disagree
    assert misled["reconciled_orders"] <= baseline["reconciled_orders"]
    assert misled["match_rate_pct"] <= baseline["match_rate_pct"]

    # the mismatch surfaces as an exception rather than a silent bad match
    assert misled["exception_reason_counts"].get("bank_amount_mismatch", 0) > 0


def test_audit_trail_appends_and_never_destroys_an_earlier_run(workspace, monkeypatch):
    """
    An audit trail that truncates on every run is not an audit trail. Two runs
    must both survive in the file, and each must remain separable by run_id.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data, out = workspace
    log = out / "audit_trail.jsonl"

    _, first = run_reconciliation(data_dir=str(data), output_dir=str(out))
    after_first = log.read_text().strip().splitlines()

    _, second = run_reconciliation(data_dir=str(data), output_dir=str(out))
    after_second = log.read_text().strip().splitlines()

    assert first.run_id != second.run_id
    assert len(after_second) == len(after_first) + len(second.entries)

    # the first run's lines are still there, byte for byte
    assert after_second[:len(after_first)] == after_first
    assert len(first.entries_for_run(log)) == len(first.entries)
    assert len(second.entries_for_run(log)) == len(second.entries)
