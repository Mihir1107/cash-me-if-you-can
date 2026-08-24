import pandas as pd

from src.matcher import (
    BANK_DATE_WINDOW_DAYS,
    link_bank_rows,
    match_ledger_to_settlement,
    match_settlement_to_bank,
)

LEDGER_COLS = ["ledger_id", "order_id", "customer", "amount", "date", "status"]
STL_COLS = ["settlement_id", "payment_id", "order_id", "gross_amount", "fee", "tax",
            "refund_amount", "settled_amount", "settlement_date", "utr"]


def ledger(order_id="order_1", amount=1000.0, status="paid"):
    return {"ledger_id": f"led_{order_id}", "order_id": order_id, "customer": "c1",
            "amount": amount, "date": "2026-08-01", "status": status}


def settlement(order_id="order_1", gross=1000.0, fee=20.0, tax=3.6,
               refund=0.0, settled=None, utr="UTR900001", date="2026-08-03",
               settlement_id="stl_1"):
    if settled is None:
        settled = round(gross - fee - tax - refund, 2)
    return {"settlement_id": settlement_id, "payment_id": "pay_1", "order_id": order_id,
            "gross_amount": gross, "fee": fee, "tax": tax, "refund_amount": refund,
            "settled_amount": settled, "settlement_date": date, "utr": utr}


def bank(txn_id="bnk_1", date="2026-08-05", amount=976.4, narration=None, utr="UTR900001"):
    if narration is None:
        narration = f"RAZORPAY SETTLEMENT UTR:{utr} ORD:order_1"
    return {"txn_id": txn_id, "date": date, "amount": amount,
            "narration": narration, "type": "credit"}


def codes(results, status="exception"):
    return [r.reason_code for r in results if r.status == status]


# ---------------------------------------------------------------- Stage A

def test_clean_match():
    results, settled_ids = match_ledger_to_settlement(
        pd.DataFrame([ledger()]), pd.DataFrame([settlement()]))
    assert len(results) == 1
    assert results[0].status == "matched"
    assert "order_1" in settled_ids


def test_fee_footing_mismatch_caught():
    results, settled_ids = match_ledger_to_settlement(
        pd.DataFrame([ledger()]), pd.DataFrame([settlement(settled=900.0)]))
    assert results[0].status == "exception"
    assert results[0].reason_code == "fee_footing_mismatch"
    assert settled_ids == set()


def test_no_settlement_found():
    results, _ = match_ledger_to_settlement(
        pd.DataFrame([ledger(order_id="order_orphan", amount=500.0)]),
        pd.DataFrame(columns=STL_COLS))
    assert results[0].status == "exception"
    assert results[0].reason_code == "no_settlement_found"


def test_duplicate_settlement_flagged_not_double_counted():
    results, settled_ids = match_ledger_to_settlement(
        pd.DataFrame([ledger()]),
        pd.DataFrame([settlement(settlement_id="stl_1"),
                      settlement(settlement_id="stl_2", utr="UTR900002")]))
    assert results[0].status == "exception"
    assert results[0].reason_code == "duplicate_settlement"
    assert settled_ids == set()  # a flagged duplicate must never count as matched


def test_refund_not_reflected_gets_its_own_reason_code():
    """Settlement foots correctly once the refund is counted; the ledger never took it."""
    results, settled_ids = match_ledger_to_settlement(
        pd.DataFrame([ledger(amount=1000.0, status="paid")]),      # still books gross
        pd.DataFrame([settlement(refund=300.0)]))                  # 300 refunded
    assert results[0].status == "exception"
    assert results[0].reason_code == "refund_not_reflected"
    assert results[0].detail["refund_amount"] == 300.0
    assert settled_ids == set()


def test_refund_correctly_reflected_still_matches():
    """Control: the rule must flag a ledger gap, not merely the presence of a refund."""
    by_amount, ids_a = match_ledger_to_settlement(
        pd.DataFrame([ledger(amount=700.0)]),                      # books net
        pd.DataFrame([settlement(refund=300.0)]))
    assert by_amount[0].status == "matched"
    assert ids_a == {"order_1"}

    by_status, ids_b = match_ledger_to_settlement(
        pd.DataFrame([ledger(amount=1000.0, status="partially_refunded")]),
        pd.DataFrame([settlement(refund=300.0)]))
    assert by_status[0].status == "matched"
    assert ids_b == {"order_1"}


