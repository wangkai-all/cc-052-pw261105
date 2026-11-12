"""Shared fixtures for the release desk test suite."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.crashpoints import CrashPoints  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ReleaseDesk  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_root=tmp_path,
        staging_dir=tmp_path / "staging",
        published_dir=tmp_path / "published",
        db_path=tmp_path / "desk.sqlite3",
        diagnostics_dir=tmp_path / "diagnostics",
        tmp_dir=tmp_path / ".tmp",
        lock_dir=tmp_path / ".locks",
        frontend_dir=tmp_path / "frontend-dist",
    )
    settings.ensure_layout()
    return settings


@pytest.fixture()
def database(settings: Settings) -> Database:
    db = Database(settings.db_path)
    yield db
    db.close()


@pytest.fixture()
def desk(settings: Settings, database: Database) -> ReleaseDesk:
    return ReleaseDesk(settings, database, CrashPoints([]))


def upload(desk: ReleaseDesk, uid: str, rel_path: str, data: bytes) -> dict:
    return desk.write_upload(uid, rel_path, io.BytesIO(data))


def make_published(desk: ReleaseDesk, batch_id: str = "batch-a") -> dict:
    declared = [("a.txt", 5), ("dir/b.bin", 4)]
    info, _ = desk.create_batch(batch_id, declared)
    uid = info["uid"]
    upload(desk, uid, "a.txt", b"hello")
    upload(desk, uid, "dir/b.bin", b"data")
    desk.seal(uid)
    desk.publish(uid)
    return desk.get_batch(uid)
