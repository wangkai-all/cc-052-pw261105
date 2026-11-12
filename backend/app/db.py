"""SQLite persistence.

Write transactions use ``BEGIN IMMEDIATE`` so that cancel/publish decisions are
serialised by the database, in addition to the per-batch file lock.  The
database runs in WAL mode with ``synchronous=FULL`` so committed bookkeeping
survives an abrupt process exit.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    uid                 TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL UNIQUE,
    attempt             INTEGER NOT NULL DEFAULT 0,
    phase               TEXT NOT NULL
                        CHECK (phase IN ('preparing', 'file_ready', 'published')),
    publish_state       TEXT NOT NULL
                        CHECK (publish_state IN
                               ('collecting', 'ready', 'moving', 'committed')),
    content_digest      TEXT,
    publish_requested_at TEXT,
    cancelled_at        TEXT,
    failure_code        TEXT,
    failure_reason      TEXT,
    failed_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_phase ON batches(phase);

CREATE TABLE IF NOT EXISTS batch_declarations (
    batch_uid       TEXT NOT NULL REFERENCES batches(uid) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    rel_path        TEXT NOT NULL,
    declared_size   INTEGER NOT NULL,
    UNIQUE (batch_uid, attempt, rel_path)
);

CREATE TABLE IF NOT EXISTS batch_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uid   TEXT NOT NULL REFERENCES batches(uid) ON DELETE CASCADE,
    attempt     INTEGER NOT NULL DEFAULT 0,
    rel_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('staged', 'moved')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (batch_uid, attempt, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_batch_files_uid ON batch_files(batch_uid, attempt);

CREATE TABLE IF NOT EXISTS batch_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uid       TEXT NOT NULL REFERENCES batches(uid) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL DEFAULT 0,
    stage           TEXT NOT NULL
                    CHECK (stage IN ('staged', 'file_ready', 'moved', 'published')),
    content_digest  TEXT NOT NULL,
    file_count      INTEGER NOT NULL,
    total_bytes     INTEGER NOT NULL,
    manifest_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (batch_uid, attempt, stage)
);

CREATE TABLE IF NOT EXISTS batch_diagnostics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uid   TEXT NOT NULL REFERENCES batches(uid) ON DELETE CASCADE,
    attempt     INTEGER NOT NULL DEFAULT 0,
    code        TEXT NOT NULL,
    rel_path    TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_uid, attempt, code, rel_path, detail)
);

-- First bytes ever uploaded for a (batch, path).  It survives retries so a
-- batch id can never be re-used to publish different content, even when the
-- first attempt failed before a seal ever recorded a content digest.
CREATE TABLE IF NOT EXISTS batch_content_witness (
    batch_uid   TEXT NOT NULL REFERENCES batches(uid) ON DELETE CASCADE,
    rel_path    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_uid, rel_path)
);

CREATE TABLE IF NOT EXISTS recovery_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT NOT NULL,
    batches_checked   INTEGER NOT NULL,
    published_resumed INTEGER NOT NULL,
    failures_json     TEXT NOT NULL,
    summary_json      TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
