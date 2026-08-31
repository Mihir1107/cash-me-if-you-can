"""
Reporting. The one rule here: an order counts as reconciled only if BOTH legs
confirmed it -- ledger<->settlement AND settlement<->bank.

An earlier version of this file scored the match rate off Stage A alone, which
meant five orders were counted as matched while simultaneously appearing in the
exception list, and a missing OPENAI_API_KEY cost the headline number
nothing. A match rate that can't lose points when verification fails isn't
measuring anything.
"""

import json

from src.money import build_money_report
from src.close_gate import print_close_gate
from src.triage import build_triage_report, print_triage


def build_report(total_orders, stage_a_results, stage_b_results,
                 fuzzy_links, llm_links, llm_exceptions, bank_error=None,
                 throughput=None, exposure_index=None, source_diagnostics=None):
    matched_a = {r.order_id for r in stage_a_results if r.status == "matched"}
    exceptions_a = [r for r in stage_a_results if r.status == "exception"]

    matched_b = {r.order_id for r in stage_b_results if r.status == "matched"}
    exceptions_b = [r for r in stage_b_results if r.status == "exception"]

    all_order_ids = {r.order_id for r in stage_a_results}

    # Three-way: the ledger agreed with the settlement AND the money arrived.
    reconciled = matched_a & matched_b
    unreconciled = all_order_ids - reconciled

    match_rate = round(100 * len(reconciled) / total_orders, 2) if total_orders else 0.0

    def as_record(r):
        return {
            "order_id": r.order_id, "stage": r.stage,
            "reason_code": r.reason_code, "basis": r.basis,
            "detail": r.detail,
        }

    # Order-level exceptions. An order can fail both legs and appear twice --
    # they are different problems with the same order, and unreconciled_orders
    # below is the de-duplicated count.
    exception_list = [as_record(r) for r in exceptions_a]
    exception_list += [as_record(r) for r in exceptions_b
                       if r.order_id not in matched_a]

    # An order that passed Stage A but failed Stage B is the case most worth
    # seeing: the books and Razorpay agree, and the bank disagrees with both.
    exception_list += [dict(as_record(r), also_matched_in_stage_a=True)
                       for r in exceptions_b if r.order_id in matched_a]

    # Bank credits no tier could attribute to a settlement. These are NOT
    # order-level exceptions -- they are unattributed money, listed separately
    # so they can't be confused with, or double-counted against, an order.
    unattributed = [{
        "txn_id": e["bank_row"]["txn_id"],
        "amount": float(e["bank_row"]["amount"]),
        "narration": e["bank_row"]["narration"],
        "reason_code": e["reason_code"],
        "basis": e["basis"],
    } for e in llm_exceptions]

    reason_counts = {}
    for e in exception_list:
        reason_counts[e["reason_code"]] = reason_counts.get(e["reason_code"], 0) + 1

    # Order counts answer "how many broke". Money answers "how much is at
    # stake", which is the question a controller actually asks.
    money = None
    triage = None
    if exposure_index is not None:
        money = build_money_report(exposure_index, reconciled, exception_list,
                                   unattributed)
        # Finding the breaks is half the loop. Routing them is the other half.
        triage = build_triage_report(exception_list, exposure_index,
                                     money["total_exposure"])

    return {
        "total_orders": total_orders,
        "reconciled_orders": len(reconciled),
        "unreconciled_orders": len(unreconciled),
        "match_rate_pct": match_rate,
        "match_rate_definition": (
            "orders confirmed on both legs: ledger<->settlement AND settlement<->bank"
        ),
        "stages": {
            "ledger_settlement": {
                "matched": len(matched_a), "exceptions": len(exceptions_a),
            },
            "settlement_bank": {
                "matched": len(matched_b), "exceptions": len(exceptions_b),
            },
        },
        "narration_resolution": {
            "resolved_by_fuzzy_no_llm": len(fuzzy_links),
            "resolved_by_llm": len(llm_links),
            "unresolved": len(llm_exceptions),
        },
        "money": money or {},
        "triage": triage or {},
        "throughput": throughput or {},
        "bank_source_error": bank_error,
        # Whether the three files actually describe the same money. Reported
        # whoever chose the columns -- a person who mapped them by hand still
        # wants to know if the ledger and the settlements share no order ids.
        "source_diagnostics": source_diagnostics or {},
        "exception_count": len(exception_list),
        "exception_reason_counts": reason_counts,
        "exceptions": exception_list,
        "unattributed_bank_credits": unattributed,
    }


