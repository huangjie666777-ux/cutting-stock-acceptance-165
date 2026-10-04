"""SQLite persistence for sampling batches.

Single connection guarded by a lock; every multi-statement write runs in
one transaction so a failure never leaves half-written state. Concurrent
creation of the same batch is settled by the PRIMARY KEY: exactly one
insert wins, the loser re-reads the stored row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    acceptance INTEGER NOT NULL,
    conclusion TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    demand_key TEXT NOT NULL,
    demand_id_json TEXT NOT NULL,
    instance INTEGER NOT NULL,
    nominal_um INTEGER NOT NULL,
    lower_dev_um INTEGER NOT NULL,
    upper_dev_um INTEGER NOT NULL,
    measured_um INTEGER,
    defective INTEGER,
    PRIMARY KEY (batch_id, demand_key, instance)
);
"""


class BatchStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    def insert_batch(self, batch_id, content_hash, response, sample_size, acceptance, samples):
        """Atomically persist a new batch. Returns (outcome, stored_response).

        outcome: 'created' | 'existing' (same content) | 'conflict' (different content).
        """
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO batches (batch_id, content_hash, response_json, sample_size, acceptance)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (batch_id, content_hash, json.dumps(response), sample_size, acceptance),
                    )
                    self._conn.executemany(
                        "INSERT INTO samples"
                        " (batch_id, demand_key, demand_id_json, instance, nominal_um, lower_dev_um, upper_dev_um)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                batch_id,
                                s["demand_key"],
                                json.dumps(s["demand_id"]),
                                s["instance"],
                                s["nominal_um"],
                                s["lower_dev_um"],
                                s["upper_dev_um"],
                            )
                            for s in samples
                        ],
                    )
                return "created", response
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT content_hash, response_json FROM batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                if row is None:  # pragma: no cover - defensive
                    raise
                stored = json.loads(row["response_json"])
                if row["content_hash"] == content_hash:
                    return "existing", stored
                return "conflict", None

    def get_batch(self, batch_id) -> Optional[dict]:
        with self._lock:
            batch = self._conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                return None
            samples = self._conn.execute(
                "SELECT * FROM samples WHERE batch_id = ? ORDER BY demand_key, instance",
                (batch_id,),
            ).fetchall()
        return self._view(batch, samples)

    @staticmethod
    def _view(batch, samples) -> dict:
        return {
            "batch_id": batch["batch_id"],
            "conclusion": batch["conclusion"],
            "sample_size": batch["sample_size"],
            "acceptance": batch["acceptance"],
            "plan": json.loads(batch["response_json"]),
            "samples": [
                {
                    "demand_id": json.loads(s["demand_id_json"]),
                    "instance": s["instance"],
                    "nominal_um": s["nominal_um"],
                    "lower_dev_um": s["lower_dev_um"],
                    "upper_dev_um": s["upper_dev_um"],
                    "measured_um": s["measured_um"],
                    "defective": None if s["defective"] is None else bool(s["defective"]),
                }
                for s in samples
            ],
        }

    def record_measurement(self, batch_id, demand_key, instance, measured_um):
        """Record one measurement. Returns (outcome, conclusion_or_None).

        outcome: 'recorded' | 'idempotent' | 'conflict' | 'not_sample' | 'batch_missing'
        """
        with self._lock:
            with self._conn:
                batch = self._conn.execute(
                    "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                if batch is None:
                    return "batch_missing", None
                row = self._conn.execute(
                    "SELECT * FROM samples WHERE batch_id = ? AND demand_key = ? AND instance = ?",
                    (batch_id, demand_key, instance),
                ).fetchone()
                if row is None:
                    return "not_sample", None
                if row["measured_um"] is not None:
                    if row["measured_um"] == measured_um:
                        return "idempotent", batch["conclusion"]
                    return "conflict", None
                nominal = row["nominal_um"]
                in_tolerance = nominal + row["lower_dev_um"] <= measured_um <= nominal + row["upper_dev_um"]
                cur = self._conn.execute(
                    "UPDATE samples SET measured_um = ?, defective = ?"
                    " WHERE batch_id = ? AND demand_key = ? AND instance = ? AND measured_um IS NULL",
                    (measured_um, 0 if in_tolerance else 1, batch_id, demand_key, instance),
                )
                if cur.rowcount == 0:  # lost a concurrent race; treat as conflict
                    return "conflict", None
                remaining = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM samples WHERE batch_id = ? AND measured_um IS NULL",
                    (batch_id,),
                ).fetchone()["c"]
                conclusion = None
                if remaining == 0:
                    defects = self._conn.execute(
                        "SELECT COUNT(*) AS c FROM samples WHERE batch_id = ? AND defective = 1",
                        (batch_id,),
                    ).fetchone()["c"]
                    conclusion = "ACCEPT" if defects <= batch["acceptance"] else "REJECT"
                    self._conn.execute(
                        "UPDATE batches SET conclusion = ? WHERE batch_id = ? AND conclusion IS NULL",
                        (conclusion, batch_id),
                    )
                return "recorded", conclusion
