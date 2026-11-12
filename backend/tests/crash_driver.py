"""Subprocess driver used by crash tests.

Usage::

    python crash_driver.py <data-dir> <mode> [crash-point] [batch-id]

``mode`` is one of:

``prepare``   create the batch, upload all files, seal, then publish
``upload``    create the batch, upload all files (no seal)
``publish``   assume the batch is already sealed and publish it
``recover``   construct the desk and run recovery, then dump state as JSON

The crashed process is killed via ``os._exit`` at the configured point; the
wrapper ``run`` command returns process output plus a status code the caller
inspects.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.crashpoints import CrashPoints
from app.db import Database
from app.errors import BatchFinished
from app.service import ReleaseDesk

BATCH_ID = "crash-batch"
FILES = {"alpha.txt": b"alpha-payload", "nested/beta.bin": bytes(range(16))}


def build(data_dir: Path, crash_points: set[str]) -> ReleaseDesk:
    root = Path(data_dir)
    settings = Settings(
        data_root=root,
        staging_dir=root / "staging",
        published_dir=root / "published",
        db_path=root / "desk.sqlite3",
        diagnostics_dir=root / "diagnostics",
        tmp_dir=root / ".tmp",
        lock_dir=root / ".locks",
        frontend_dir=root / "frontend-dist",
        crash_points=frozenset(crash_points),
    )
    settings.ensure_layout()
    database = Database(settings.db_path)
    return ReleaseDesk(settings, database, CrashPoints(crash_points))


def dump(desk: ReleaseDesk, batch_id: str) -> dict:
    row = desk.db.query_one("SELECT uid FROM batches WHERE batch_id=?", (batch_id,))
    if not row:
        return {"batch": None}
    detail = desk.get_batch(row["uid"])
    return {"batch": detail}


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1])
    mode = argv[2]
    crash_points = set((argv[3] if len(argv) > 3 else "").split(",")) - {""}
    batch_id = argv[4] if len(argv) > 4 else BATCH_ID

    if mode == "recover":
        desk = build(data_dir, set())
        result = desk.recover()
        print(json.dumps({"recovery": result}, default=str))
        return 0

    desk = build(data_dir, crash_points)
    declared = [(name, len(data)) for name, data in FILES.items()]
    info, _ = desk.create_batch(batch_id, declared)
    uid = info["uid"]

    if mode == "publish":
        desk.publish(uid)
        print(json.dumps({"published": True}))
        return 0

    if mode == "prepare":
        # Record publication intent before uploads finish so recovery knows the
        # operator wanted this released even if the kill lands mid-upload.
        try:
            desk.publish(uid)
        except Exception:
            pass
        for name, data in FILES.items():
            desk.write_upload(uid, name, io.BytesIO(data))
        desk.seal(uid)
        try:
            desk.publish(uid)
        except BatchFinished:
            pass  # the seal already chained through the recorded request
        print(json.dumps({"published": True}))
        return 0

    if mode == "upload":
        for name, data in FILES.items():
            desk.write_upload(uid, name, io.BytesIO(data))
        print(json.dumps(dump(desk, batch_id), default=str))
        return 0

    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
