"""
Idempotent/resumable run state.

Keyed by a stable object identifier (e.g. "SalesDW.dbo.usp_LoadOrdersFromStaging"
or "SSISDB.DiscoveryPilot.Pkg_Master"), we store a content fingerprint
(modify_date + definition hash for SQL objects, file mtime + content hash
for .dtsx) from the last successful extraction. On re-run, objects whose
fingerprint is unchanged are skipped entirely (logged as
skipped_unchanged) instead of being re-parsed -- this is what makes
discovery safe to re-run on a large estate and supports incremental
re-scan of changed objects only.

Backed by SQLite for durability across runs; at pilot/estate scale this
is more than sufficient and avoids standing up external infrastructure
just to track discovery state.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class StateStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS object_fingerprints (
                    object_id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    last_run_id TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    objects_scanned INTEGER DEFAULT 0,
                    objects_skipped_unchanged INTEGER DEFAULT 0,
                    objects_failed INTEGER DEFAULT 0
                )
                """
            )

    def has_changed(self, object_id: str, fingerprint: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM object_fingerprints WHERE object_id = ?",
                (object_id,),
            ).fetchone()
        return row is None or row[0] != fingerprint

    def record_fingerprint(self, object_id: str, object_type: str, fingerprint: str, run_id: str, now_iso: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO object_fingerprints (object_id, object_type, fingerprint, last_run_id, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(object_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    last_run_id = excluded.last_run_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (object_id, object_type, fingerprint, run_id, now_iso),
            )

    @contextmanager
    def run(self, run_id: str, now_iso: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at) VALUES (?, ?)",
                (run_id, now_iso),
            )
        counters = {"scanned": 0, "skipped_unchanged": 0, "failed": 0}
        try:
            yield counters
        finally:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE runs SET finished_at = ?, objects_scanned = ?,
                        objects_skipped_unchanged = ?, objects_failed = ?
                    WHERE run_id = ?
                    """,
                    (now_iso, counters["scanned"], counters["skipped_unchanged"], counters["failed"], run_id),
                )
