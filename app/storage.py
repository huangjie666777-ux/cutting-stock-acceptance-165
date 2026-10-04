"""SQLite persistence for acceptance batches: lot, fixed sample, measurements.

A single connection guarded by one lock serializes writers; every mutation
runs inside BEGIN IMMEDIATE so concurrent creates/measurements either commit
once or fail without leaving partial state.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import threading
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

DB_PATH = os.environ.get("BATCH_DB_PATH", "batches.db")

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id      TEXT PRIMARY KEY,
            content_hash  TEXT NOT NULL,
            request_json  TEXT NOT NULL,
            tolerances_json TEXT NOT NULL,
            layouts_json  TEXT NOT NULL,
            metrics_json  TEXT,
            status        TEXT NOT NULL,
            n_total       INTEGER NOT NULL,
            dg            INTEGER NOT NULL,
            dbad          INTEGER NOT NULL,
            alpha         TEXT NOT NULL,
            beta          TEXT NOT NULL,
            sample_n      INTEGER NOT NULL,
            accept_c      INTEGER NOT NULL,
            producer_risk TEXT NOT NULL,
            consumer_risk TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS samples (
            batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
            demand_id  TEXT NOT NULL,
            instance   INTEGER NOT NULL,
            sample_pos INTEGER NOT NULL,
            nominal_um INTEGER NOT NULL,
            lower_dev  INTEGER NOT NULL,
            upper_dev  INTEGER NOT NULL,
            PRIMARY KEY (batch_id, demand_id, instance)
        );
        CREATE TABLE IF NOT EXISTS measurements (
            batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
            demand_id   TEXT NOT NULL,
            instance    INTEGER NOT NULL,
            measured_um INTEGER NOT NULL,
            defective   INTEGER NOT NULL,
            PRIMARY KEY (batch_id, demand_id, instance),
            FOREIGN KEY (batch_id, demand_id, instance)
                REFERENCES samples(batch_id, demand_id, instance)
        );
        """
    )
    return conn


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _connect()
    return _conn


def reset_for_tests(path: str) -> None:
    """Close and reopen the database at a new path (tests only)."""
    global _conn, DB_PATH
    with _lock:
        if _conn is not None:
            _conn.close()
        DB_PATH = path
        if os.path.exists(path):
            os.remove(path)
        _conn = _connect()


def reset_connection_for_restart(path: Optional[str] = None) -> None:
    """Close and reopen the connection against the same (or given) DB file."""
    global _conn, DB_PATH
    with _lock:
        if _conn is not None:
            _conn.close()
        if path is not None:
            DB_PATH = path
        _conn = _connect()


