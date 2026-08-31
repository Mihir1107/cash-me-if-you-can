"""
The period close gate, tested directly.

This is the module that answers the only question the whole pipeline exists to
support -- can the books be signed -- and it was previously covered only by
end-to-end runs, which exercise one shape of report and never the shapes that
matter: a broken audit trail, an unreadable bank statement, an identity that
does not hold.

Each condition is tested twice: once for the state that must block, once for
the state that must not. The second half is the half that gets skipped, and it
is where a gate quietly becomes a rubber stamp or a permanent red light.
"""

import pytest

from src.close_gate import CONDITIONS, evaluate_close, print_close_gate


def report(**overrides):
    """A clean report that closes. Each test breaks exactly one thing."""
    base = {
        "bank_source_error": None,
        "exception_reason_counts": {},
        "money": {
            "total_exposure": 100_000.0,
            "unattributed_bank_credit_value": 0.0,
            "at_risk_by_reason": {},
            "identity": {"holds": True, "residual": 0.0},
        },
        "triage": {"materiality_threshold": 1_000.0, "material_incident_count": 0},
    }
    base.update(overrides)
    return base


def intact(ok=True):
    return {"intact": ok, "broken_at": None if ok else 7,
            "reason": "verified" if ok else "line 7 has been edited"}


def blockers(gate):
    return {b["condition"] for b in gate["blockers"]}


# --------------------------------------------------------- the clean baseline

def test_a_clean_report_can_close():
    gate = evaluate_close(report(), intact())
    assert gate["can_close"]
    assert gate["blocker_count"] == 0
    assert len(gate["conditions_passed"]) == len(CONDITIONS) == 7


def test_the_gate_decides_nothing_it_only_records():
    """
    The line that keeps this honest. Nothing here posts an entry or closes a
    period; it states what a controller would have to resolve first.
    """
    assert "signs nothing" in evaluate_close(report(), intact())["note"]


# ------------------------------------------------------ each condition blocks

def test_a_broken_audit_trail_blocks():
    """A figure whose derivation cannot be verified is not evidence."""
    gate = evaluate_close(report(), intact(ok=False))
    assert not gate["can_close"]
    assert "audit_trail_intact" in blockers(gate)


def test_an_unreadable_bank_statement_blocks_everything():
    gate = evaluate_close(report(bank_source_error="file not found"), intact())
    assert "sources_verifiable" in blockers(gate)
    # nothing was confirmed, so the whole book is what is at stake
    at_risk = [b for b in gate["blockers"] if b["condition"] == "sources_verifiable"]
    assert at_risk[0]["value_at_risk"] == 100_000.0


def test_an_identity_that_does_not_hold_blocks():
    """The report failing to account for its own arithmetic."""
    money = report()["money"] | {"identity": {"holds": False, "residual": -12.5}}
    gate = evaluate_close(report(money=money), intact())
    assert "books_balance" in blockers(gate)
    found = [b for b in gate["blockers"] if b["condition"] == "books_balance"]
    assert found[0]["value_at_risk"] == 12.5   # a magnitude, not a signed residual


def test_unbooked_revenue_blocks():
    gate = evaluate_close(report(
        exception_reason_counts={"no_ledger_entry": 2},
        money=report()["money"] | {"at_risk_by_reason": {"no_ledger_entry": 28_876.92}},
    ), intact())
    assert "revenue_recorded" in blockers(gate)


def test_an_unbooked_reversal_blocks():
    gate = evaluate_close(report(
        exception_reason_counts={"settlement_reversed": 1},
        money=report()["money"] | {"at_risk_by_reason": {"settlement_reversed": 13_381.5}},
    ), intact())
    assert "reversals_booked" in blockers(gate)


def test_unattributable_cash_above_the_threshold_blocks():
    money = report()["money"] | {"unattributed_bank_credit_value": 43_868.63}
    gate = evaluate_close(report(money=money), intact())
    assert "cash_attributable" in blockers(gate)


def test_open_material_incidents_block():
    gate = evaluate_close(report(
        triage={"materiality_threshold": 1_000.0, "material_incident_count": 10},
    ), intact())
    assert "material_exceptions_resolved" in blockers(gate)


# -------------------------------------------- and what must deliberately NOT

def test_unattributable_cash_below_the_threshold_does_not_block():
    """
    Holding a close open for less than the cost of chasing it is theatre. The
    money is still reported, still counted, still inside the identity.
    """
    money = report()["money"] | {"unattributed_bank_credit_value": 999.0}
    gate = evaluate_close(report(money=money), intact())
    assert gate["can_close"]


def test_a_late_but_arrived_credit_does_not_block():
    """
    A payment outside the normal window is a timing observation, not a
    misstatement. The money is there and the books can say so.
    """
    gate = evaluate_close(report(
        exception_reason_counts={"bank_credit_delayed": 5},
        money=report()["money"] | {"at_risk_by_reason": {"bank_credit_delayed": 52_592.63}},
    ), intact())
    assert gate["can_close"]


def test_no_audit_trail_at_all_does_not_block():
    """
    An ablation pass writes no trail. Absence of a trail is not evidence of a
    broken one, and treating it as such would make every tier comparison fail.
    """
    assert evaluate_close(report(), None)["can_close"]


def test_an_ambiguous_attribution_does_not_by_itself_block():
    """
    Refusing to attribute a credit is the system working. It becomes a blocker
    only through the ordinary routes -- unattributable cash, or an open material
    incident -- never as a special case.
    """
    gate = evaluate_close(report(
        exception_reason_counts={"attribution_ambiguous": 3},
    ), intact())
    assert gate["can_close"]


# --------------------------------------------------------------- the shapes

def test_an_empty_report_does_not_explode():
    """Degraded and empty runs still have to produce a decision."""
    gate = evaluate_close({}, None)
    assert gate["conditions_checked"] == 7
    assert isinstance(gate["can_close"], bool)


def test_blocking_value_is_the_sum_of_the_blockers():
    money = report()["money"] | {
        "unattributed_bank_credit_value": 43_868.63,
        "at_risk_by_reason": {"settlement_reversed": 13_381.5},
    }
    gate = evaluate_close(report(
        money=money, exception_reason_counts={"settlement_reversed": 1},
    ), intact())
    assert gate["value_blocking_close"] == pytest.approx(
        sum(b["value_at_risk"] for b in gate["blockers"]))


def test_passed_and_blocked_conditions_partition_the_whole_set():
    """No condition may be silently skipped."""
    gate = evaluate_close(report(
        exception_reason_counts={"no_ledger_entry": 1}), intact())
    assert len(gate["conditions_passed"]) + gate["blocker_count"] == len(CONDITIONS)


def test_printing_a_blocked_gate_names_what_to_do(capsys):
    gate = evaluate_close(report(
        exception_reason_counts={"no_ledger_entry": 2},
        money=report()["money"] | {"at_risk_by_reason": {"no_ledger_entry": 28_876.92}},
    ), intact())
    print_close_gate(gate)
    printed = capsys.readouterr().out

    assert "BLOCKED" in printed
    assert "revenue_recorded" in printed
    assert "28,876.92" in printed


def test_printing_a_clean_gate_says_it_can_close(capsys):
    print_close_gate(evaluate_close(report(), intact()))
    assert "BLOCKED" not in capsys.readouterr().out


def test_printing_nothing_is_safe(capsys):
    print_close_gate(None)
    assert capsys.readouterr().out == ""
