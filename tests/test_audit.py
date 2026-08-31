"""
The audit trail, tested directly.

This module carries one of the project's load-bearing claims -- that a figure's
derivation can be checked rather than taken on trust -- and until now it was
only exercised sideways, through end-to-end runs that never tampered with
anything. A tamper-evident log that is never tampered with in the test suite is
an untested claim.

So every one of these tests attacks a written log and asserts the specific
thing verify_chain says about it. Where a claim is narrower than it sounds, the
test says so: this is tamper-*evident*, not tamper-proof, and the last test
pins that honestly rather than pretending otherwise.
"""

import json

import pytest

from src.audit import (GENESIS, AuditTrail, audit_summary, entry_digest,
                       new_run_id, read_chain_tip, verify_chain)


@pytest.fixture
def log(tmp_path):
    """A three-entry chain, honestly written."""
    path = tmp_path / "audit.jsonl"
    trail = AuditTrail(run_id="run_test_000001")
    trail.log("order_1", "ledger_settlement", "matched", "footing agrees")
    trail.log("order_2", "settlement_bank", "exception", "no credit found", 0.9)
    trail.log("order_3", "settlement_bank", "matched", "UTR match", 1.0)
    trail.save(str(path))
    return path


def lines(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def rewrite(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


# ----------------------------------------------------------- the happy path

def test_an_untouched_chain_verifies(log):
    status = verify_chain(str(log))
    assert status["intact"]
    assert status["verified"] == 3
    assert status["broken_at"] is None
    assert status["unverifiable_prefix"] == 0


def test_the_first_entry_anchors_to_genesis(log):
    assert lines(log)[0]["prev_hash"] == GENESIS


def test_each_entry_carries_the_hash_of_the_one_before(log):
    entries = lines(log)
    for earlier, later in zip(entries, entries[1:]):
        assert later["prev_hash"] == earlier["entry_hash"]


# ------------------------------------------------------------- the attacks

def test_editing_one_word_is_caught_and_located(log):
    """The plainest attack: change a basis string and leave everything else."""
    entries = lines(log)
    entries[1]["basis"] = "credit found, all fine"
    rewrite(log, entries)

    status = verify_chain(str(log))
    assert not status["intact"]
    assert status["broken_at"] == 2
    assert "edited" in status["reason"]


def test_flipping_an_exception_to_matched_is_caught(log):
    """
    The attack that actually matters. Anyone rewriting this log is rewriting it
    to say the books were clean.
    """
    entries = lines(log)
    entries[1]["decision"] = "matched"
    rewrite(log, entries)

    status = verify_chain(str(log))
    assert not status["intact"]
    assert status["broken_at"] == 2


def test_deleting_an_entry_is_caught(log):
    entries = lines(log)
    del entries[1]
    rewrite(log, entries)

    status = verify_chain(str(log))
    assert not status["intact"]
    assert status["broken_at"] == 2
    assert "deleted, reordered, or inserted" in status["reason"]


def test_reordering_entries_is_caught(log):
    entries = lines(log)
    entries[1], entries[2] = entries[2], entries[1]
    rewrite(log, entries)

    status = verify_chain(str(log))
    assert not status["intact"]


def test_truncating_the_tail_is_NOT_caught(log):
    """
    An honest limitation, pinned so nobody claims otherwise. Dropping entries
    from the *end* leaves a shorter but internally consistent chain. Detecting
    it needs an external anchor -- a countersigned tip, a witness -- which this
    does not have.
    """
    entries = lines(log)
    rewrite(log, entries[:2])

    assert verify_chain(str(log))["intact"] is True


def test_a_recomputed_chain_is_NOT_caught(log):
    """
    Tamper-evident, not tamper-proof, stated as a test rather than a footnote.
    Anyone who can rewrite the file can also recompute every digest. What the
    chain proves is that the log was not quietly adjusted between the run and
    the review, which is the question an auditor actually asks.
    """
    entries = lines(log)
    entries[1]["decision"] = "matched"

    prev = GENESIS
    for entry in entries:
        entry["prev_hash"] = prev
        entry["entry_hash"] = entry_digest(entry, prev)
        prev = entry["entry_hash"]
    rewrite(log, entries)

    assert verify_chain(str(log))["intact"] is True


# ----------------------------------------------- malformed and legacy files

def test_a_corrupt_line_is_reported_as_corrupt_not_as_tampering(log):
    log.write_text(log.read_text() + "{not json at all\n")
    status = verify_chain(str(log))
    assert not status["intact"]
    assert status["broken_at"] == 4
    assert "not valid JSON" in status["reason"]


def test_entries_predating_the_chain_are_unverifiable_not_tampered(tmp_path):
    """
    Different claims, and conflating them would cry wolf on every upgrade. A
    log written before hashing existed is not evidence of wrongdoing.
    """
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({"run_id": "old", "stage": "x", "decision": "y"}) + "\n")

    trail = AuditTrail(run_id="run_new")
    trail.log("order_1", "ledger_settlement", "matched", "footing agrees")
    trail.save(str(path))

    status = verify_chain(str(path))
    assert status["intact"]
    assert status["unverifiable_prefix"] == 1
    assert "predate hashing" in status["reason"]


def test_stripping_a_hash_from_the_middle_is_caught(log):
    """The obvious way around the legacy allowance, and it is closed."""
    entries = lines(log)
    del entries[1]["entry_hash"]
    rewrite(log, entries)

    status = verify_chain(str(log))
    assert not status["intact"]
    assert status["broken_at"] == 2
    assert "digest was removed" in status["reason"]


def test_a_missing_file_is_not_intact(tmp_path):
    status = verify_chain(str(tmp_path / "nothing.jsonl"))
    assert not status["intact"]
    assert status["entries"] == 0
    assert "no audit trail" in status["reason"]


# --------------------------------------------------------- appending safely

def test_a_second_run_continues_the_same_chain(log):
    second = AuditTrail(run_id="run_test_000002")
    second.log("order_4", "ledger_settlement", "matched", "footing agrees")
    second.save(str(log))

    status = verify_chain(str(log))
    assert status["intact"]
    assert status["verified"] == 4


def test_runs_stay_separable_in_a_shared_log(log):
    second = AuditTrail(run_id="run_test_000002")
    second.log("order_4", "ledger_settlement", "matched", "footing agrees")
    second.save(str(log))

    assert len(second.entries_for_run(str(log))) == 1
    assert {e["run_id"] for e in lines(log)} == {"run_test_000001", "run_test_000002"}


def test_read_chain_tip_survives_a_junk_last_line(log):
    log.write_text(log.read_text() + "garbage\n")
    assert read_chain_tip(str(log)) == GENESIS


def test_read_chain_tip_of_a_missing_file_is_genesis(tmp_path):
    assert read_chain_tip(str(tmp_path / "nothing.jsonl")) == GENESIS


def test_run_ids_are_unique(log):
    assert new_run_id() != new_run_id()


# -------------------------------------------------------------- the summary

def test_the_summary_names_the_broken_line(log):
    entries = lines(log)
    entries[0]["basis"] = "tampered"
    rewrite(log, entries)

    summary = audit_summary(verify_chain(str(log)))
    assert summary.startswith("BROKEN at line 1")


def test_the_summary_of_no_trail_says_so():
    assert "no audit trail" in audit_summary(None)