def test_refund_not_reflected_is_distinct_from_footing_error():
    """A settlement that both carries a refund and miscomputes fees is a footing error."""
    results, _ = match_ledger_to_settlement(
        pd.DataFrame([ledger()]),
        pd.DataFrame([settlement(refund=300.0, settled=600.0)]))  # doesn't foot
    assert results[0].reason_code == "fee_footing_mismatch"


def test_stage_a_tolerates_sources_without_refund_column():
    legacy = {k: v for k, v in settlement().items() if k != "refund_amount"}
    results, _ = match_ledger_to_settlement(
        pd.DataFrame([ledger()]), pd.DataFrame([legacy]))
    assert results[0].status == "matched"


# ---------------------------------------------------------------- Stage B

def test_bank_utr_match_within_window():
    results, unresolved = match_settlement_to_bank(
        pd.DataFrame([settlement()]), pd.DataFrame([bank()]))
    assert results[0].status == "matched"
    assert unresolved == []


def test_bank_amount_mismatch_caught():
    results, _ = match_settlement_to_bank(
        pd.DataFrame([settlement()]), pd.DataFrame([bank(amount=900.0)]))
    assert results[0].status == "exception"
    assert results[0].reason_code == "bank_amount_mismatch"


def test_mangled_narration_is_never_matched_on_amount_alone():
    """
    The bank credit is for EXACTLY the settled amount, and the matcher still
    refuses to call it a match, because the narration proves nothing. It is
    flagged as an unattributed credit and handed to the later tiers.
    """
    results, unresolved = match_settlement_to_bank(
        pd.DataFrame([settlement(settled=976.4)]),
        pd.DataFrame([bank(amount=976.4, narration="NEFT-RZPY-900001/settlemnt")]))
    assert len(unresolved) == 1
    assert not [r for r in results if r.status == "matched"]
    assert codes(results) == ["credit_unattributed"]
    assert results[0].detail["candidate_txn_ids"] == ["bnk_1"]
    assert "NOT matched on amount" in results[0].basis


def test_uncredited_and_unattributed_are_different_exceptions():
    """
    Money that never arrived is escalated to Razorpay; money that arrived under
    an unreadable narration is a parsing problem. Same symptom, opposite triage.
    """
    stl = pd.DataFrame([settlement(settled=976.4)])

    nothing_arrived, _ = match_settlement_to_bank(
        stl, pd.DataFrame(columns=list(bank().keys())))
    assert codes(nothing_arrived) == ["settlement_not_credited"]
    assert "money never arrived" in nothing_arrived[0].basis

    wrong_amount_arrived, _ = match_settlement_to_bank(
        stl, pd.DataFrame([bank(amount=12.0, narration="CR/ONLINE TRF/no ref")]))
    assert codes(wrong_amount_arrived) == ["settlement_not_credited"]


def test_settlement_with_no_bank_credit_is_reported():
    """Razorpay says it paid out; nothing ever arrived. The headline exception."""
    results, _ = match_settlement_to_bank(
        pd.DataFrame([settlement()]), pd.DataFrame(columns=list(bank().keys())))
    assert results[0].status == "exception"
    assert results[0].reason_code == "settlement_not_credited"


def test_bank_credit_delayed_fires_past_the_window_boundary():
    stl = pd.DataFrame([settlement(date="2026-08-03")])

    on_boundary = match_settlement_to_bank(stl, pd.DataFrame([bank(date="2026-08-08")]))[0]
    assert on_boundary[0].status == "matched"  # gap == window is still normal

    past = match_settlement_to_bank(stl, pd.DataFrame([bank(date="2026-08-09")]))[0]
    assert past[0].status == "exception"
    assert past[0].reason_code == "bank_credit_delayed"
    assert past[0].detail["date_gap_days"] == BANK_DATE_WINDOW_DAYS + 1


def test_many_to_one_batch_matches_against_the_batch_total():
    """Razorpay pays several settlements out under one UTR as a single credit."""
    settlements = pd.DataFrame([
        settlement(order_id="order_1", settlement_id="stl_1", settled=100.0, utr="UTR700007"),
        settlement(order_id="order_2", settlement_id="stl_2", settled=250.0, utr="UTR700007"),
        settlement(order_id="order_3", settlement_id="stl_3", settled=400.0, utr="UTR700007"),
    ])
    credit = pd.DataFrame([bank(amount=750.0,
                                narration="RAZORPAY SETTLEMENT UTR:UTR700007 BATCH OF 3")])
    results, _ = match_settlement_to_bank(settlements, credit)
    assert len(results) == 3
    assert all(r.status == "matched" for r in results)
    assert {r.order_id for r in results} == {"order_1", "order_2", "order_3"}


