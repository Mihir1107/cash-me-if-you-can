"""
Tests for the triage layer.

Two properties matter more than the rest. Triage must not lose or invent money,
since it is a re-presentation of the same exposure the money report already
balanced; and it must never imply that anything has been fixed, because nothing
here touches a ledger.
"""

import shutil

import pytest

from src.reconcile import run_reconciliation
from src.triage import (MATERIALITY_FLOOR, build_incidents, build_triage_report,
                        materiality_threshold)


def exc(order_id, reason_code, stage="ledger_settlement", basis="b", **detail):
    return {"order_id": order_id, "stage": stage, "reason_code": reason_code,
            "basis": basis, "detail": detail}


def exposure(**pairs):
    return {oid: {"exposure": value, "basis": "test"} for oid, value in pairs.items()}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = tmp_path / "data"
    out = tmp_path / "output"
    shutil.copytree("data", data)
    out.mkdir()
    return data, out


# ------------------------------------------------------------- clustering

def test_one_systemic_cause_becomes_one_incident_not_five():
    """Five orders, one misconfigured fee. That is one ticket."""
    exceptions = [exc(f"order_{i}", "duplicate_settlement") for i in range(5)]
    incidents = build_incidents(
        exceptions, exposure(**{f"order_{i}": 100.0 for i in range(5)}), 10_000.0)

    assert len(incidents) == 1
    assert incidents[0]["order_count"] == 5
    assert incidents[0]["value_at_risk"] == 500.0
    assert len(incidents[0]["order_ids"]) == 5


def test_per_order_codes_stay_separate():
    """Each chargeback is its own event and must not be collapsed."""
    exceptions = [exc(f"order_{i}", "settlement_reversed") for i in range(3)]
    incidents = build_incidents(
        exceptions, exposure(**{f"order_{i}": 100.0 for i in range(3)}), 10_000.0)

    assert len(incidents) == 3
    assert all(i["order_count"] == 1 for i in incidents)


def test_delays_cluster_by_severity_band_not_into_one_lump():
    exceptions = [
        exc("order_1", "bank_credit_delayed", stage="settlement_bank", date_gap_days=6),
        exc("order_2", "bank_credit_delayed", stage="settlement_bank", date_gap_days=7),
        exc("order_3", "bank_credit_delayed", stage="settlement_bank", date_gap_days=40),
    ]
    incidents = build_incidents(
        exceptions, exposure(order_1=100.0, order_2=100.0, order_3=100.0), 10_000.0)

    assert len(incidents) == 2
    assert sorted(i["order_count"] for i in incidents) == [1, 2]


def test_an_order_failing_both_legs_is_counted_once():
    """Triage inherits the money report's rule: one bucket per order."""
    exceptions = [
        exc("order_1", "fee_footing_mismatch", stage="ledger_settlement", fee=20.0, tax=3.6),
        exc("order_1", "bank_amount_mismatch", stage="settlement_bank"),
    ]
    incidents = build_incidents(exceptions, exposure(order_1=500.0), 10_000.0)

    assert sum(i["order_count"] for i in incidents) == 1
    assert sum(i["value_at_risk"] for i in incidents) == 500.0
    assert incidents[0]["reason_code"] == "fee_footing_mismatch"  # root cause wins


# ------------------------------------------------------------ prioritising

def test_consequence_outranks_size():
    """
    Unrecorded revenue outranks a larger delayed credit. A late credit that
    arrived is not the same kind of problem as money the books never saw.
    """
    exceptions = [
        exc("order_big", "bank_credit_delayed", stage="settlement_bank", date_gap_days=6),
        exc("order_small", "no_ledger_entry"),
    ]
    incidents = build_incidents(
        exceptions, exposure(order_big=900_000.0, order_small=50_000.0), 1_000_000.0)

    assert incidents[0]["reason_code"] == "no_ledger_entry"
    assert incidents[0]["urgency"] == "critical"
    assert incidents[-1]["reason_code"] == "bank_credit_delayed"


def test_immaterial_incidents_sort_last_but_are_still_reported():
    """Below the threshold is a recommendation, not a disappearance."""
    exceptions = [exc("order_1", "no_ledger_entry"), exc("order_2", "no_settlement_found")]
    incidents = build_incidents(
        exceptions, exposure(order_1=5.0, order_2=90_000.0), 1_000_000.0)

    trivial = next(i for i in incidents if i["order_ids"] == ["order_1"])
    assert trivial["material"] is False
    assert incidents[-1] is trivial          # sorted last
    assert trivial in incidents              # but never dropped


def test_materiality_scales_with_the_book_but_has_a_floor():
    assert materiality_threshold(10_000_000.0) == 50_000.0
    assert materiality_threshold(1_000.0) == MATERIALITY_FLOOR


def test_an_unknown_reason_code_routes_to_a_human_not_into_the_void():
    incidents = build_incidents([exc("order_1", "something_new_entirely")],
                                exposure(order_1=100.0), 10_000.0)
    assert incidents[0]["owner"] == "unassigned"
    assert incidents[0]["routed_by_policy"] is False
    assert "by hand" in incidents[0]["recommended_action"]


def test_every_shipped_reason_code_has_a_routing_policy(workspace):
    """A code the pipeline can emit but triage cannot route is a gap."""
    from src import triage

    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    emitted = set(report["exception_reason_counts"])

    assert emitted <= set(triage.POLICY), (
        f"unrouted reason codes: {sorted(emitted - set(triage.POLICY))}")
    assert all(i["routed_by_policy"] for i in report["triage"]["incidents"])


# ---------------------------------------------------------------- honesty

def test_triage_neither_loses_nor_invents_money(workspace):
    """
    The incident values must reconcile to the money report exactly. Triage is a
    re-presentation of the same exposure, so any drift means one of the two
    views is lying.
    """
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    triage, money = report["triage"], report["money"]

    total = round(sum(i["value_at_risk"] for i in triage["incidents"]), 2)
    assert abs(total - money["at_risk_value"]) < 0.01

    above = triage["value_above_threshold"]
    below = triage["value_below_threshold"]
    assert abs((above + below) - money["at_risk_value"]) < 0.01

    orders = sum(i["order_count"] for i in triage["incidents"])
    assert orders == report["unreconciled_orders"]


def test_triage_collapses_the_queue_without_hiding_any_order(workspace):
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))
    triage = report["triage"]

    assert triage["incident_count"] < triage["exception_rows"]  # actually collapses

    listed = {oid for i in triage["incidents"] for oid in i["order_ids"]}
    excepted = {e["order_id"] for e in report["exceptions"] if e["order_id"]}
    assert listed == excepted  # and nothing is lost in the collapsing


def test_triage_never_claims_to_have_fixed_anything(workspace):
    data, out = workspace
    report, _ = run_reconciliation(data_dir=str(data), output_dir=str(out))

    assert "Recommendations only" in report["triage"]["note"]
    for incident in report["triage"]["incidents"]:
        assert "recommended_action" in incident
        assert "applied" not in incident
        assert "resolved" not in incident


def test_triage_is_absent_rather_than_wrong_without_exposure_data():
    assert build_triage_report([], {}, 0.0)["incidents"] == []
