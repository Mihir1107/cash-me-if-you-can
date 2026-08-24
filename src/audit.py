"""
Append-only decision log.

Every decision any stage makes -- matched, exception, degraded, or a link
proposed by a later tier -- lands here with a timestamp, the basis for it, and
a confidence. Two properties make it an audit trail rather than a debug log:

  * It is opened in append mode. An earlier version truncated the file on every
    run, which quietly destroyed the record it existed to keep. A controller
    reconstructing why an order was flagged last Tuesday needs last Tuesday's
    lines to still be there.
  * Every entry carries a run_id, so appending never blurs one run into the
    next. Filter by run_id to reconstruct a single batch exactly.
"""

import json
import uuid
from datetime import datetime, timezone


def new_run_id():
    """Sortable, unique, and readable at a glance in the log."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


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
        Append by default. Pass append=False only when you genuinely intend to
        discard prior history -- nothing in the pipeline does.
        """
        with open(path, "a" if append else "w") as f:
            for entry in self.entries:
                f.write(json.dumps(entry) + "\n")

    def entries_for_run(self, path):
        """Read back just this run's lines from a log that may hold many."""
        with open(path) as f:
            return [e for e in (json.loads(line) for line in f if line.strip())
                    if e.get("run_id") == self.run_id]