def test_batch_short_paid_flags_every_leg():
    settlements = pd.DataFrame([
        settlement(order_id="order_1", settlement_id="stl_1", settled=100.0, utr="UTR700007"),
        settlement(order_id="order_2", settlement_id="stl_2", settled=250.0, utr="UTR700007"),
    ])
    credit = pd.DataFrame([bank(amount=300.0,  # 50 short
                                narration="RAZORPAY SETTLEMENT UTR:UTR700007 BATCH")])
    results, _ = match_settlement_to_bank(settlements, credit)
    assert codes(results) == ["bank_amount_mismatch", "bank_amount_mismatch"]


def test_duplicate_settlement_row_is_not_mistaken_for_a_batch():
    """
    Two settlement rows for ONE order under one UTR is a duplicate, already
    reported by Stage A. Stage B must not double the expected total and blame
    the bank for a shortfall that never happened.
    """
    settlements = pd.DataFrame([
        settlement(settlement_id="stl_1"),
        settlement(settlement_id="stl_2"),  # same order_id, same utr, same amount
    ])
    results, _ = match_settlement_to_bank(settlements, pd.DataFrame([bank()]))
    assert [r.status for r in results] == ["matched"]
    assert "duplicate settlement row" in results[0].basis


# ------------------------------------------------- narration link boundary

def test_link_only_accepts_proposals_naming_a_real_settlement():
    """
    A proposal from a later tier is an input to verification, not a verdict.
    One that names a UTR no settlement carries is discarded outright.
    """
    bank_df = pd.DataFrame([bank(narration="CR/ONLINE TRF/no ref quoted")])

    by_utr, unresolved, _ = link_bank_rows(
        bank_df, {"UTR900001"}, extra_links={"bnk_1": "UTR_DOES_NOT_EXIST"})
    assert by_utr == {}
    assert len(unresolved) == 1

    by_utr, unresolved, _ = link_bank_rows(
        bank_df, {"UTR900001"}, extra_links={"bnk_1": "UTR900001"})
    assert list(by_utr) == ["UTR900001"]
    assert unresolved == []


# --------------------------------------------- indexed lookup (perf refactor)

def test_indexed_lookup_agrees_with_a_naive_scan():
    """
    The index exists for speed. It must not change a single verdict, so pin it
    against the obvious O(n) implementation it replaced.
    """
    from src.matcher import build_utr_index, extract_utr_from_narration

    known = {"UTR900001", "UTR900002", "UTR700007", "SBIN12345678"}
    index = build_utr_index(known)
    # each narration quotes at most one known UTR, so the naive scan has a
    # single unambiguous answer to agree with
    narrations = [
        "RAZORPAY SETTLEMENT UTR:UTR900001 ORD:order_1",
        "IMPS/cust/razorpay payout ref UTR700007",
        "NEFT-SBIN-12345678/credit",
        "CR/ONLINE TRF/no ref quoted",
        "",
    ]
    for narration in narrations:
        naive = next((u for u in sorted(known, key=len, reverse=True)
                      if u in "".join(ch for ch in narration.upper() if ch.isalnum())), None)
        assert extract_utr_from_narration(narration, index=index) == naive


def test_two_utrs_in_one_narration_are_refused_not_ranked():
    """
    A narration quoting a reversal ref and a credit ref contains two real UTRs.
    Substring matching finds both and has no basis for ranking them — picking
    the earlier one would confidently return the reversal. It must refuse and
    let a tier that can read the surrounding words decide.
    """
    from src.matcher import extract_utr_from_narration

    known = {"UTR900001", "UTR900002"}
    ambiguous = "RAZORPAY NET STLMT/DR RVSL REF UTR900001/CR REF UTR900002"
    assert extract_utr_from_narration(ambiguous, known) is None
    # ...but a single quoted reference still resolves
    assert extract_utr_from_narration("CR REF UTR900002 ONLY", known) == "UTR900002"


