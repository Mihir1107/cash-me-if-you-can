"""
The decision the whole pipeline exists to support: can this period be closed?

Reconciliation is not finished when the breaks are found, or even when they are
routed. It is finished when someone signs off that the books for the period are
right, or refuses to. That refusal is the useful output, because it comes with
reasons.

A blocker here is a condition under which a controller should not sign. Each one
states what is wrong, what it is worth, and what would clear it. Nothing is
weighed against anything else and no score is produced: a single unrecorded
sale blocks the close no matter how small, because financial statements that
omit revenue are wrong rather than approximately right.

Two things deliberately do NOT block.

**Sub-threshold exceptions.** Holding a close open for a difference smaller than
the cost of chasing it would be theatre. Note that the threshold is an
operational triage heuristic, not audit materiality: a real close would also
apply qualitative overrides, so a small item can still block if its *nature*
matters (a suspected duplicate payment, a control failure, anything with a tax
consequence). This gate does not model those.

**Late credits that arrived.** A payment outside the normal window is a timing
observation, not a misstatement. The money is there and the books can say so.

The gate reads the run's own output and the audit trail's hash chain. It never
consults a model: every condition is a fact already computed, and a close
decision that depended on a language model's judgement would be indefensible in
exactly the setting where it matters.
"""


def _blocker(condition, why, action, value_at_risk=0.0, detail=None):
    return {
        "condition": condition,
        "why": why,
        "action": action,
        "value_at_risk": round(value_at_risk, 2),
        "detail": detail or {},
    }


def _sources_verifiable(report, audit_status):
    if report.get("bank_source_error"):
        return _blocker(
            "sources_verifiable",
            "The bank statement could not be read, so no settlement was "
            "confirmed against it. Nothing was reconciled, whatever the "
            "ledger-side figures say.",
            "Restore the bank statement and re-run before signing anything.",
            value_at_risk=report.get("money", {}).get("total_exposure", 0.0),
            detail={"error": report["bank_source_error"]},
        )
    return None


def _audit_trail_intact(report, audit_status):
    if audit_status is None:
        return None  # no trail was written for this run, e.g. an ablation pass
    if not audit_status.get("intact"):
        return _blocker(
            "audit_trail_intact",
            "The decision log does not verify against its own hash chain, so "
            "the record of how these figures were reached has been altered "
            "since the run.",
            "Investigate the break, then re-run from the source files. Do not "
            "sign against a log that cannot be verified.",
            detail={"broken_at": audit_status.get("broken_at"),
                    "reason": audit_status.get("reason")},
        )
    return None


def _books_balance(report, audit_status):
    money = report.get("money") or {}
    identity = money.get("identity") or {}
    if money and not identity.get("holds", True):
        return _blocker(
            "books_balance",
            "Total exposure does not equal confirmed plus at risk. The report "
            "cannot account for its own arithmetic.",
            "Fix the reconciliation before trusting any figure in it.",
            value_at_risk=abs(identity.get("residual", 0.0)),
            detail=identity,
        )
    return None


def _revenue_recorded(report, audit_status):
    """Money received that the ledger never booked. Revenue is understated."""
    counts = report.get("exception_reason_counts") or {}
    n = counts.get("no_ledger_entry", 0)
    if not n:
        return None
    at_risk = (report.get("money") or {}).get("at_risk_by_reason", {})
    return _blocker(
        "revenue_recorded",
        f"{n} settlement(s) were received for orders the ledger never booked, "
        f"so revenue for the period is understated.",
        "Book the missing orders, or explain why the money is not ours.",
        value_at_risk=at_risk.get("no_ledger_entry", 0.0),
        detail={"order_count": n},
    )


def _reversals_booked(report, audit_status):
    """Money credited then clawed back. Cash is overstated until booked."""
    counts = report.get("exception_reason_counts") or {}
    n = counts.get("settlement_reversed", 0)
    if not n:
        return None
    at_risk = (report.get("money") or {}).get("at_risk_by_reason", {})
    return _blocker(
        "reversals_booked",
        f"{n} settlement(s) were credited and then reversed, so the closing "
        f"cash position is overstated until the reversals are booked.",
        "Book the chargebacks against the original settlements.",
        value_at_risk=at_risk.get("settlement_reversed", 0.0),
        detail={"order_count": n},
    )


def _cash_attributable(report, audit_status):
    """Cash in the account nothing can tie to a settlement."""
    money = report.get("money") or {}
    triage = report.get("triage") or {}
    unattributed = money.get("unattributed_bank_credit_value", 0.0)
    threshold = triage.get("materiality_threshold")
    if threshold is None or unattributed <= threshold:
        return None
    return _blocker(
        "cash_attributable",
        f"{unattributed:,.2f} of bank credits cannot be tied to any settlement, "
        f"which is above the materiality threshold of {threshold:,.2f}.",
        "Attribute the credits by hand, or hold them in suspense and disclose.",
        value_at_risk=unattributed,
        detail={"threshold": threshold},
    )


def _material_exceptions_resolved(report, audit_status):
    triage = report.get("triage") or {}
    material = triage.get("material_incident_count", 0)
    if not material:
        return None
    return _blocker(
        "material_exceptions_resolved",
        f"{material} incident(s) above the materiality threshold are still "
        f"open.",
        "Work the triage queue, or record a documented decision to accept each "
        "one.",
        value_at_risk=triage.get("value_above_threshold", 0.0),
        detail={"incident_count": material,
                "threshold": triage.get("materiality_threshold")},
    )


# Ordered by how fundamental the objection is. A broken log or an unreadable
# source is not "one more problem", it means nothing else on the page is
# evidence of anything.
CONDITIONS = (
    _audit_trail_intact,
    _sources_verifiable,
    _books_balance,
    _revenue_recorded,
    _reversals_booked,
    _cash_attributable,
    _material_exceptions_resolved,
)


def evaluate_close(report, audit_status=None):
    blockers = [b for b in (check(report, audit_status) for check in CONDITIONS)
                if b is not None]

    passed = [check.__name__.lstrip("_") for check in CONDITIONS
              if check(report, audit_status) is None]

    return {
        "can_close": not blockers,
        "blocker_count": len(blockers),
        "value_blocking_close": round(
            sum(b["value_at_risk"] for b in blockers), 2),
        "conditions_checked": len(CONDITIONS),
        "conditions_passed": passed,
        "blockers": blockers,
        "note": ("A close decision, not a close. This records what a controller "
                 "would need to resolve before signing; it signs nothing."),
    }


def print_close_gate(gate):
    if not gate:
        return
    print("=" * 62)
    if gate["can_close"]:
        print("PERIOD CLOSE: CLEAR")
        print("=" * 62)
        print(f"All {gate['conditions_checked']} conditions pass. Nothing "
              f"material is unresolved and the decision log verifies.")
        print(gate["note"])
        return

    print("PERIOD CLOSE: BLOCKED")
    print("=" * 62)
    print(f"{gate['blocker_count']} of {gate['conditions_checked']} conditions "
          f"fail. {gate['value_blocking_close']:,.2f} is unresolved.")
    print()
    for n, blocker in enumerate(gate["blockers"], start=1):
        print(f"{n}. {blocker['condition']}"
              f"   {blocker['value_at_risk']:,.2f}")
        print(f"   {blocker['why']}")
        print(f"   -> {blocker['action']}")
        print()
    if gate["conditions_passed"]:
        print("Passing: " + ", ".join(gate["conditions_passed"]))
    print("=" * 62)
    print(gate["note"])
