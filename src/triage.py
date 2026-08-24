"""
Turns an exception list into a work queue.

Reconciliation does not end when a break is found. It ends when someone has
fixed it, and between those two points sits the work this module does: deciding
which breaks are the same problem, who owns each one, what the next action is,
and which are worth a human's time at all.

Three ideas from finance operations drive it.

**Incidents, not rows.** Five orders failing `fee_footing_mismatch` with the
same implied fee error are one misconfigured fee, not five tickets. Clustering
by root-cause signature collapses a queue of thirty-five rows into a handful of
incidents, and it is the difference between a list and a plan.

**Ownership.** The exception codes already encode who can act. A footing error
is Razorpay's to explain; an unreflected refund is the merchant's to book; a
delayed credit is the bank's to clear. Routing is a property of the code, so it
is a table, not a judgement.

**Materiality.** Auditors do not investigate every rupee, they set a threshold
below which a difference is not worth the cost of chasing. Incidents under it
are still reported, still counted, and still in the money identity. They are
just marked as not worth a human today, which is a recommendation a controller
can overrule, not a number that quietly disappears.

Everything here is deterministic. No model is consulted, because none is needed:
routing is a lookup, clustering is a signature, and priority is arithmetic on
money already computed. This is the same judgement as Tiers 1 and 2 of the
narration matcher, one layer up.

And like every other proposal in this pipeline, a remediation is a suggestion
with its evidence attached. Nothing here edits a ledger, posts an entry, or
closes anything. A human does that, holding the evidence pack this produces.
"""

MATERIALITY_PCT = 0.005      # 0.5% of total exposure
MATERIALITY_FLOOR = 1000.00  # ...but never below this, in rupees

# Who can actually fix each break, what the next action is, and how fast it
# needs attention. Urgency is about consequence, not size: unrecorded revenue is
# a reporting integrity problem at any amount, while a late credit is usually
# just late.
POLICY = {
    "no_ledger_entry": {
        "owner": "merchant_finance",
        "urgency": "critical",
        "action": "Book the missing order. Money was received that the ledger "
                  "cannot explain, so revenue is understated until it is recorded.",
        "cluster": "single",
    },
    "settlement_reversed": {
        "owner": "chargeback_ops",
        "urgency": "critical",
        "action": "Treat as a chargeback. The credit was taken back, so the cash "
                  "position is overstated until the reversal is booked.",
        "cluster": "per_order",
    },
    "settlement_not_credited": {
        "owner": "razorpay_support",
        "urgency": "critical",
        "action": "Raise with Razorpay quoting the UTR. A payout was reported "
                  "and no matching credit exists in the statement.",
        "cluster": "single",
    },
    "fee_footing_mismatch": {
        "owner": "razorpay_support",
        "urgency": "high",
        "action": "Query the fee schedule. The settlement does not foot against "
                  "its own gross, fee, tax and refund.",
        "cluster": "fee_rate_error",
    },
    "duplicate_settlement": {
        "owner": "razorpay_support",
        "urgency": "high",
        "action": "Confirm which settlement row is authoritative before booking "
                  "either, so the order is not recognised twice.",
        "cluster": "single",
    },
    "bank_amount_mismatch": {
        "owner": "razorpay_support",
        "urgency": "high",
        "action": "Reconcile the payout against the credit. The bank moved a "
                  "different amount from the one the settlement reports.",
        "cluster": "shortfall_delta",
    },
    "refund_not_reflected": {
        "owner": "merchant_finance",
        "urgency": "high",
        "action": "Post the refund to the ledger. Revenue is overstated by the "
                  "refunded amount until it is booked.",
        "cluster": "single",
    },
    "ledger_gross_amount_mismatch": {
        "owner": "merchant_finance",
        "urgency": "medium",
        "action": "Correct the booked amount, or explain the difference. The "
                  "ledger and the settlement disagree on the sale value.",
        "cluster": "per_order",
    },
    "no_settlement_found": {
        "owner": "razorpay_support",
        "urgency": "medium",
        "action": "Check payment status. The order is booked as paid and no "
                  "settlement exists for it.",
        "cluster": "single",
    },
    "bank_credit_delayed": {
        "owner": "bank_ops",
        "urgency": "low",
        "action": "Monitor. The credit landed outside the normal window but did "
                  "land; chase only if this recurs for the same account.",
        "cluster": "delay_bucket",
    },
    "credit_unattributed": {
        "owner": "merchant_finance",
        "urgency": "medium",
        "action": "Attribute the credit by hand. The money is in the account; "
                  "only the narration is unreadable.",
        "cluster": "single",
    },
    "source_value_missing": {
        "owner": "data_engineering",
        "urgency": "high",
        "action": "Re-export the source. A value needed for verification is "
                  "missing or non-numeric, so nothing about this order is proven.",
        "cluster": "single",
    },
    "date_unparseable": {
        "owner": "data_engineering",
        "urgency": "medium",
        "action": "Re-export the source. A date could not be read, so the "
                  "settlement window could not be checked.",
        "cluster": "single",
    },
    "bank_credit_predates_settlement": {
        "owner": "data_engineering",
        "urgency": "high",
        "action": "Investigate the data lineage. A credit is dated before the "
                  "settlement that produced it, which cannot happen.",
        "cluster": "single",
    },
    "fee_exceeds_gross": {
        "owner": "razorpay_support",
        "urgency": "critical",
        "action": "Escalate. Fees and tax exceed the transaction value, so the "
                  "settlement nets negative against a completed sale.",
        "cluster": "single",
    },
    "duplicate_ledger_entry": {
        "owner": "merchant_finance",
        "urgency": "high",
        "action": "De-duplicate the ledger. One order is booked more than once, "
                  "so revenue is counted more than once.",
        "cluster": "single",
    },
    "bank_source_unavailable": {
        "owner": "data_engineering",
        "urgency": "critical",
        "action": "Restore the bank statement. Nothing on the bank leg could be "
                  "verified for any order in this batch.",
        "cluster": "single",
    },
}

URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

FALLBACK = {
    "owner": "unassigned",
    "urgency": "medium",
    "action": "No routing rule for this reason code. Triage by hand and add a "
              "policy entry so the next occurrence routes itself.",
    "cluster": "single",
}


def materiality_threshold(total_exposure):
    """A percentage of the book, floored so tiny books do not chase pennies."""
    return round(max(total_exposure * MATERIALITY_PCT, MATERIALITY_FLOOR), 2)


def _signature(exception, strategy):
    """
    What makes two exceptions the same problem. Deliberately coarse: the point
    is to collapse a queue, and a controller can always open an incident to see
    the individual orders inside it.
    """
    if strategy == "per_order":
        return exception["order_id"]

    if strategy == "fee_rate_error":
        # Two footing errors are the same root cause when the settlement is off
        # by the same proportion of the fee, which is what a misconfigured rate
        # looks like. Bucketed, since floating point will not agree exactly.
        detail = exception.get("detail") or {}
        fee, tax = detail.get("fee"), detail.get("tax")
        if not fee:
            return "unknown_fee_shape"
        return f"fee_ratio_{round((fee + (tax or 0.0)) / fee, 1)}"

    if strategy == "shortfall_delta":
        # Two payouts short by the same amount are one deduction being applied
        # twice, not two coincidences. Clustering on the delta surfaces that;
        # clustering per order would have hidden it as unrelated small change.
        detail = exception.get("detail") or {}
        settled, bank = detail.get("settled_total"), detail.get("bank_amount")
        if settled is None or bank is None:
            return "unknown_shortfall"
        return f"short_by_{round(settled - bank, 2)}"

    if strategy == "delay_bucket":
        detail = exception.get("detail") or {}
        gap = detail.get("date_gap_days")
        if gap is None:
            return "unknown_delay"
        return "delay_1w" if gap <= 7 else "delay_over_1w"

    return "all"


def build_incidents(exceptions, exposure_index, total_exposure):
    """
    Groups exceptions into incidents, ranks them, and attaches an action.
    Returns the list, most consequential first.
    """
    threshold = materiality_threshold(total_exposure)

    # One bucket per order so money is never counted twice, matching how the
    # money report attributes an order that fails both legs.
    bucket = {}
    for exc in exceptions:
        oid = exc["order_id"]
        if oid is None:
            continue
        if oid not in bucket or exc["stage"] == "ledger_settlement":
            bucket[oid] = exc

    groups = {}
    for oid, exc in bucket.items():
        code = exc["reason_code"]
        policy = POLICY.get(code, FALLBACK)
        key = (code, _signature(exc, policy["cluster"]))
        groups.setdefault(key, []).append((oid, exc))

    incidents = []
    for (code, signature), members in groups.items():
        policy = POLICY.get(code, FALLBACK)
        value = round(sum(exposure_index.get(oid, {}).get("exposure", 0.0)
                          for oid, _ in members), 2)
        order_ids = sorted(oid for oid, _ in members)
        incidents.append({
            "reason_code": code,
            "signature": signature,
            "owner": policy["owner"],
            "urgency": policy["urgency"],
            "recommended_action": policy["action"],
            "order_count": len(members),
            "value_at_risk": value,
            "material": value >= threshold,
            "order_ids": order_ids,
            "sample_basis": members[0][1]["basis"],
            "routed_by_policy": code in POLICY,
        })

    # Consequence first, then money. A critical incident outranks a larger
    # low-urgency one, because a late credit that arrives is not the same kind
    # of problem as revenue that was never recorded.
    incidents.sort(key=lambda i: (not i["material"],
                                  URGENCY_RANK.get(i["urgency"], 9),
                                  -i["value_at_risk"]))
    return incidents