def test_a_nested_utr_is_not_counted_as_a_second_reference():
    """
    UTR9000 appears inside UTR90001234 as a substring artefact, not as a second
    reference. Position-claiming keeps that from reading as ambiguity.
    """
    from src.matcher import extract_utr_from_narration

    known = {"UTR9000", "UTR90001234"}
    assert extract_utr_from_narration("PAYOUT UTR90001234 CR", known) == "UTR90001234"


def test_shared_numeric_core_stays_ambiguous_after_indexing():
    """
    Two settlements can share reference digits. The index buckets cores into
    lists precisely so that stays ambiguous instead of silently resolving to
    whichever UTR happened to be indexed last.
    """
    from src.fuzzy_resolver import build_core_index, fuzzy_resolve_narration

    known = {"UTR100005", "XYZ100005"}          # same numeric core
    index = build_core_index(known)
    assert sorted(index[6]["100005"]) == ["UTR100005", "XYZ100005"]
    assert fuzzy_resolve_narration("NEFT-RZPY-100005/x", known, core_index=index) is None





def test_one_settlement_split_across_two_bank_credits_matches_on_the_sum():
    """
    One-to-many: the bank pays a single settlement out in two tranches under the
    same UTR. Neither leg matches the settlement alone; only their sum does.
    """
    settlements = pd.DataFrame([settlement(settled=1000.0)])
    credits = pd.DataFrame([
        bank(txn_id="bnk_1", amount=400.0, narration="RAZORPAY UTR:UTR900001 PART 1/2"),
        bank(txn_id="bnk_2", amount=600.0, narration="RAZORPAY UTR:UTR900001 PART 2/2"),
    ])
    results, _ = match_settlement_to_bank(settlements, credits)
    assert [r.status for r in results] == ["matched"]


def test_a_short_paid_split_still_fails_on_the_sum():
    """Splitting a payout must not become a way to hide a shortfall."""
    settlements = pd.DataFrame([settlement(settled=1000.0)])
    credits = pd.DataFrame([
        bank(txn_id="bnk_1", amount=400.0, narration="RAZORPAY UTR:UTR900001 PART 1/2"),
        bank(txn_id="bnk_2", amount=500.0, narration="RAZORPAY UTR:UTR900001 PART 2/2"),
    ])
    results, _ = match_settlement_to_bank(settlements, credits)
    assert codes(results) == ["bank_amount_mismatch"]



def test_a_debit_quoting_a_settlement_utr_is_never_linked_to_it():
    """Money leaving must not be read as the settlement arriving."""
    from src.matcher import link_bank_rows

    debit = pd.DataFrame([bank(narration="CHARGEBACK UTR:UTR900001", txn_id="bnk_d")])
    debit["type"] = "debit"
    by_utr, unresolved, non_credits = link_bank_rows(debit, {"UTR900001"})
    assert by_utr == {}
    assert unresolved == []
    assert [r["txn_id"] for r in non_credits] == ["bnk_d"]


def test_bank_credit_predating_the_settlement_is_flagged():
    """
    Only the late side of the window was ever checked, so a credit dated before
    the settlement matched cleanly. Money cannot arrive before it was sent.
    """
    stl_df = pd.DataFrame([settlement(date="2026-08-20")])
    results, _ = match_settlement_to_bank(stl_df, pd.DataFrame([bank(date="2026-08-01")]))
    assert codes(results) == ["bank_credit_predates_settlement"]
    assert results[0].detail["date_gap_days"] == -19

    # a same-day credit is still fine
    same_day, _ = match_settlement_to_bank(stl_df, pd.DataFrame([bank(date="2026-08-20")]))
    assert same_day[0].status == "matched"


def test_fees_exceeding_gross_are_flagged_even_though_the_footing_is_correct():
    """
    gross - fee - tax genuinely equals the negative settled_amount, so every
    arithmetic check in Stage A passes. It is still not a valid settlement.
    Found by the property suite rather than by hand.
    """
    results, settled_ids = match_ledger_to_settlement(
        pd.DataFrame([ledger(amount=1000.0)]),
        pd.DataFrame([settlement(gross=1000.0, fee=990.0, tax=178.2,
                                 settled=-168.2)]))
    assert results[0].reason_code == "fee_exceeds_gross"
    assert settled_ids == set()


def test_a_normal_fee_is_not_flagged_as_excessive():
    """Control: the rule must catch nonsense, not ordinary fees."""
    results, _ = match_ledger_to_settlement(
        pd.DataFrame([ledger()]), pd.DataFrame([settlement()]))
    assert results[0].status == "matched"
