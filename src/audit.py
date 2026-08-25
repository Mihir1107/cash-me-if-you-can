"""
Append-only, tamper-evident decision log.

Every decision any stage makes lands here with a timestamp, the basis for it,
and a confidence. Three properties make it an audit trail rather than a debug
log.

**It appends.** An earlier version opened the file with mode "w" and truncated
it on every run, quietly destroying the record it existed to keep. A controller
reconstructing why an order was flagged last Tuesday needs last Tuesday's lines
to still be there.

**Runs stay separable.** Every entry carries a run_id, so appending never blurs
one batch into the next.

**It is tamper-evident.** Each entry carries the SHA-256 of the entry before it,
so the file is a hash chain. Editing a single character of a single basis, or
deleting a line, or reordering two, breaks every link from that point on and
verify_chain() reports the first entry that does not check out.

That last property is the difference between claiming the exception list is
honest and being able to prove nobody edited it afterwards. It is not
cryptographic security -- anyone who can rewrite the file can recompute the
whole chain -- it is what an auditor actually wants, which is evidence that the
log has not been quietly adjusted between the run and the review.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

GENESIS = "0" * 64
HASH_FIELDS = ("prev_hash", "entry_hash")


def new_run_id():
    """Sortable, unique, and readable at a glance in the log."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def _canonical(entry):
    """
    Stable bytes for an entry, excluding its own hash fields. Sorted keys and
    fixed separators, so a re-serialised entry hashes identically.
    """
    payload = {k: v for k, v in entry.items() if k not in HASH_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode()


def entry_digest(entry, prev_hash):
    return hashlib.sha256(prev_hash.encode() + _canonical(entry)).hexdigest()


def read_chain_tip(path):
    """
    The last entry's hash, so a new run continues the existing chain rather than
    starting a parallel one. A missing or empty file starts at genesis.
    """
    try:
        with open(path) as f:
            last = None
            for line in f:
                if line.strip():
                    last = line
    except FileNotFoundError:
        return GENESIS
    if last is None:
        return GENESIS
    try:
        return json.loads(last).get("entry_hash", GENESIS)
    except json.JSONDecodeError:
        return GENESIS


def audit_summary(status):
    """One line a human can read off a report."""
    if not status:
        return "no audit trail written for this run"
    if not status.get("intact"):
        return f"BROKEN at line {status.get('broken_at')}: {status.get('reason')}"
    return status.get("reason", "verified")


def verify_chain(path):
    """
    Walk the file and check every link. Returns
    {"intact", "entries", "verified", "unverifiable_prefix", "broken_at",
     "reason"}.

    broken_at is the 1-based line number of the first entry that does not check
    out, which is where a reviewer should look, not merely a yes/no.

    A log written before hashing existed carries no digests. Those leading
    entries are counted and reported as unverifiable rather than called
    tampering, because they are different claims and conflating them would cry
    wolf on every upgrade. Stripping hashes from the *middle* is not a way in:
    the next hashed entry's prev_hash no longer matches, and that is caught.
    """
    prev = GENESIS
    verified = 0
    unverifiable_prefix = 0
    started = False
    try:
        lines = [l for l in open(path).read().splitlines() if l.strip()]
    except FileNotFoundError:
        return {"intact": False, "entries": 0, "verified": 0,
                "unverifiable_prefix": 0, "broken_at": None,
                "reason": f"no audit trail at {path}"}

    for number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            return {"intact": False, "entries": len(lines), "verified": verified,
                    "unverifiable_prefix": unverifiable_prefix,
                    "broken_at": number,
                    "reason": f"line {number} is not valid JSON: {e}"}

        if "entry_hash" not in entry:
            if not started:
                unverifiable_prefix += 1
                continue
            return {"intact": False, "entries": len(lines), "verified": verified,
                    "unverifiable_prefix": unverifiable_prefix,
                    "broken_at": number,
                    "reason": (f"line {number} carries no hash but follows "
                               f"entries that do, so a digest was removed")}

        if not started:
            # first hashed entry: it anchors the chain from here on
            started = True
            prev = entry.get("prev_hash", GENESIS)

        if entry.get("prev_hash") != prev:
            return {"intact": False, "entries": len(lines), "verified": verified,
                    "unverifiable_prefix": unverifiable_prefix,
                    "broken_at": number,
                    "reason": (f"line {number} does not follow the entry before "
                               f"it: an entry was deleted, reordered, or inserted")}

        expected = entry_digest(entry, prev)
        if entry["entry_hash"] != expected:
            return {"intact": False, "entries": len(lines), "verified": verified,
                    "unverifiable_prefix": unverifiable_prefix,
                    "broken_at": number,
                    "reason": (f"line {number} has been edited since it was "
                               f"written: its contents no longer hash to its "
                               f"recorded digest")}

        prev = entry["entry_hash"]
        verified += 1

    reason = f"all {verified} hashed entries verify against the chain"
    if unverifiable_prefix:
        reason += (f"; the first {unverifiable_prefix} predate hashing and "
                   f"cannot be verified either way")
    return {"intact": True, "entries": len(lines), "verified": verified,
            "unverifiable_prefix": unverifiable_prefix, "broken_at": None,
            "reason": reason}


class AuditTrail:
    def __init__(self, run_id=None):
        self.run_id = run_id or new_run_id()
        self.entries = []

    def log(self, order_id, stage, decision, basis, confidence=1.0, detail=None):
        self.entries.append({
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "stage": stage,
            "decision": decision,      # matched | exception | degraded | link_proposed
            "basis": basis,
            "confidence": confidence,
            "detail": detail or {},
        })

    def log_match_result(self, mr):
        self.log(mr.order_id, mr.stage, mr.status, mr.basis, mr.confidence, mr.detail)

    def save(self, path, append=True):
        """
        Append by default, continuing the existing hash chain. Pass append=False
        only when you genuinely intend to discard prior history; nothing in the
        pipeline does, and doing so restarts the chain from genesis.
        """
        prev = read_chain_tip(path) if append else GENESIS
        with open(path, "a" if append else "w") as f:
            for entry in self.entries:
                entry["prev_hash"] = prev
                entry["entry_hash"] = entry_digest(entry, prev)
                prev = entry["entry_hash"]
                f.write(json.dumps(entry, default=str) + "\n")

    def entries_for_run(self, path):
        """Read back just this run's lines from a log that may hold many."""
        with open(path) as f:
            return [e for e in (json.loads(line) for line in f if line.strip())
                    if e.get("run_id") == self.run_id]
