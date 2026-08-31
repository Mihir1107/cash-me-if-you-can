"""
Tier 3 for schemas, tested the way the narration tier is: by being wrong on
purpose.

The model proposes what a column means. That proposal is checked against the
file before anything uses it. So the question these tests ask is not whether the
model is clever -- it is what happens when it is confidently, plausibly wrong,
because a mis-mapped column is the quietest failure in this system. Swap gross
and settled and the footing still foots, the identity still balances, and every
figure in the report is wrong with nothing anywhere to show for it.

If any test in this file fails, the schema tier has stopped being a proposal and
started being a decision.
"""

import csv
from pathlib import Path

import pandas as pd
import pytest

from src import reconcile, schema, schema_llm
from src.reconcile import SourceUnavailable, load_source

DATA = Path(__file__).resolve().parent.parent / "data"

OPAQUE = {"settlement_id": "c1", "payment_id": "c2", "order_id": "c3",
          "gross_amount": "c4", "fee": "c5", "tax": "c6", "refund_amount": "c7",
          "settled_amount": "c8", "settlement_date": "c9", "utr": "c10"}


@pytest.fixture
def opaque_settlements(tmp_path):
    """Real settlement data under headers no alias table could ever place."""
    rows = list(csv.DictReader((DATA / "razorpay_settlements.csv").open()))
    out = [{new: r[old] for old, new in OPAQUE.items()} for r in rows]
    path = tmp_path / "razorpay_settlements.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(OPAQUE.values()))
        w.writeheader()
        w.writerows(out)
    return path


def stub(monkeypatch, mapping, confidence=0.99):
    """Make the model return exactly this proposal, at high confidence."""
    def fake(columns, source, filename="x"):
        return {"mapping": dict(mapping), "llm_invoked": True, "note": None}
    monkeypatch.setattr(schema_llm, "propose_mapping", fake)


# ------------------------------------------------- a correct proposal is used

def test_a_correct_proposal_unlocks_a_file_the_table_could_not_read(
        monkeypatch, opaque_settlements):
    stub(monkeypatch, {field: col for field, col in OPAQUE.items()})
    df = load_source(opaque_settlements, "settlements", allow_llm=True)

    assert list(df.columns)
    assert "settled_amount" in df.columns
    # and it is genuinely the settled column, not merely a column called that
    canonical = pd.read_csv(DATA / "razorpay_settlements.csv")
    assert df["settled_amount"].tolist() == canonical["settled_amount"].tolist()


def test_without_the_tier_the_same_file_is_refused(opaque_settlements):
    """The proposal is what unlocks it, so the default must still refuse."""
    with pytest.raises(SourceUnavailable):
        load_source(opaque_settlements, "settlements", allow_llm=False)


# ----------------------------------------- a confidently wrong one is refused

def test_a_gross_settled_swap_is_caught_and_the_whole_proposal_discarded(
        monkeypatch, opaque_settlements):
    """
    The most dangerous mistake available. Every downstream check still passes
    with these two swapped, so verification against the file is the only thing
    standing between this and a report that is wrong everywhere.
    """
    wrong = dict(OPAQUE)
    wrong["gross_amount"], wrong["settled_amount"] = (OPAQUE["settled_amount"],
                                                      OPAQUE["gross_amount"])
    stub(monkeypatch, wrong)

    with pytest.raises(SourceUnavailable) as caught:
        load_source(opaque_settlements, "settlements", allow_llm=True)
    assert caught.value.missing


def test_a_fee_gross_swap_is_caught(monkeypatch, opaque_settlements):
    wrong = dict(OPAQUE)
    wrong["fee"], wrong["gross_amount"] = OPAQUE["gross_amount"], OPAQUE["fee"]
    stub(monkeypatch, wrong)

    with pytest.raises(SourceUnavailable):
        load_source(opaque_settlements, "settlements", allow_llm=True)


