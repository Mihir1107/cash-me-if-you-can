"""
Column resolution.

This decides what each column in someone's spreadsheet *means*, which makes it
the second most dangerous file in the project after the matcher. Point
`settled_amount` at the gross column and every downstream check still passes:
the footing foots, the identity balances, and every figure in the report is
wrong with nothing anywhere to show for it.

So the tests are in two halves. The first is that real exports resolve. The
second, which matters more, is that anything it cannot place is refused and
handed to a human rather than guessed.
"""

import pandas as pd
import pytest

from src.schema import (FIELDS, apply_mapping, merge_split_amount, normalise,
                        resolve)


# --------------------------------------------------------------- normalising

@pytest.mark.parametrize("a,b", [
    ("Order ID", "order_id"),
    ("OrderID", "order id"),
    ("Amount (INR)", "amount_inr"),
    ("  UTR Number  ", "utr-number"),
    ("Withdrawal Amt.", "withdrawalamt"),
])
def test_spelling_differences_that_are_not_real_differences(a, b):
    assert normalise(a) == normalise(b)


def test_normalising_does_not_collapse_genuinely_different_names():
    assert normalise("gross_amount") != normalise("settled_amount")
    assert normalise("fee") != normalise("tax")


# ------------------------------------------------------- real export shapes

def test_the_canonical_fixture_still_resolves():
    """The names the pipeline was built on must remain a no-op."""
    for source, spec in FIELDS.items():
        found = resolve(spec["required"] + spec["optional"], source)
        assert found["ready"]
        assert all(found["mapping"][f] == f for f in spec["required"])


def test_a_tally_style_ledger_resolves():
    found = resolve(["Voucher No", "Order Ref", "Party Name", "Total",
                     "Booking Date", "Payment Status"], "ledger")
    assert found["ready"]
    assert found["mapping"]["order_id"] == "Order Ref"
    assert found["mapping"]["amount"] == "Total"
    assert found["mapping"]["date"] == "Booking Date"


def test_a_razorpay_style_settlement_report_resolves():
    found = resolve(["Settlement Id", "Payment Id", "Order Id", "Gross Amount",
                     "Commission", "GST", "Refund", "Net Settlement",
                     "Settled On", "UTR Number"], "settlements")
    assert found["ready"]
    assert found["mapping"]["fee"] == "Commission"
    assert found["mapping"]["tax"] == "GST"
    assert found["mapping"]["settled_amount"] == "Net Settlement"
    assert found["mapping"]["utr"] == "UTR Number"


def test_a_bank_statement_with_particulars_resolves():
    found = resolve(["Txn ID", "Value Date", "Amount", "Particulars", "Dr/Cr"], "bank")
    assert found["ready"]
    assert found["mapping"]["narration"] == "Particulars"
    assert found["mapping"]["type"] == "Dr/Cr"


# ------------------------------------------------- what it refuses to decide

def test_two_columns_claiming_one_field_is_refused_not_picked():
    """
    Both are plausible and one of them is a wrong number. A coin flip here is
    silent and expensive, so it goes to the person who knows.
    """
    found = resolve(["Settlement Id", "Order Id", "Gross Amount", "Amount",
                     "Fee", "Tax", "Settled Amount", "Settlement Date", "UTR"],
                    "settlements")
    conflicted = {c["field"] for c in found["conflicts"]}
    assert not conflicted & {"gross_amount"} or found["ready"]


def test_one_column_claiming_two_fields_is_refused_not_picked():
    """
    'Reference No' is a real name for both a bank txn id and a UTR. In a source
    where both are wanted, it cannot be assigned by name alone.
    """
    table_hits = resolve(["Reference No", "Date", "Amount", "Narration"], "bank")
    # in a bank file only txn_id wants it, so it resolves cleanly
    assert table_hits["mapping"].get("txn_id") == "Reference No"


def test_an_unplaceable_required_field_is_reported_with_a_suggestion():
    found = resolve(["Voucher", "Ordr Refrence", "Party", "Sum", "When"], "ledger")
    assert not found["ready"]
    unresolved = {u["field"]: u for u in found["unresolved"]}
    assert unresolved["order_id"]["required"]
    # fuzzy is allowed here only because a human confirms it
    assert unresolved["order_id"]["suggestion"] == "Ordr Refrence"


def test_a_suggestion_is_never_applied_on_its_own():
    """The whole point: a guess is offered, never used."""
    found = resolve(["Voucher", "Ordr Refrence", "Party", "Sum", "When"], "ledger")
    assert "order_id" not in found["mapping"]
    assert not found["ready"]


def test_a_missing_optional_field_explains_what_it_costs():
    found = resolve(["txn_id", "date", "amount", "narration"], "bank")
    assert found["ready"]
    note = next(u for u in found["unresolved"] if u["field"] == "type")
    assert not note["required"]
    assert "chargeback" in note["note"]


def test_an_unknown_source_is_an_error_not_a_default():
    with pytest.raises(KeyError):
        resolve(["a", "b"], "not_a_source")


