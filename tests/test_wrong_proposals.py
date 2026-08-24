"""
The design thesis, stated as tests: an upstream tier being confidently WRONG
must not be able to produce a false match.

Every other test here checks that the tiers get things right. These check what
happens when they do not, which is the case the architecture actually exists
for. Tiers 2 and 3 only propose which settlement a bank credit should be
compared against; Stage B independently verifies amount, batch total and date
window. So a wrong proposal can only ever mis-route a comparison, never satisfy
one.

If any test in this file fails, the separation between proposing and confirming
has collapsed, and no other number in this project can be trusted.
"""

import pandas as pd
import pytest

from src import llm_resolver
from src.fuzzy_resolver import resolve_unresolved_bank_rows as fuzzy_resolve
from src.llm_resolver import resolve_unresolved_bank_rows as llm_resolve
from src.matcher import match_settlement_to_bank


def settlement(order_id, utr, settled, settlement_id="s"):
    return {"settlement_id": settlement_id, "payment_id": "p", "order_id": order_id,
            "gross_amount": round(settled * 1.03, 2), "fee": 20.0, "tax": 3.6,
            "refund_amount": 0.0, "settled_amount": settled,
            "settlement_date": "2026-08-03", "utr": utr}


def bank(txn_id, amount, narration, date="2026-08-05"):
    return {"txn_id": txn_id, "date": date, "amount": amount,
            "narration": narration, "type": "credit"}


TWO_SETTLEMENTS = pd.DataFrame([
    settlement("order_1", "UTR100005", 976.40, "s1"),
    settlement("order_2", "UTR100006", 4882.00, "s2"),
])


def verdicts(results):
    return {r.order_id: (r.status, r.reason_code) for r in results}


def test_a_confidently_wrong_fuzzy_proposal_creates_no_match():
    """
    The credit is order_2's money, but its narration is corrupted into something
    a character closer to order_1's reference. The fuzzy tier proposes order_1
    at 94% confidence. It is wrong.

    Stage B compares the credit against order_1's settlement, sees 4882 against
    976.40, and refuses. Nothing is matched, and both orders are reported
    accurately: order_1 as an amount mismatch, order_2 as never credited.
    """
    credit = pd.DataFrame([bank("b1", 4882.00, "NEFT RZPY UTR10005 CR")])
    known = set(TWO_SETTLEMENTS["utr"])

    links, _ = fuzzy_resolve([r for _, r in credit.iterrows()], known)
    assert links, "precondition: the fuzzy tier must actually make a proposal here"
    assert links[0]["utr_candidate"] == "UTR100005"   # the wrong settlement
    assert links[0]["confidence"] >= 0.9              # and it is confident

    results, _ = match_settlement_to_bank(
        TWO_SETTLEMENTS, credit,
        {l["txn_id"]: l["utr_candidate"] for l in links})

    assert not [r for r in results if r.status == "matched"]
    assert verdicts(results) == {
        "order_1": ("exception", "bank_amount_mismatch"),
        "order_2": ("exception", "settlement_not_credited"),
    }


def test_a_confidently_wrong_llm_proposal_creates_no_match(monkeypatch):
    """
    Same shape, but the model is the one that is wrong, and it names a real
    settlement at maximum confidence. Verification does not care how sure it was.
    """
    monkeypatch.setattr(llm_resolver, "resolve_narration", lambda narration: {
        "utr_candidate": "UTR100005", "order_id_candidate": None,
        "confidence": 1.0, "llm_invoked": True,
    })

    credit = pd.DataFrame([bank("b1", 4882.00, "CR/ONLINE TRF/no ref quoted")])
    known = set(TWO_SETTLEMENTS["utr"])

    links, exceptions = llm_resolve([r for _, r in credit.iterrows()], known)
    assert exceptions == []
    assert links[0]["utr_candidate"] == "UTR100005"   # verified as real, still wrong
    assert links[0]["confidence"] == 1.0

    results, _ = match_settlement_to_bank(
        TWO_SETTLEMENTS, credit,
        {l["txn_id"]: l["utr_candidate"] for l in links})

    assert not [r for r in results if r.status == "matched"]
    assert verdicts(results)["order_1"] == ("exception", "bank_amount_mismatch")


def test_a_wrong_proposal_cannot_smuggle_a_credit_past_the_date_window():
    """Mis-routing must not bypass the date check either."""
    monkeypatch_free = pd.DataFrame([settlement("order_1", "UTR100005", 976.40)])
    stale = pd.DataFrame([bank("b1", 976.40, "no ref", date="2026-09-30")])

    results, _ = match_settlement_to_bank(
        monkeypatch_free, stale, {"b1": "UTR100005"})

    assert verdicts(results)["order_1"] == ("exception", "bank_credit_delayed")


def test_a_proposal_naming_a_settlement_that_does_not_exist_is_dropped():
    """Verification happens before comparison, not after."""
    credit = pd.DataFrame([bank("b1", 976.40, "no ref")])
    results, _ = match_settlement_to_bank(
        TWO_SETTLEMENTS, credit, {"b1": "UTR_NOT_REAL"})

    assert not [r for r in results if r.status == "matched"]


@pytest.mark.parametrize("confidence", [0.7, 0.9, 0.99, 1.0])
def test_no_confidence_level_can_buy_a_match(monkeypatch, confidence):
    """
    Confidence gates whether a proposal is considered at all. It never
    substitutes for verification, at any value including certainty.
    """
    monkeypatch.setattr(llm_resolver, "resolve_narration", lambda narration: {
        "utr_candidate": "UTR100005", "order_id_candidate": None,
        "confidence": confidence, "llm_invoked": True,
    })
    credit = pd.DataFrame([bank("b1", 999_999.0, "CR/ONLINE TRF/no ref")])

    links, _ = llm_resolve([r for _, r in credit.iterrows()],
                           set(TWO_SETTLEMENTS["utr"]))
    extra = {l["txn_id"]: l["utr_candidate"] for l in links}
    results, _ = match_settlement_to_bank(TWO_SETTLEMENTS, credit, extra)

    assert not [r for r in results if r.status == "matched"]
