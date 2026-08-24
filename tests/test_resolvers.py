"""
Tests for the two narration-resolution tiers above the regex matcher.

The load-bearing tests here are the ones asserting what the LLM tier CANNOT do:
it cannot turn a confident-sounding guess into a match, and it cannot match
anything at all when there is no API key.
"""

import pandas as pd

from src import llm_resolver
from src.fuzzy_resolver import fuzzy_resolve_narration
from src.fuzzy_resolver import resolve_unresolved_bank_rows as fuzzy_resolve
from src.llm_resolver import resolve_unresolved_bank_rows as llm_resolve
from src.llm_resolver import verify_candidate

KNOWN = {"UTR900001", "UTR900002", "UTR700007"}


def bank_row(txn_id="bnk_1", narration="", amount=976.4):
    return pd.Series({"txn_id": txn_id, "date": "2026-08-05", "amount": amount,
                      "narration": narration, "type": "credit"})


# ------------------------------------------------------- fuzzy tier (no LLM)

def test_fuzzy_recovers_a_utr_whose_prefix_the_bank_dropped():
    result = fuzzy_resolve_narration("NEFT-RZPY-900001/settlemnt", KNOWN)
    assert result["utr_candidate"] == "UTR900001"
    assert result["confidence"] >= 0.9


def test_fuzzy_refuses_when_two_known_utrs_are_equally_plausible():
    """An ambiguous guess is worse than no guess -- hand it to the next tier."""
    assert fuzzy_resolve_narration("PAYOUT 900001 AND 900002 COMBINED", KNOWN) is None


def test_fuzzy_returns_nothing_for_genuinely_free_text():
    assert fuzzy_resolve_narration("CR/ONLINE TRF/paymnt gateway aug batch", KNOWN) is None


def test_fuzzy_never_proposes_a_utr_outside_the_settlement_data():
    result = fuzzy_resolve_narration("NEFT-RZPY-123456/settlemnt", KNOWN)
    assert result is None


def test_fuzzy_tier_splits_rows_it_can_and_cannot_recover():
    links, still = fuzzy_resolve(
        [bank_row("bnk_1", "NEFT-RZPY-900001/settlemnt"),
         bank_row("bnk_2", "CR/ONLINE TRF/no ref quoted")],
        KNOWN)
    assert [l["txn_id"] for l in links] == ["bnk_1"]
    assert [r["txn_id"] for r in still] == ["bnk_2"]


# -------------------------------------------------------------- LLM tier

def test_no_api_key_means_exception_never_a_forced_match(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    links, exceptions = llm_resolve([bank_row("bnk_1", "CR/ONLINE TRF/no ref")], KNOWN)
    assert links == []
    assert len(exceptions) == 1
    assert exceptions[0]["reason_code"] == "narration_unresolved"
    assert "no ANTHROPIC_API_KEY" in exceptions[0]["basis"]


def test_a_confident_llm_proposal_for_an_unknown_utr_is_rejected(monkeypatch):
    """
    THE boundary test. The model returns a plausible, well-formed, maximally
    confident UTR that no settlement carries. It must not become a match.
    """
    monkeypatch.setattr(llm_resolver, "resolve_narration", lambda narration: {
        "utr_candidate": "UTR999999", "order_id_candidate": None, "confidence": 1.0,
    })
    links, exceptions = llm_resolve([bank_row("bnk_1", "CR/ONLINE TRF/no ref")], KNOWN)
    assert links == []
    assert exceptions[0]["reason_code"] == "narration_unresolved"
    assert "proposal rejected" in exceptions[0]["basis"]


def test_low_confidence_proposal_is_rejected_even_when_the_utr_is_real(monkeypatch):
    monkeypatch.setattr(llm_resolver, "resolve_narration", lambda narration: {
        "utr_candidate": "UTR900001", "order_id_candidate": None, "confidence": 0.4,
    })
    links, exceptions = llm_resolve([bank_row("bnk_1", "vague")], KNOWN)
    assert links == []
    assert "below threshold" in exceptions[0]["basis"]


def test_verified_proposal_becomes_a_link_not_a_match(monkeypatch):
    """A survivor is still only a proposal -- Stage B does the money check."""
    monkeypatch.setattr(llm_resolver, "resolve_narration", lambda narration: {
        "utr_candidate": "UTR 900001", "order_id_candidate": None, "confidence": 0.95,
    })
    links, exceptions = llm_resolve([bank_row("bnk_1", "some narration")], KNOWN)
    assert exceptions == []
    assert links[0]["utr_candidate"] == "UTR900001"
    assert links[0]["txn_id"] == "bnk_1"
    assert "amount and date still checked deterministically" in links[0]["basis"]


def test_llm_failure_is_an_exception_not_a_guess(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(llm_resolver.anthropic, "Anthropic", Boom)
    links, exceptions = llm_resolve([bank_row("bnk_1", "narration")], KNOWN)
    assert links == []
    assert "routed to exception" in exceptions[0]["basis"]


def test_verify_candidate_rules():
    assert verify_candidate("UTR900001", KNOWN) == "UTR900001"
    assert verify_candidate("utr-900001", KNOWN) == "UTR900001"   # normalised
    assert verify_candidate("900001", KNOWN) == "UTR900001"       # prefix dropped
    assert verify_candidate("UTR999999", KNOWN) is None           # not ours
    assert verify_candidate(None, KNOWN) is None
    assert verify_candidate("", KNOWN) is None
    assert verify_candidate("9", KNOWN) is None                   # too short to mean anything


# ------------------------------------- the request the LLM tier actually sends

def _fake_anthropic(monkeypatch, payload, captured):
    """Stands in for the SDK so the request shape and parsing are covered."""
    import json as _json
    from types import SimpleNamespace

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=_json.dumps(payload))])

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.anthropic, "Anthropic", FakeClient)


def test_llm_request_never_contains_settlement_data(monkeypatch):
    """
    The model is shown the narration and nothing else. It cannot see the list of
    valid UTRs, so it cannot pick a plausible one off a menu -- it has to read
    one out of the text, and verify_candidate then checks whether it exists.
    """
    captured = {}
    narration = "NEFT-RZPY-900001/settlemnt"
    _fake_anthropic(monkeypatch, {
        "utr_candidate": "900001", "order_id_candidate": None, "confidence": 0.9,
    }, captured)

    result = llm_resolver.resolve_narration(narration)

    assert result["utr_candidate"] == "900001"
    assert captured["messages"] == [{"role": "user", "content": narration}]
    for known in KNOWN:
        assert known not in str(captured)


def test_llm_request_asks_for_schema_constrained_json(monkeypatch):
    captured = {}
    _fake_anthropic(monkeypatch, {
        "utr_candidate": None, "order_id_candidate": None, "confidence": 0.0,
    }, captured)

    llm_resolver.resolve_narration("CR/ONLINE TRF/no ref")

    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {
        "utr_candidate", "order_id_candidate", "confidence"}
    assert captured["model"] == llm_resolver.MODEL


def test_llm_tier_end_to_end_with_a_stubbed_model(monkeypatch):
    captured = {}
    _fake_anthropic(monkeypatch, {
        "utr_candidate": "900001", "order_id_candidate": None, "confidence": 0.92,
    }, captured)

    links, exceptions = llm_resolve([bank_row("bnk_1", "NEFT-RZPY-900001/x")], KNOWN)
    assert exceptions == []
    assert links[0]["utr_candidate"] == "UTR900001"   # prefix restored by verification
    assert links[0]["confidence"] == 0.92