def build_triage_report(exceptions, exposure_index, total_exposure):
    incidents = build_incidents(exceptions, exposure_index, total_exposure)
    threshold = materiality_threshold(total_exposure)
    material = [i for i in incidents if i["material"]]

    by_owner = {}
    for incident in incidents:
        entry = by_owner.setdefault(incident["owner"],
                                    {"incidents": 0, "orders": 0, "value_at_risk": 0.0})
        entry["incidents"] += 1
        entry["orders"] += incident["order_count"]
        entry["value_at_risk"] = round(entry["value_at_risk"] + incident["value_at_risk"], 2)

    return {
        "exception_rows": sum(i["order_count"] for i in incidents),
        "incident_count": len(incidents),
        "material_incident_count": len(material),
        "materiality_threshold": threshold,
        "materiality_basis": (f"{MATERIALITY_PCT:.1%} of total exposure, floored "
                              f"at {MATERIALITY_FLOOR:,.2f}"),
        "value_above_threshold": round(sum(i["value_at_risk"] for i in material), 2),
        "value_below_threshold": round(
            sum(i["value_at_risk"] for i in incidents if not i["material"]), 2),
        "by_owner": dict(sorted(by_owner.items(),
                                key=lambda kv: -kv[1]["value_at_risk"])),
        "incidents": incidents,
        "note": ("Recommendations only. Nothing here edits a ledger, posts an "
                 "entry, or closes an exception."),
    }


def print_triage(triage):
    if not triage or not triage.get("incidents"):
        return
    print("=" * 62)
    print("TRIAGE: what to do about it")
    print("=" * 62)
    print(f"{triage['exception_rows']} exception rows cluster into "
          f"{triage['incident_count']} incidents, "
          f"{triage['material_incident_count']} above the materiality threshold "
          f"of {triage['materiality_threshold']:,.2f}")
    print(f"  ({triage['materiality_basis']})")
    print()
    print(f"{'owner':<20}{'incidents':>10}{'orders':>8}{'value at risk':>16}")
    for owner, stats in triage["by_owner"].items():
        print(f"{owner:<20}{stats['incidents']:>10}{stats['orders']:>8}"
              f"{stats['value_at_risk']:>16,.2f}")
    def show(n, incident):
        print(f"\n{n}. [{incident['urgency'].upper()}] {incident['reason_code']}"
              f"  {incident['value_at_risk']:,.2f} across "
              f"{incident['order_count']} order(s)")
        print(f"   owner:  {incident['owner']}")
        print(f"   action: {incident['recommended_action']}")
        shown = ", ".join(incident["order_ids"][:4])
        more = (f" (+{len(incident['order_ids']) - 4} more)"
                if len(incident["order_ids"]) > 4 else "")
        print(f"   orders: {shown}{more}")

    material = [i for i in triage["incidents"] if i["material"]]
    immaterial = [i for i in triage["incidents"] if not i["material"]]

    print("-" * 62)
    print("Work queue, most consequential first:")
    for n, incident in enumerate(material, start=1):
        show(n, incident)

    # Kept in a section of their own. Materiality outranks urgency, so mixing
    # these into the numbered queue puts a HIGH below a LOW and reads as a
    # sorting bug rather than as the deliberate judgement it is.
    if immaterial:
        print()
        print("-" * 62)
        print(f"Below the materiality threshold of "
              f"{triage['materiality_threshold']:,.2f}, "
              f"{triage['value_below_threshold']:,.2f} in total.")
        print("Still reported, still counted, still inside the money identity.")
        print("Not worth a controller's day unless they decide otherwise.")
        for n, incident in enumerate(immaterial, start=len(material) + 1):
            show(n, incident)

    print("=" * 62)
    print(triage["note"])
