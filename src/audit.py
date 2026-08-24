import json
from datetime import datetime, timezone


class AuditTrail:
    def __init__(self):
        self.entries = []

    def log(self, order_id, stage, decision, basis, confidence=1.0, detail=None):
        self.entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "stage": stage,
            "decision": decision,      # matched | exception
            "basis": basis,
            "confidence": confidence,
            "detail": detail or {},
        })

    def log_match_result(self, mr):
        self.log(mr.order_id, mr.stage, mr.status, mr.basis, mr.confidence, mr.detail)

    def save(self, path):
        with open(path, "w") as f:
            for entry in self.entries:
                f.write(json.dumps(entry) + "\n")
