"""
Scores the pipeline against the answer key the generator wrote at injection time
(`data/ground_truth.csv`), which the pipeline itself never reads.

Why this exists: a match rate on its own is unfalsifiable. 49% could mean the
matcher found every real problem, or that it is flagging healthy orders and
missing broken ones. Ground truth turns it into a claim that can be checked --
per reason code, with the false positives named.

Also runs a tier ablation, so the value of each narration-resolution tier is a
measured number rather than an assertion.

    python -m src.evaluate
"""

import json
import math
import os

import pandas as pd

Z_95 = 1.959963984540054  # two-sided 95%

from src.reconcile import run_reconciliation

MATCHED = "matched"


def wilson_interval(successes, trials, z=Z_95):
    """
    Wilson score interval for a proportion.

    Used here on detection rates and NOT on the match rate, and the distinction
    is the point. The match rate is a census: every order in the batch was
    checked, so 24/57 is exactly the answer with no sampling error to interval.
    Putting a confidence band on it would imply an uncertainty that does not
    exist.

    Recall and precision are different. They estimate how this agent would
    behave on faults it has not seen, from the 32 that happened to be injected.
    32 out of 32 is a perfect score on a small sample, and the honest reading is
    "consistent with a true detection rate above about 89%", not "proven 100%".
    Wilson rather than the normal approximation because the latter produces
    intervals that include impossible values exactly at the boundaries where
    these results sit.
    """
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def predicted_labels(report):
    """order_id -> the reason code the pipeline assigned, or 'matched'."""
    labels = {}
    for e in report["exceptions"]:
        oid = e["order_id"]
        # An order can fail both legs; the ledger-side finding is the root cause,
        # so it wins. Deterministic either way -- never dict-order dependent.
        if oid not in labels or e["stage"] == "ledger_settlement":
            labels[oid] = e["reason_code"]
    return labels