def test_a_date_field_pointed_at_a_reference_is_caught(
        monkeypatch, opaque_settlements):
    wrong = dict(OPAQUE)
    wrong["settlement_date"] = OPAQUE["utr"]
    stub(monkeypatch, wrong)

    with pytest.raises(SourceUnavailable):
        load_source(opaque_settlements, "settlements", allow_llm=True)


@pytest.mark.parametrize("confidence", [0.7, 0.95, 1.0])
def test_no_confidence_rescues_a_mapping_the_data_contradicts(
        monkeypatch, opaque_settlements, confidence):
    """Certainty is not evidence. Same rule as the narration tier."""
    wrong = dict(OPAQUE)
    wrong["gross_amount"], wrong["settled_amount"] = (OPAQUE["settled_amount"],
                                                      OPAQUE["gross_amount"])
    stub(monkeypatch, wrong, confidence=confidence)

    with pytest.raises(SourceUnavailable):
        load_source(opaque_settlements, "settlements", allow_llm=True)


def test_a_failed_proposal_is_discarded_whole_not_partially_kept(
        monkeypatch, opaque_settlements):
    """
    A mapping is one claim about what a file means. If the model confused two
    columns, the rest of what it said has not earned any trust either.
    """
    wrong = dict(OPAQUE)
    wrong["gross_amount"], wrong["settled_amount"] = (OPAQUE["settled_amount"],
                                                      OPAQUE["gross_amount"])
    stub(monkeypatch, wrong)

    df = pd.read_csv(opaque_settlements)
    found = schema.resolve(df.columns, "settlements")
    out = reconcile.propose_and_verify(df, found, "settlements")

    assert not out["ready"]
    assert out["mapping"] == found["mapping"]      # nothing of the proposal kept
    assert out["llm_rejected"]


# ----------------------------------------- the deterministic table still wins

def test_the_alias_table_beats_the_model_where_it_was_confident(monkeypatch):
    """
    A free, exact, deterministic answer is never overridden by a proposal. The
    model fills gaps; it does not get a vote on what the table already knows.
    """
    df = pd.read_csv(DATA / "razorpay_settlements.csv")
    found = schema.resolve(df.columns, "settlements")
    assert found["ready"]

    stub(monkeypatch, {"gross_amount": "settled_amount",
                       "settled_amount": "gross_amount"})
    out = reconcile.propose_and_verify(df, found, "settlements")
    assert out["mapping"]["gross_amount"] == "gross_amount"


def test_the_tier_is_off_by_default(opaque_settlements, monkeypatch):
    """A run must not silently depend on a network call."""
    called = []
    monkeypatch.setattr(schema_llm, "propose_mapping",
                        lambda *a, **k: called.append(1) or {"mapping": {}})
    with pytest.raises(SourceUnavailable):
        load_source(opaque_settlements, "settlements")
    assert not called


