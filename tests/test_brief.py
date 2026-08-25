"""
Tests for the one place a model is used constructively.

The model phrases facts it is given; deterministic code checks it introduced
nothing. These tests are mostly about the checking, because that is what makes
the feature shippable rather than merely nice.
"""

from types import SimpleNamespace

import pytest

from src import brief as brief_module
from src.brief import (allowed_numbers, attach_briefs, draft_brief,
                       incident_facts, verify_brief)

FACTS = {
    "orders_affected": 2,
    "value_at_risk": 28876.92,
    "example_order_ids": ["order_000901", "order_000902"],
    "one_example_finding": "Razorpay settled 14000.00 for this order",
}


def incident(**overrides):
    base = {
        "reason_code": "no_ledger_entry", "signature": "all",
        "owner": "merchant_finance", "urgency": "critical",
        "recommended_action": "Book the missing order.",
        "order_count": 2, "value_at_risk": 28876.92, "material": True,
        "order_ids": ["order_000901", "order_000902"],
        "sample_basis": "Razorpay settled 14000.00 for this order",
        "routed_by_policy": True,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- the guard

def test_a_faithful_brief_is_accepted():
    text = ("Razorpay settled 2 orders totalling 28,876.92 that your ledger "
            "never recorded, including order_000901.")
    ok, invented = verify_brief(text, FACTS)
    assert ok and invented == []


@pytest.mark.parametrize("text,invented,why", [
    ("2 orders totalling 28,876.92, about 3.4% of monthly revenue.",
     [3.4], "a statistic nobody supplied"),
    ("2 orders worth roughly 28,877.",
     [28877.0], "a rounded figure is a different figure"),
    ("2 orders including order_000903.",
     [903.0], "an order id that does not exist"),
    ("This has been outstanding for 45 days.",
     [45.0], "a duration invented whole"),
])
def test_an_invented_number_is_caught(text, invented, why):
    ok, found = verify_brief(text, FACTS)
    assert not ok, why
    assert found == invented


def test_prose_without_numbers_is_fine():
    ok, invented = verify_brief(
        "Some settlements arrived for orders your ledger never recorded.", FACTS)
    assert ok and invented == []


def test_comma_formatting_does_not_read_as_a_different_number():
    assert verify_brief("28,876.92 is at risk", FACTS)[0]
    assert verify_brief("28876.92 is at risk", FACTS)[0]


def test_allowed_numbers_reaches_into_nested_facts():
    allowed = allowed_numbers(FACTS)
    assert 28876.92 in allowed
    assert 2.0 in allowed
    assert 901.0 in allowed        # from the order id
    assert 14000.0 in allowed      # from the example finding
    assert 3.4 not in allowed


# ------------------------------------------------------- what is shown

def test_the_model_is_shown_facts_and_nothing_derived():
    facts = incident_facts(incident())
    assert facts["value_at_risk"] == 28876.92
    assert facts["orders_affected"] == 2
    # no room to reason about anything it was not handed
    assert set(facts) == {
        "reason_code", "orders_affected", "value_at_risk", "urgency",
        "who_fixes_it", "recommended_action", "example_order_ids",
        "one_example_finding", "above_materiality_threshold",
    }


# ------------------------------------------------------------- drafting

def test_no_api_key_falls_back_to_the_deterministic_line(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = draft_brief(incident())
    assert result["source"] == "deterministic"
    assert result["text"] == "Book the missing order."


def _stub_model(monkeypatch, text):
    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                output_parsed=SimpleNamespace(brief=text) if text is not None else None)

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(brief_module.openai, "OpenAI", FakeClient)


def test_a_verified_draft_is_used(monkeypatch):
    _stub_model(monkeypatch,
                "Razorpay settled 2 orders totalling 28,876.92 your ledger "
                "never recorded.")
    result = draft_brief(incident())
    assert result["source"] == "llm"
    assert "28,876.92" in result["text"]


def test_a_draft_that_invents_a_figure_is_discarded(monkeypatch):
    """
    The whole point. A well-written brief carrying a fabricated number is worse
    than the dull deterministic line, so it loses.
    """
    _stub_model(monkeypatch,
                "This represents 3.4% of monthly revenue and has been open 45 days.")
    result = draft_brief(incident())

    assert result["source"] == "deterministic_after_rejected_draft"
    assert result["text"] == "Book the missing order."
    assert 3.4 in result["invented"]


def test_a_model_failure_falls_back_rather_than_raising(monkeypatch):
    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(brief_module.openai, "OpenAI", Boom)
    assert draft_brief(incident())["source"] == "deterministic"


def test_an_empty_draft_falls_back(monkeypatch):
    _stub_model(monkeypatch, "   ")
    assert draft_brief(incident())["source"] == "deterministic"


# --------------------------------------------------------------- scoping

def test_briefs_are_drafted_only_for_material_incidents(monkeypatch):
    _stub_model(monkeypatch, "Two orders totalling 28,876.92 need booking.")
    triage = {"incidents": [
        incident(material=True),
        incident(material=False, value_at_risk=5.0),
    ]}
    attach_briefs(triage, limit=5)

    assert "brief" in triage["incidents"][0]
    assert "brief" not in triage["incidents"][1]
    assert triage["briefs_drafted"] == 1


def test_drafting_is_capped_so_the_queue_cannot_run_up_a_bill(monkeypatch):
    _stub_model(monkeypatch, "Two orders totalling 28,876.92 need booking.")
    triage = {"incidents": [incident() for _ in range(10)]}
    attach_briefs(triage, limit=3)
    assert triage["briefs_drafted"] == 3


def test_attach_briefs_tolerates_an_empty_triage():
    assert attach_briefs({}) == {}
    assert attach_briefs({"incidents": []})["incidents"] == []
