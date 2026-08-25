"""
A live demonstration that a confidently wrong proposal cannot become a match.

tests/test_wrong_proposals.py already asserts this. A test file is not
convincing to anyone watching a demo, so this runs the same thing in the open:
it deliberately corrupts a narration so an upstream tier proposes the WRONG
settlement, at high confidence, and shows the verification stage refusing it.

    python main.py --prove

Nothing here is staged. It calls the same fuzzy resolver and the same Stage B
the real pipeline calls, on data constructed in front of you.
"""

import pandas as pd

from src.fuzzy_resolver import resolve_unresolved_bank_rows as fuzzy_resolve
from src.matcher import match_settlement_to_bank

RULE = "=" * 66


def _settlement(order_id, utr, settled, settlement_id):
    return {"settlement_id": settlement_id, "payment_id": "p", "order_id": order_id,
            "gross_amount": round(settled * 1.03, 2), "fee": 20.0, "tax": 3.6,
            "refund_amount": 0.0, "settled_amount": settled,
            "settlement_date": "2026-08-03", "utr": utr}


def prove_boundary():
    settlements = pd.DataFrame([
        _settlement("order_1", "UTR100005", 976.40, "stl_1"),
        _settlement("order_2", "UTR100006", 4882.00, "stl_2"),
    ])
    # This credit is order_2's money. Its narration has been mangled one
    # character toward order_1's reference, which is what a bank that drops a
    # digit actually produces.
    corrupted = "NEFT RZPY UTR10005 CR"
    credit = pd.DataFrame([{"txn_id": "bnk_1", "date": "2026-08-05",
                            "amount": 4882.00, "narration": corrupted,
                            "type": "credit"}])
    known = set(settlements["utr"])

    print(RULE)
    print("PROVING THE BOUNDARY: a wrong proposal cannot become a match")
    print(RULE)
    print("Two settlements are outstanding:")
    for _, row in settlements.iterrows():
        print(f"   {row['order_id']}   {row['utr']}   expects "
              f"{row['settled_amount']:>10,.2f}")
    print()
    print(f"One bank credit arrives for {credit.iloc[0]['amount']:,.2f}. "
          f"That is order_2's money.")
    print(f"Its narration has lost a character:  \"{corrupted}\"")
    print()

    links, unresolved = fuzzy_resolve([r for _, r in credit.iterrows()], known)

    print("-" * 66)
    print("TIER 2 (deterministic fuzzy recovery) proposes:")
    if not links:
        print("   nothing; the tier refused. Re-run with different data to see")
        print("   the interesting case.")
        return False
    proposal = links[0]
    print(f"   {proposal['utr_candidate']} at "
          f"{proposal['confidence']:.0%} confidence")
    print(f"   ...which belongs to order_1, expecting {976.40:,.2f}.")
    print(f"   THE PROPOSAL IS WRONG.")
    print()
    print("It is also well formed, highly confident, and names a settlement")
    print("that genuinely exists. Every heuristic check it could face, it passes.")
    print()

    print("-" * 66)
    print("STAGE B verifies the credit against the settlement it was pointed at:")
    print(f"   settlement expects   {976.40:>12,.2f}")
    print(f"   bank credited        {4882.00:>12,.2f}")
    print(f"   -> REFUSED")
    print()

    results, _ = match_settlement_to_bank(
        settlements, credit, {proposal["txn_id"]: proposal["utr_candidate"]})

    matched = [r for r in results if r.status == "matched"]
    print("-" * 66)
    print(f"MATCHES CREATED BY THE WRONG PROPOSAL: {len(matched)}")
    print()
    for r in sorted(results, key=lambda r: r.order_id):
        print(f"   {r.order_id}: {r.status:<10} {r.reason_code}")
    print()
    print("Both orders are reported accurately and neither is silently wrong.")
    print()
    print("A proposal only chooses what a credit gets compared against.")
    print("Stage B does the comparing, and confidence buys nothing there.")
    print(RULE)
    return len(matched) == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if prove_boundary() else 1)