def _id_key(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def content_hash(request_dict: dict, tolerances_dict: dict, params: Optional[dict] = None) -> str:
    blob = json.dumps(
        {"request": request_dict, "tolerances": tolerances_dict, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def lot_instances(request_dict: dict, layouts: List[dict]) -> List[Tuple[object, int, int]]:
    """All finished pieces as (demand_id, instance, nominal_um), in frozen order."""
    result: List[Tuple[object, int, int]] = []
    for layout in layouts:
        for cut in layout["cuts"]:
            result.append((cut["demand_id"], cut["instance"], cut["length"] * 1000))
    return result


def choose_sample(
    batch_id: str, chash: str, instances: List[Tuple[object, int, int]], n: int
) -> List[Tuple[object, int, int]]:
    """Deterministic uniform sampling without replacement, fixed once chosen."""
    seed_blob = hashlib.sha256(f"{_id_key(batch_id)}|{chash}".encode("utf-8")).digest()
    seed_int = int.from_bytes(seed_blob, "big")
    rng = random.Random(seed_int)
    return rng.sample(instances, n)


class ContentMismatch(Exception):
    pass


def create_batch(
    batch_id,
    request_dict: dict,
    tolerances_dict: dict,
    layouts: List[dict],
    metrics: Optional[dict],
    dg: int,
    dbad: int,
    alpha: Decimal,
    beta: Decimal,
    sample_n: int,
    accept_c: int,
    producer_risk: Decimal,
    consumer_risk: Decimal,
) -> Tuple[dict, bool]:
    """Create a batch; returns (row, created). Same content replays the batch."""
    chash = content_hash(
        request_dict,
        tolerances_dict,
        {"dg": dg, "db": dbad, "alpha": str(alpha), "beta": str(beta)},
    )
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (_id_key(batch_id),)
            ).fetchone()
            created = False
            if row is not None:
                if row["content_hash"] != chash:
                    raise ContentMismatch(f"batch {batch_id!r} already exists with different content")
                conn.execute("COMMIT")
                return dict(row), created

            instances = lot_instances(request_dict, layouts)
            chosen = choose_sample(batch_id, chash, instances, sample_n)
            tol = {_id_key(k): v for k, v in tolerances_dict.items()}
            conn.execute(
                """INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _id_key(batch_id), chash,
                    json.dumps(request_dict, ensure_ascii=False),
                    json.dumps(tolerances_dict, ensure_ascii=False),
                    json.dumps(layouts, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False) if metrics is not None else None,
                    "PENDING", len(instances), dg, dbad, str(alpha), str(beta),
                    sample_n, accept_c, str(producer_risk), str(consumer_risk),
                ),
            )
            for pos, (demand_id, instance, nominal_um) in enumerate(chosen, start=1):
                t = tol[_id_key(demand_id)]
                conn.execute(
                    "INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                    (_id_key(batch_id), _id_key(demand_id), instance, pos,
                     nominal_um, t["lower"], t["upper"]),
                )
            conn.execute("COMMIT")
            created = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (_id_key(batch_id),)).fetchone()
        return dict(row), created


class MeasurementConflict(Exception):
    pass


class NotSampled(Exception):
    pass


def record_measurement(batch_id, demand_id, instance: int, measured_um: int) -> dict:
    conn = get_conn()
    bid, did = _id_key(batch_id), _id_key(demand_id)
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            brow = conn.execute("SELECT * FROM batches WHERE batch_id=?", (bid,)).fetchone()
            if brow is None:
                raise KeyError(f"unknown batch {batch_id!r}")
            srow = conn.execute(
                "SELECT * FROM samples WHERE batch_id=? AND demand_id=? AND instance=?",
                (bid, did, instance),
            ).fetchone()
            if srow is None:
                raise NotSampled(f"piece {demand_id!r}#{instance} is not in the fixed sample")
            existing = conn.execute(
                "SELECT * FROM measurements WHERE batch_id=? AND demand_id=? AND instance=?",
                (bid, did, instance),
            ).fetchone()
            if existing is not None:
                if existing["measured_um"] != measured_um:
                    raise MeasurementConflict(
                        f"piece already measured as {existing['measured_um']} um; "
                        f"cannot overwrite with {measured_um} um"
                    )
            else:
                defective = not (
                    srow["nominal_um"] + srow["lower_dev"]
                    <= measured_um
                    <= srow["nominal_um"] + srow["upper_dev"]
                )
                conn.execute(
                    "INSERT INTO measurements VALUES (?,?,?,?,?)",
                    (bid, did, instance, measured_um, 1 if defective else 0),
                )

            measured = conn.execute(
                "SELECT COUNT(*) AS k FROM measurements WHERE batch_id=?", (bid,)
            ).fetchone()["k"]
            defects = conn.execute(
                "SELECT COALESCE(SUM(defective),0) AS d FROM measurements WHERE batch_id=?",
                (bid,),
            ).fetchone()["d"]
            n_total, c = brow["sample_n"], brow["accept_c"]
            if measured < n_total:
                status = "PENDING"
                verdict = None
            else:
                verdict = "ACCEPTED" if defects <= c else "REJECTED"
                status = verdict
                conn.execute("UPDATE batches SET status=? WHERE batch_id=?", (status, bid))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return get_batch(batch_id)


def get_batch(batch_id) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM batches WHERE batch_id=?", (_id_key(batch_id),)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    samples = conn.execute(
        "SELECT * FROM samples WHERE batch_id=? ORDER BY sample_pos", (out["batch_id"],)
    ).fetchall()
    measures = {
        (r["demand_id"], r["instance"]): r
        for r in conn.execute(
            "SELECT * FROM measurements WHERE batch_id=?", (out["batch_id"],)
        ).fetchall()
    }
    sample_rows = []
    defect_count = 0
    for s in samples:
        m = measures.get((s["demand_id"], s["instance"]))
        item = {
            "demand_id": json.loads(s["demand_id"]),
            "instance": s["instance"],
            "sample_position": s["sample_pos"],
            "nominal_length_um": s["nominal_um"],
            "lower_deviation_um": s["lower_dev"],
            "upper_deviation_um": s["upper_dev"],
            "measured": m is not None,
        }
        if m is not None:
            item["measured_length_um"] = m["measured_um"]
            item["defective"] = bool(m["defective"])
            defect_count += m["defective"]
        sample_rows.append(item)
    out["batch_id"] = json.loads(out["batch_id"])
    out["samples"] = sample_rows
    out["measured_count"] = len(measures)
    out["defect_count"] = defect_count
    out["verdict"] = None if out["status"] == "PENDING" else out["status"]
    return out


def list_batches() -> List[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT batch_id, status, n_total, sample_n FROM batches").fetchall()
    return [
        {
            "batch_id": json.loads(r["batch_id"]),
            "status": r["status"],
            "lot_size": r["n_total"],
            "sample_size": r["sample_n"],
        }
        for r in rows
    ]