def score(truth_df, report):
    predicted = predicted_labels(report)

    # An answer key with the same order twice has no answer for that order, and
    # zipping it into a dict silently keeps whichever row came last. That is how
    # a generator bug hid: raising N_ORDERS past the pinned unbooked order
    # numbers settled orders the ledger HAD booked, so two truth rows disagreed
    # about the same order and the score quietly reported against one of them.
    # An unusable source is an exception here, exactly as it is in the pipeline.
    duplicates = truth_df["order_id"][truth_df["order_id"].duplicated()].unique()
    if len(duplicates):
        raise ValueError(
            f"ground truth names {len(duplicates)} order(s) more than once "
            f"(e.g. {', '.join(map(str, duplicates[:3]))}); the answer key is "
            f"ambiguous and cannot be scored against")

    expected = dict(zip(truth_df["order_id"], truth_df["expected_reason_code"]))

    pairs = [(exp, predicted.get(oid, MATCHED)) for oid, exp in expected.items()]
    labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})

    per_label = {}
    for label in labels:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        fp = sum(1 for e, p in pairs if e != label and p == label)
        fn = sum(1 for e, p in pairs if e == label and p != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {
            "support": tp + fn, "predicted": tp + fp,
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    # Binary view: did we notice something was wrong, regardless of how we labelled it?
    faults_tp = sum(1 for e, p in pairs if e != MATCHED and p != MATCHED)
    faults_fp = sum(1 for e, p in pairs if e == MATCHED and p != MATCHED)
    faults_fn = sum(1 for e, p in pairs if e != MATCHED and p == MATCHED)
    detect_p = faults_tp / (faults_tp + faults_fp) if faults_tp + faults_fp else 0.0
    detect_r = faults_tp / (faults_tp + faults_fn) if faults_tp + faults_fn else 0.0

    confusion = {}
    for exp, pred in pairs:
        confusion.setdefault(exp, {}).setdefault(pred, 0)
        confusion[exp][pred] += 1

    misclassified = sorted(
        [{"order_id": oid, "expected": exp, "predicted": predicted.get(oid, MATCHED)}
         for oid, exp in expected.items() if predicted.get(oid, MATCHED) != exp],
        key=lambda r: r["order_id"],
    )

    return {
        "orders_scored": len(pairs),
        "exact_label_accuracy": round(
            sum(1 for e, p in pairs if e == p) / len(pairs), 4) if pairs else 0.0,
        "fault_detection": {
            "injected_faults": faults_tp + faults_fn,
            "detected": faults_tp,
            "false_positives": faults_fp,
            "missed": faults_fn,
            "precision": round(detect_p, 4),
            "recall": round(detect_r, 4),
            # what this sample of faults implies about unseen ones
            "recall_ci95": wilson_interval(faults_tp, faults_tp + faults_fn),
            "precision_ci95": wilson_interval(faults_tp, faults_tp + faults_fp),
            "sample_note": ("95% Wilson intervals. These are estimates from the "
                            "injected faults; the match rate is a census and "
                            "carries no interval."),
        },
        "per_reason_code": per_label,
        "confusion_matrix": confusion,
        "misclassified": misclassified,
    }


def ablation(data_dir="data"):
    """What each narration tier is actually worth, measured on the same batch."""
    configs = [
        ("regex only", dict(enable_fuzzy=False, enable_llm=False)),
        ("+ fuzzy (still no LLM call)", dict(enable_fuzzy=True, enable_llm=False)),
        ("+ LLM tier", dict(enable_fuzzy=True, enable_llm=True)),
    ]
    has_key = bool(os.environ.get("OPENAI_API_KEY"))

    rows = []
    for name, kwargs in configs:
        if kwargs["enable_llm"] and not has_key:
            rows.append({"config": name, "skipped": "no OPENAI_API_KEY set"})
            continue
        report, _ = run_reconciliation(data_dir=data_dir, write_outputs=False, **kwargs)
        rows.append({
            "config": name,
            "match_rate_pct": report["match_rate_pct"],
            "reconciled_orders": report["reconciled_orders"],
            "resolved_by_fuzzy": report["narration_resolution"]["resolved_by_fuzzy_no_llm"],
            "resolved_by_llm": report["narration_resolution"]["resolved_by_llm"],
            "unresolved_narrations": report["narration_resolution"]["unresolved"],
        })
    return rows


def print_evaluation(result, ablation_rows):
    print("=" * 74)
    print("EVALUATION AGAINST GROUND TRUTH")
    print("=" * 74)
    fd = result["fault_detection"]
    print(f"Orders scored:            {result['orders_scored']}")
    print(f"Injected faults:          {fd['injected_faults']}")
    print(f"  detected:               {fd['detected']}")
    print(f"  missed (false neg):     {fd['missed']}")
    print(f"  false positives:        {fd['false_positives']}")
    lo_r, hi_r = fd.get("recall_ci95", (0.0, 0.0))
    lo_p, hi_p = fd.get("precision_ci95", (0.0, 0.0))
    print(f"Fault detection precision {fd['precision']:.2%}  "
          f"[95% CI {lo_p:.1%}-{hi_p:.1%}]")
    print(f"Fault detection recall    {fd['recall']:.2%}  "
          f"[95% CI {lo_r:.1%}-{hi_r:.1%}]")
    print(f"  ({fd['injected_faults']} injected faults; the match rate is a "
          f"census and carries no interval)")
    print(f"Exact reason-code accuracy: {result['exact_label_accuracy']:.2%}")
    print("-" * 74)
    # 30 fits the longest code, ledger_gross_amount_mismatch, without pushing
    # the rest of its row out of alignment.
    print(f"{'reason_code':<30}{'support':>8}{'prec':>9}{'recall':>9}{'F1':>8}{'FP':>5}{'FN':>5}")
    for code, m in sorted(result["per_reason_code"].items(),
                          key=lambda kv: (-kv[1]["support"], kv[0])):
        print(f"{code:<30}{m['support']:>8}{m['precision']:>9.2%}"
              f"{m['recall']:>9.2%}{m['f1']:>8.2f}"
              f"{m['false_positives']:>5}{m['false_negatives']:>5}")

    if result["misclassified"]:
        print("-" * 74)
        print("Misclassified orders (every one, no filtering):")
        for m in result["misclassified"]:
            print(f"  {m['order_id']}  expected={m['expected']:<30} got={m['predicted']}")

    print("-" * 74)
    print("TIER ABLATION — what each narration tier is worth on this batch")
    print(f"{'config':<30}{'match rate':>12}{'fuzzy':>8}{'llm':>6}{'unresolved':>12}")
    for row in ablation_rows:
        if "skipped" in row:
            print(f"{row['config']:<30}{'— ' + row['skipped']:>38}")
            continue
        print(f"{row['config']:<30}{row['match_rate_pct']:>11.2f}%"
              f"{row['resolved_by_fuzzy']:>8}{row['resolved_by_llm']:>6}"
              f"{row['unresolved_narrations']:>12}")
    print("=" * 74)


def run_evaluation(data_dir="data", output_dir="output"):
    truth = pd.read_csv(f"{data_dir}/ground_truth.csv")
    report, _ = run_reconciliation(data_dir=data_dir, output_dir=output_dir)
    result = score(truth, report)
    ablation_rows = ablation(data_dir)

    payload = {
        "match_rate_pct": report["match_rate_pct"],
        "match_rate_definition": report["match_rate_definition"],
        "scoring": result,
        "tier_ablation": ablation_rows,
    }
    with open(f"{output_dir}/evaluation.json", "w") as f:
        json.dump(payload, f, indent=2)
    return payload, result, ablation_rows


if __name__ == "__main__":
    payload, result, ablation_rows = run_evaluation()
    print_evaluation(result, ablation_rows)
    print("\nFull evaluation: output/evaluation.json")
