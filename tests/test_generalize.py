"""
Generalization: does the matcher work on conventions it was not developed on?

The primary batch and this one share nothing but a CSV schema. Different
reference formats (no "UTR" prefix anywhere, some references purely numeric),
a different fee model, a T+1 settlement cadence instead of T+2, four unfamiliar
narration templates, and a different order-id scheme.

Not one line of src/ changes to run it. If the matcher were quietly keyed to
the primary batch's conventions, detection would collapse here -- which is
precisely the failure this file exists to rule out.
"""

import pandas as pd
import pytest

from src.evaluate import score
from src.reconcile import run_reconciliation

ALT_DIR = "data/alt"


@pytest.fixture(scope="module")
def alt_result(tmp_path_factory):
    out = tmp_path_factory.mktemp("alt")
    report, _ = run_reconciliation(data_dir=ALT_DIR, output_dir=str(out))
    truth = pd.read_csv(f"{ALT_DIR}/ground_truth.csv")
    return report, score(truth, report)


def test_no_fault_is_missed_on_unfamiliar_conventions(alt_result):
    _, result = alt_result
    detection = result["fault_detection"]
    assert detection["injected_faults"] > 0
    assert detection["missed"] == 0
    assert detection["recall"] == 1.0


def test_no_healthy_order_is_falsely_flagged(alt_result):
    _, result = alt_result
    assert result["fault_detection"]["false_positives"] == 0
    assert result["fault_detection"]["precision"] == 1.0


def test_every_fault_gets_the_right_reason_code(alt_result):
    """Detection alone is not enough -- the root cause must be named correctly."""
    _, result = alt_result
    assert result["exact_label_accuracy"] == 1.0
    assert result["misclassified"] == []


def test_references_without_a_utr_prefix_still_resolve(alt_result):
    """
    The alt batch quotes RRN/IMPS/AXIS references and bare numeric ones. If
    extraction depended on the literal string "UTR", nothing here would match.
    """
    report, _ = alt_result
    assert report["stages"]["settlement_bank"]["matched"] > 0
    assert report["narration_resolution"]["unresolved"] == 0

    bank = pd.read_csv(f"{ALT_DIR}/bank_statement.csv")
    assert not bank["narration"].str.contains("UTR").any()


def test_money_identity_holds_on_the_alt_batch(alt_result):
    report, _ = alt_result
    assert report["money"]["identity"]["holds"]
    assert report["money"]["total_exposure"] > 0