def test_no_key_degrades_to_the_alias_table(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = schema_llm.propose_mapping(["a", "b"], "ledger")
    assert out["mapping"] == {}
    assert out["llm_invoked"] is False
    assert "OPENAI_API_KEY" in out["note"]


# --------------------------------------------- the proposal must be well-formed

def test_a_column_the_file_does_not_have_is_dropped(monkeypatch):
    """The model naming an imaginary column is discarded before any checking."""
    class Choice:
        def __init__(self, f, c, conf):
            self.field, self.column, self.confidence = f, c, conf

    class Parsed:
        choices = [Choice("order_id", "does_not_exist", 0.99),
                   Choice("amount", "Total", 0.99)]

    class FakeResp:
        output_parsed = Parsed()

    class FakeClient:
        def __init__(self, **kw): self.responses = self
        def parse(self, **kw): return FakeResp()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(schema_llm, "openai", type("m", (), {"OpenAI": FakeClient}))

    out = schema_llm.propose_mapping(["Total", "When"], "ledger")
    assert "order_id" not in out["mapping"]
    assert out["mapping"]["amount"] == "Total"


def test_one_column_cannot_be_claimed_by_two_fields(monkeypatch):
    class Choice:
        def __init__(self, f, c, conf):
            self.field, self.column, self.confidence = f, c, conf

    class Parsed:
        choices = [Choice("amount", "Total", 0.99), Choice("date", "Total", 0.99)]

    class FakeResp:
        output_parsed = Parsed()

    class FakeClient:
        def __init__(self, **kw): self.responses = self
        def parse(self, **kw): return FakeResp()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(schema_llm, "openai", type("m", (), {"OpenAI": FakeClient}))

    out = schema_llm.propose_mapping(["Total", "When"], "ledger")
    assert list(out["mapping"].values()).count("Total") == 1


# ------------------------------- the checks a single file cannot make alone

def opaque_all_three(tmp_path):
    """All three files under headers no table could place."""
    import shutil

    for src, dst, cols in [
        ("internal_ledger.csv", "internal_ledger.csv",
         {"ledger_id": "f1", "order_id": "f2", "customer": "f3",
          "amount": "f4", "date": "f5", "status": "f6"}),
        ("razorpay_settlements.csv", "razorpay_settlements.csv", OPAQUE),
        ("bank_statement.csv", "bank_statement.csv",
         {"txn_id": "g1", "date": "g2", "amount": "g3", "narration": "g4",
          "type": "g5"}),
    ]:
        rows = list(csv.DictReader((DATA / src).open()))
        with (tmp_path / dst).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(cols.values()))
            w.writeheader()
            w.writerows([{n: r[o] for o, n in cols.items()} for r in rows])
    return tmp_path


def test_a_proposal_that_breaks_the_join_is_refused_at_the_pipeline_level(
        monkeypatch, tmp_path):
    """
    The gap single-file verification cannot close. Proposing the ledger's own
    row id as the order id passes every check inside that file -- it is a
    populated, unique string column. It only falls apart against the settlement
    report, where the two then share no orders at all.
    """
    import os

    from src.reconcile import run_reconciliation

    d = opaque_all_three(tmp_path)
    os.environ.pop("OPENAI_API_KEY", None)

    def fake(columns, source, filename="x"):
        if source == "ledger":
            return {"mapping": {"ledger_id": "f2", "order_id": "f1",
                                "amount": "f4", "date": "f5", "customer": "f3",
                                "status": "f6"},
                    "llm_invoked": True, "note": None}
        if source == "settlements":
            return {"mapping": dict(OPAQUE), "llm_invoked": True, "note": None}
        return {"mapping": {"txn_id": "g1", "date": "g2", "amount": "g3",
                            "narration": "g4", "type": "g5"},
                "llm_invoked": True, "note": None}

    monkeypatch.setattr(schema_llm, "propose_mapping", fake)

    with pytest.raises(SourceUnavailable) as caught:
        run_reconciliation(data_dir=str(d), output_dir=str(tmp_path / "out"),
                           allow_llm_schema=True)
    assert "same money" in str(caught.value)


def test_a_correct_proposal_for_all_three_runs_end_to_end(monkeypatch, tmp_path):
    import os
    import shutil

    from src.reconcile import run_reconciliation

    d = opaque_all_three(tmp_path)
    (tmp_path / "out").mkdir(exist_ok=True)
    os.environ.pop("OPENAI_API_KEY", None)

    right = {
        "ledger": {"ledger_id": "f1", "order_id": "f2", "customer": "f3",
                   "amount": "f4", "date": "f5", "status": "f6"},
        "settlements": dict(OPAQUE),
        "bank": {"txn_id": "g1", "date": "g2", "amount": "g3",
                 "narration": "g4", "type": "g5"},
    }
    monkeypatch.setattr(schema_llm, "propose_mapping",
                        lambda columns, source, filename="x": {
                            "mapping": right[source], "llm_invoked": True,
                            "note": None})

    report, _ = run_reconciliation(data_dir=str(d),
                                   output_dir=str(tmp_path / "out"),
                                   allow_llm_schema=True)
    assert report["total_orders"] == 57
    assert report["money"]["identity"]["holds"]
    assert report["source_diagnostics"]["ok"]