def print_summary(report):
    print("=" * 62)
    print("RECONCILIATION REPORT")
    print("=" * 62)
    if report["bank_source_error"]:
        print(f"!! DEGRADED: {report['bank_source_error']}")
        print(f"!! Stage A results below are still valid; Stage B could not run.")
        print("-" * 62)

    stages = report["stages"]
    print(f"Total orders processed:            {report['total_orders']}")
    print(f"Ledger<->Settlement matched:       {stages['ledger_settlement']['matched']}")
    print(f"Ledger<->Settlement exceptions:    {stages['ledger_settlement']['exceptions']}")
    print(f"Settlement<->Bank matched:         {stages['settlement_bank']['matched']}")
    print(f"Settlement<->Bank exceptions:      {stages['settlement_bank']['exceptions']}")
    print("-" * 62)

    tp = report.get("throughput") or {}
    if tp:
        print(f"Wall clock:                        {tp['wall_clock_ms']:.1f} ms "
              f"({tp['records_per_second']:,.0f} records/sec over "
              f"{tp['records_processed']} records)")
        print(f"LLM calls made:                    {tp['llm_calls']} "
              f"({tp['llm_calls_per_100_orders']} per 100 orders)")
        slowest = sorted(tp.get("stage_ms", {}).items(), key=lambda kv: -kv[1])[:3]
        if slowest:
            print("  slowest stages: " + ",  ".join(f"{k} {v:.1f}ms" for k, v in slowest))
        print("-" * 62)

    tiers = report["narration_resolution"]
    print("Bank narration resolution by tier:")
    print(f"  recovered by fuzzy match (no LLM call): {tiers['resolved_by_fuzzy_no_llm']}")
    print(f"  resolved by LLM:                        {tiers['resolved_by_llm']}")
    print(f"  unresolved -> reported honestly:        {tiers['unresolved']}")
    print("-" * 62)
    print(f"MATCH RATE: {report['match_rate_pct']}%  "
          f"({report['reconciled_orders']}/{report['total_orders']} orders "
          f"confirmed on BOTH legs)")
    print(f"UNRECONCILED ORDERS: {report['unreconciled_orders']}")
    # An order can fail both legs and so carry two records. They are different
    # problems with the same order, and conflating them would either hide one
    # or double-count the order, so say plainly when the two numbers differ.
    extra = report["exception_count"] - report["unreconciled_orders"]
    suffix = (f"  ({extra} order(s) failed both legs)" if extra > 0 else "")
    print(f"EXCEPTION RECORDS:   {report['exception_count']}{suffix}")

    money = report.get("money") or {}
    if money:
        print("-" * 62)
        print("MONEY RECONCILED (what a controller actually asks)")
        print(f"  Total exposure:        {money['total_exposure']:>16,.2f}")
        print(f"  Confirmed in bank:     {money['confirmed_value']:>16,.2f}  "
              f"({money['value_match_rate_pct']}% by value)")
        print(f"  AT RISK:               {money['at_risk_value']:>16,.2f}")
        for reason, value in money["at_risk_by_reason"].items():
            print(f"    {reason:<32}{value:>14,.2f}")
        if money["unattributed_bank_credit_value"]:
            print(f"  Unattributed credits:  "
                  f"{money['unattributed_bank_credit_value']:>16,.2f}  "
                  f"(cash held, not placeable)")
        identity = money["identity"]
        if identity["holds"]:
            print(f"  [OK] {identity['statement']}  (residual "
                  f"{identity['residual']:.2f})")
        else:
            print(f"  [!!] IDENTITY BROKEN: {identity['statement']} — residual "
                  f"{identity['residual']:.2f}. Report cannot be trusted.")
    print("=" * 62)

    print("\nException breakdown (reason_code : count):")
    for code, n in sorted(report["exception_reason_counts"].items(), key=lambda x: -x[1]):
        print(f"  {code:30s} {n}")

    if report.get("triage"):
        print()
        print_triage(report["triage"])

    if report.get("close_gate"):
        print()
        print_close_gate(report["close_gate"])

    if report["unattributed_bank_credits"]:
        print(f"\nUnattributed bank credits: {len(report['unattributed_bank_credits'])}")
        for c in report["unattributed_bank_credits"]:
            print(f"  {c['txn_id']}  {c['amount']:>12,.2f}  {c['narration'][:52]}")


def save_report(report, path):
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