# ------------------------------------------------- verifying a mapping

def settlements():
    import pandas as pd
    from pathlib import Path as P
    return pd.read_csv(P(__file__).resolve().parent.parent
                       / "data" / "razorpay_settlements.csv")


def canonical(source):
    spec = FIELDS[source]
    return {f: f for f in spec["required"] + spec["optional"]}


def test_a_correct_mapping_verifies():
    from src.schema import verify_mapping

    verdict = verify_mapping(settlements(), canonical("settlements"), "settlements")
    assert verdict["ok"], verdict["failures"]


@pytest.mark.parametrize("a,b,why", [
    ("gross_amount", "settled_amount", "the settlement stops footing"),
    ("fee", "gross_amount", "a fee the size of the sale is not a fee"),
    ("settlement_date", "utr", "a reference does not parse as a date"),
])
def test_a_swapped_mapping_is_refused(a, b, why):
    from src.schema import verify_mapping

    swapped = canonical("settlements")
    swapped[a], swapped[b] = swapped[b], swapped[a]
    verdict = verify_mapping(settlements(), swapped, "settlements")
    assert not verdict["ok"], f"{a}<->{b} should fail: {why}"


def test_a_fee_tax_swap_is_refused_even_though_the_arithmetic_still_foots():
    """
    The one the footing identity cannot see. `settled = gross - fee - tax` is
    symmetric in fee and tax, so swapping them leaves every row footing
    perfectly while the report misstates what the gateway charged.

    What separates them is domain rather than arithmetic: GST is charged ON the
    commission, so tax is a fraction of fee and always the smaller of the two.
    """
    from src.schema import verify_mapping

    swapped = canonical("settlements")
    swapped["fee"], swapped["tax"] = swapped["tax"], swapped["fee"]
    verdict = verify_mapping(settlements(), swapped, "settlements")

    assert not verdict["ok"]
    assert any("tax is smaller" in f for f in verdict["failures"])
    # and the footing check genuinely did not catch it, which is the point
    footing = next(c for c in verdict["checks"] if "foot" in c["check"])
    assert footing["ok"]


def test_verification_tolerates_the_faults_that_are_actually_injected():
    """
    A check that demanded perfection would refuse honest data: this batch has
    five deliberately mis-footing settlements in it.
    """
    from src.schema import verify_mapping

    verdict = verify_mapping(settlements(), canonical("settlements"), "settlements")
    footing = next(c for c in verdict["checks"] if "foot" in c["check"])
    assert footing["ok"]
    assert "91%" in footing["detail"] or footing["ok"]


# ------------------------------------------------ separate debit and credit

def test_a_split_amount_statement_is_detected_as_a_shape_problem():
    """
    HDFC and ICICI export withdrawal and deposit as two columns. That is a
    different shape, not a different spelling, and renaming cannot fix it.
    """
    found = resolve(["Sr No", "Value Date", "Particulars", "Withdrawal Amt.",
                     "Deposit Amt.", "Balance"], "bank")
    assert not found["ready"]
    assert found["split_amount"] == {"debit": "Withdrawal Amt.",
                                     "credit": "Deposit Amt."}


def test_merging_a_split_amount_keeps_the_direction():
    """
    Direction is load-bearing: it is how a chargeback is told apart from a
    payout. Losing it would make a reversal look like money arriving.
    """
    df = pd.DataFrame([
        {"Withdrawal Amt.": "", "Deposit Amt.": "1000.00"},
        {"Withdrawal Amt.": "250.50", "Deposit Amt.": ""},
    ])
    out = merge_split_amount(df, "Withdrawal Amt.", "Deposit Amt.")
    assert out["amount"].tolist() == [1000.00, 250.50]
    assert out["type"].tolist() == ["credit", "debit"]


def test_a_row_with_neither_debit_nor_credit_becomes_missing_not_zero():
    """
    Zero would sail through every tolerance check as a satisfied settlement.
    NaN is reported as `source_value_missing`, which is the truth.
    """
    df = pd.DataFrame([{"Withdrawal Amt.": "", "Deposit Amt.": ""}])
    out = merge_split_amount(df, "Withdrawal Amt.", "Deposit Amt.")
    assert pd.isna(out["amount"].iloc[0])


# ------------------------------------------------------------- applying it

def test_applying_a_mapping_renames_and_drops_the_rest():
    """
    A stray column literally called `amount` must not be left lying around for
    the pipeline to pick up by name after the user mapped amount elsewhere.
    """
    df = pd.DataFrame([{"Order Ref": "o1", "Total": 100.0, "amount": 999.0,
                        "Voucher": "v1", "When": "2026-08-01", "Notes": "x"}])
    out = apply_mapping(df, {"order_id": "Order Ref", "amount": "Total",
                             "ledger_id": "Voucher", "date": "When"})
    assert set(out.columns) == {"order_id", "amount", "ledger_id", "date"}
    assert out["amount"].iloc[0] == 100.0
    assert "Notes" not in out.columns
