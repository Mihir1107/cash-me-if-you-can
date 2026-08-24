"""
Tests for the two narration-resolution tiers above the regex matcher.

The load-bearing tests here are the ones asserting what the LLM tier CANNOT do:
it cannot turn a confident-sounding guess into a match, and it cannot match
anything at all when there is no API key.
"""

import pandas as pd
import pytest

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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    links, exceptions = llm_resolve([bank_row("bnk_1", "CR/ONLINE TRF/no ref")], KNOWN)
    assert links == []
    assert len(exceptions) == 1
    assert exceptions[0]["reason_code"] == "narration_unresolved"
    assert "no OPENAI_API_KEY" in exceptions[0]["basis"]


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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(llm_resolver.openai, "OpenAI", Boom)
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

def _fake_openai(monkeypatch, payload, captured):
    """Stands in for the SDK so the request shape and parsing are covered."""
    from types import SimpleNamespace

    from src.llm_resolver import NarrationReading

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            parsed = None if payload is None else NarrationReading(**payload)
            return SimpleNamespace(output_parsed=parsed)

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(llm_resolver.openai, "OpenAI", FakeClient)


def test_llm_request_never_contains_settlement_data(monkeypatch):
    """
    The model is shown the narration and nothing else. It cannot see the list of
    valid UTRs, so it cannot pick a plausible one off a menu -- it has to read
    one out of the text, and verify_candidate then checks whether it exists.
    """
    captured = {}
    narration = "NEFT-RZPY-900001/settlemnt"
    _fake_openai(monkeypatch, {
        "utr_candidate": "900001", "order_id_candidate": None, "confidence": 0.9,
    }, captured)

    result = llm_resolver.resolve_narration(narration)

    assert result["utr_candidate"] == "900001"
    assert captured["input"] == [
        {"role": "system", "content": llm_resolver.SYSTEM_PROMPT},
        {"role": "user", "content": narration},
    ]
    for known in KNOWN:
        assert known not in str(captured)


def test_llm_request_asks_for_schema_constrained_json(monkeypatch):
    captured = {}
    _fake_openai(monkeypatch, {
        "utr_candidate": None, "order_id_candidate": None, "confidence": 0.0,
    }, captured)

    llm_resolver.resolve_narration("CR/ONLINE TRF/no ref")

    # strict structured output: the response is schema-valid by construction,
    # so nothing downstream has to salvage JSON out of prose
    assert captured["text_format"] is llm_resolver.NarrationReading
    schema = llm_resolver.NarrationReading.model_json_schema()
    assert set(schema["required"]) == {
        "utr_candidate", "order_id_candidate", "confidence"}
    assert captured["model"] == llm_resolver.MODEL == "gpt-4o-mini"


def test_a_refusal_or_unparsable_reading_becomes_an_exception(monkeypatch):
    """`output_parsed` is None when the model declines. Never treat that as a match."""
    captured = {}
    _fake_openai(monkeypatch, None, captured)

    result = llm_resolver.resolve_narration("CR/ONLINE TRF/no ref")
    assert result["utr_candidate"] is None
    assert result["llm_invoked"] is True

    links, exceptions = llm_resolve([bank_row("bnk_1", "CR/ONLINE TRF/no ref")], KNOWN)
    assert links == []
    assert "no parsable reading" in exceptions[0]["basis"]


def test_llm_tier_end_to_end_with_a_stubbed_model(monkeypatch):
    captured = {}
    _fake_openai(monkeypatch, {
        "utr_candidate": "900001", "order_id_candidate": None, "confidence": 0.92,
    }, captured)

    links, exceptions = llm_resolve([bank_row("bnk_1", "NEFT-RZPY-900001/x")], KNOWN)
    assert exceptions == []
    assert links[0]["utr_candidate"] == "UTR900001"   # prefix restored by verification
    assert links[0]["confidence"] == 0.92


# ------------------------------------- fuzzy rule 2: character-level damage

SEQUENTIAL = {f"UTR{100000 + i}" for i in range(1, 10)}


def test_fuzzy_recovers_a_reference_missing_one_character():
    """
    Banks drop and mangle characters. Rule 1 needs the digits verbatim, so this
    is what rule 2 is for: close enough to exactly one known reference.
    """
    result = fuzzy_resolve_narration("NEFT RZPY UTR10005 CR", SEQUENTIAL)
    assert result["utr_candidate"] == "UTR100005"
    assert result["confidence"] >= 0.9
    assert "string match" in result["basis"]


@pytest.mark.parametrize("narration,why", [
    ("NEFT RZPY UTR100050 CR", "transposed digits"),
    ("NEFT RZPY UTRIOOOO5 CR", "letter/digit confusion"),
    ("NEFT RZPY UTR1000 CR", "truncated beyond recognition"),
    ("CR/ONLINE TRF/no ref quoted", "no reference at all"),
])
def test_fuzzy_refuses_rather_than_guessing(narration, why):
    """
    With nine sequential references in scope, a near-miss is as likely to be the
    wrong one as the right one. Refusing costs an LLM call; guessing costs money.
    """
    assert fuzzy_resolve_narration(narration, SEQUENTIAL) is None, why


def test_fuzzy_refuses_when_two_references_are_equally_close():
    """A tie is not a winner. It goes to the next tier."""
    known = {"UTR100005", "UTR100006"}
    # equidistant from both
    assert fuzzy_resolve_narration("NEFT RZPY UTR10000X CR", known) is None


def test_fuzzy_never_proposes_across_a_length_gap():
    """The prefilter must not be doing the matching by accident."""
    known = {"UTR100005"}
    assert fuzzy_resolve_narration("REF 1 CR", known) is None


# ---------------------------- the two tiers must not disagree with each other

@pytest.mark.parametrize("narration,expected", [
    ("PAYOUT UTR100005 CR", "UTR100005"),          # exact, both tiers
    ("NEFT-RZPY-100005/settlemnt", "UTR100005"),   # prefix dropped, fuzzy only
])
def test_a_nested_shorter_reference_is_not_a_competing_reference(narration, expected):
    """
    A book holding both UTR100005 and UTR10000 contains one nested inside the
    other. A narration quoting the longer one is not ambiguous, and neither tier
    may treat it as such.

    The matcher and the fuzzy tier once had their own copies of this scan and
    drifted: the matcher gained position-claiming, the fuzzy tier did not, and
    the same narration was resolved by one and refused by the other. They share
    one implementation now, and this pins that they agree.
    """
    from src.matcher import extract_utr_from_narration

    known = {"UTR100005", "UTR10000"}
    fuzzy = fuzzy_resolve_narration(narration, known)
    regex = extract_utr_from_narration(narration, known)

    assert fuzzy is not None
    assert fuzzy["utr_candidate"] == expected
    # where the regex tier has an opinion at all, it must be the same opinion
    assert regex in (None, expected)


def test_a_genuine_tie_is_still_refused_by_both_tiers():
    """Control: sharing the scan must not have bought resolution with safety."""
    from src.matcher import extract_utr_from_narration

    known = {"UTR100005", "UTR100006"}
    ambiguous = "PAYOUT REF 100005 AND REF 100006"

    assert fuzzy_resolve_narration(ambiguous, known) is None
    assert extract_utr_from_narration("CR REF UTR100005 / RVSL UTR100006", known) is None
