"""Crash + recovery tests: kill a real subprocess at each pause point and
verify that a fresh process reconstructs where it stopped and finishes (or
fails with a diagnosis) exactly once.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.crashpoints import CrashPoints
from app.db import Database
from app.errors import Conflict
from app.recovery import inspect_placement
from app.service import ReleaseDesk

PYTHON = sys.executable
DRIVER = Path(__file__).with_name("crash_driver.py")
BATCH_ID = "crash-batch"


def run_driver(data_dir: Path, mode: str, crash_point: str = "", batch_id: str = BATCH_ID):
    command = [PYTHON, "-I", str(DRIVER), str(data_dir), mode, crash_point, batch_id]
    return subprocess.run(
        command, capture_output=True, text=True, timeout=120, env=_clean_env()
    )


def _clean_env() -> dict:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("BATCH_DESK_")}
    return env


@pytest.fixture()
def crash_dir(tmp_path: Path) -> Path:
    root = tmp_path / "desk"
    root.mkdir()
    return root


def _open_desk(data_dir: Path) -> tuple[ReleaseDesk, Database]:
    settings = Settings(
        data_root=data_dir,
        staging_dir=data_dir / "staging",
        published_dir=data_dir / "published",
        db_path=data_dir / "desk.sqlite3",
        diagnostics_dir=data_dir / "diagnostics",
        tmp_dir=data_dir / ".tmp",
        lock_dir=data_dir / ".locks",
        frontend_dir=data_dir / "frontend-dist",
    )
    database = Database(settings.db_path)
    return ReleaseDesk(settings, database, CrashPoints([])), database


def _uid_for(data_dir: Path) -> str:
    connection = sqlite3.connect(data_dir / "desk.sqlite3")
    try:
        return connection.execute(
            "SELECT uid FROM batches WHERE batch_id=?", (BATCH_ID,)
        ).fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "crash_point,expected_move_done",
    [
        ("after_files_written", False),
        ("after_seal", False),
        ("after_move", True),
    ],
)
def test_crash_before_final_commit_resumes_to_published(
    crash_dir, crash_point, expected_move_done
):
    prepared = run_driver(crash_dir, "prepare", crash_point)
    assert prepared.returncode == 75, prepared.stderr
    uid = _uid_for(crash_dir)

    desk, database = _open_desk(crash_dir)
    try:
        before = desk.get_batch(uid)
        assert before["phase"] != "published"
        placement = inspect_placement(desk, before)
        # Evidence reflects what actually happened on disk at the kill point.
        if expected_move_done:
            assert placement["in_published"] == 2
            assert placement["in_staging"] == 0
        else:
            assert placement["in_staging"] >= 1

        recovery = desk.recover()
        assert recovery["summary"]["published"] == 1

        after = desk.get_batch(uid)
        assert after["phase"] == "published"
        assert after["downloadable"] is True
        assert {f["state"] for f in after["files"]} == {"moved"}
        root = desk.published_batch_dir(uid)
        assert (root / "alpha.txt").read_bytes() == b"alpha-payload"
        assert (root / "nested/beta.bin").read_bytes() == bytes(range(16))

        # Snapshot rows are unique per stage: no duplicate registration.
        stages = [s["stage"] for s in after["snapshots"]]
        assert len(stages) == len(set(stages))
        assert set(stages) == {"staged", "file_ready", "moved", "published"}

        # A second recovery must be a no-op.
        second = desk.recover()
        assert second["summary"]["published"] == 0
        assert len(desk.get_batch(uid)["snapshots"]) == 4
    finally:
        database.close()


def test_crash_at_commit_boundaries(crash_dir):
    # 'before_commit': all files are already in the published tree.
    prepared = run_driver(crash_dir, "prepare", "before_commit")
    assert prepared.returncode == 75
    uid = _uid_for(crash_dir)
    desk, database = _open_desk(crash_dir)
    try:
        before = desk.get_batch(uid)
        assert before["phase"] == "file_ready"
        assert before["publish_state"] == "moving"
        assert inspect_placement(desk, before)["in_published"] == 2
        desk.recover()
        after = desk.get_batch(uid)
        assert after["phase"] == "published"
        assert after["publish_state"] == "committed"
    finally:
        database.close()


def test_crash_after_commit_needs_no_resume(crash_dir):
    prepared = run_driver(crash_dir, "prepare", "after_commit")
    assert prepared.returncode == 75
    uid = _uid_for(crash_dir)
    desk, database = _open_desk(crash_dir)
    try:
        assert desk.get_batch(uid)["phase"] == "published"
        recovery = desk.recover()
        assert recovery["summary"]["checked"] == 0
        checked = desk.resolve_download(BATCH_ID)
        assert len(checked[1]) == 2
    finally:
        database.close()


def test_crash_between_moves_leaves_mixed_evidence(crash_dir):
    """Simulate a kill in the middle of the rename loop.

    The public crash points bracket the whole move; to test the interior we
    perform the move manually for the first file and kill, mimicking exactly
    what the loop would have committed.
    """
    desk, database = _open_desk(crash_dir)
    try:
        info, _ = desk.create_batch(BATCH_ID, [("alpha.txt", 13), ("nested/beta.bin", 16)])
        uid = info["uid"]
        import io

        desk.write_upload(uid, "alpha.txt", io.BytesIO(b"alpha-payload"))
        desk.write_upload(uid, "nested/beta.bin", io.BytesIO(bytes(range(16))))
        desk.seal(uid)
        # Move just the first file, exactly as _move_files would.
        row = database.query_one(
            "SELECT id, rel_path, sha256 FROM batch_files WHERE batch_uid=? AND rel_path=?",
            (uid, "alpha.txt"),
        )
        source = desk.staging_batch_dir(uid) / "alpha.txt"
        dest = desk.published_batch_dir(uid) / "alpha.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        database.execute(
            "UPDATE batch_files SET state='moved' WHERE id=?", (row["id"],)
        )
        database.execute(
            "UPDATE batches SET publish_state='moving' WHERE uid=?", (uid,)
        )
    finally:
        database.close()

    # New process: must see one file on each side and finish the job once.
    result = run_driver(crash_dir, "recover")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["recovery"]["summary"]["published"] == 1

    desk, database = _open_desk(crash_dir)
    try:
        batch = desk.get_batch(_uid_for(crash_dir))
        assert batch["phase"] == "published"
        checked = desk.resolve_download(BATCH_ID)
        paths = [f["rel_path"] for f in checked[1]]
        assert sorted(paths) == ["alpha.txt", "nested/beta.bin"]
    finally:
        database.close()


def test_repeated_recovery_does_not_duplicate_registration(crash_dir):
    prepared = run_driver(crash_dir, "prepare", "after_seal")
    assert prepared.returncode == 75
    for _ in range(3):
        result = run_driver(crash_dir, "recover")
        assert result.returncode == 0, result.stderr
    desk, database = _open_desk(crash_dir)
    try:
        uid = _uid_for(crash_dir)
        detail = desk.get_batch(uid)
        assert detail["phase"] == "published"
        assert [s["stage"] for s in detail["snapshots"]] == [
            "staged", "file_ready", "moved", "published"
        ]
        runs = desk.db.query("SELECT COUNT(*) AS n FROM recovery_runs")[0]["n"]
        assert runs == 3
    finally:
        database.close()


def test_corrupt_staged_file_fails_with_diagnosis_and_blocks_download(crash_dir):
    prepared = run_driver(crash_dir, "prepare", "after_seal")
    assert prepared.returncode == 75
    uid = _uid_for(crash_dir)
    staged = crash_dir / "staging" / uid / "alpha.txt"
    staged.write_bytes(b"corrupted!!!")

    desk, database = _open_desk(crash_dir)
    try:
        recovery = desk.recover()
        assert recovery["summary"]["failed"] == 1
        batch = desk.get_batch(uid)
        assert batch["phase"] != "published"
        assert "verification" in (batch["failure_reason"] or "").lower()
        codes = {d["code"] for d in batch["diagnostics"]}
        assert any("hash_mismatch" in c or "size_mismatch" in c for c in codes)
        # Diagnostics persisted on disk as well as in SQLite.
        diag_files = list((crash_dir / "diagnostics").rglob("*.json"))
        assert diag_files
        with pytest.raises(Exception):
            desk.resolve_download(BATCH_ID)
    finally:
        database.close()


def test_retry_after_damage_starts_fresh_attempt(crash_dir):
    prepared = run_driver(crash_dir, "prepare", "after_seal")
    assert prepared.returncode == 75
    uid = _uid_for(crash_dir)
    (crash_dir / "staging" / uid / "alpha.txt").write_bytes(b"damaged")

    desk, database = _open_desk(crash_dir)
    try:
        desk.recover()
        failed = desk.get_batch(uid)
        assert failed["failure_code"]
        retried = desk.start_new_attempt(uid)
        assert retried["attempt"] == 1
        assert retried["phase"] == "preparing"
        assert retried["failure_code"] is None
        assert retried["declarations"]
        # Fresh uploads under the new attempt can complete the release.
        import io

        for name, data in {"alpha.txt": b"alpha-payload", "nested/beta.bin": bytes(range(16))}.items():
            desk.write_upload(uid, name, io.BytesIO(data))
        desk.seal(uid)
        desk.publish(uid)
        assert desk.get_batch(uid)["phase"] == "published"
        assert desk.resolve_download(BATCH_ID)[1]
    finally:
        database.close()


def test_retry_with_different_content_conflicts(crash_dir):
    """A new attempt must not smuggle different bytes under the same id."""
    desk, database = _open_desk(crash_dir)
    try:
        info, _ = desk.create_batch(BATCH_ID, [("alpha.txt", 13)])
        uid = info["uid"]
        import io

        desk.write_upload(uid, "alpha.txt", io.BytesIO(b"alpha-payload"))
        desk.seal(uid)
    finally:
        database.close()

    # Damage it, recover to a failure, retry with different bytes.
    (crash_dir / "staging" / uid / "alpha.txt").unlink()
    desk, database = _open_desk(crash_dir)
    try:
        desk.recover()
        desk.start_new_attempt(uid)
        import io

        # The witness table remembers the first bytes this id ever accepted,
        # so different content is refused as early as the next upload.
        with pytest.raises(Conflict, match="different content"):
            desk.write_upload(uid, "alpha.txt", io.BytesIO(b"different!!!!"))
        # Re-uploading the original bytes still works.
        desk.write_upload(uid, "alpha.txt", io.BytesIO(b"alpha-payload"))
        desk.seal(uid)
        desk.publish(uid)
        assert desk.get_batch(uid)["phase"] == "published"
    finally:
        database.close()


def test_missing_published_file_blocks_download(crash_dir):
    prepared = run_driver(crash_dir, "prepare", "")
    assert prepared.returncode == 0, prepared.stderr
    uid = _uid_for(crash_dir)
    (crash_dir / "published" / uid / "alpha.txt").unlink()
    desk, database = _open_desk(crash_dir)
    try:
        with pytest.raises(Exception, match="verification"):
            desk.resolve_download(BATCH_ID)
    finally:
        database.close()


def test_dispatcher_recovery_reaches_consistent_state(crash_dir):
    """Kill the app at each phase via the driver, then start a fresh desk
    whose startup recovery runs, mirroring what FastAPI's lifespan does."""
    for point in ("after_files_written", "after_seal", "after_move", "before_commit"):
        shutil.rmtree(crash_dir)
        crash_dir.mkdir()
        run_driver(crash_dir, "prepare", point)
        desk, database = _open_desk(crash_dir)
        try:
            desk.recover()
            uid = _uid_for(crash_dir)
            detail = desk.get_batch(uid)
            assert detail["phase"] == "published", point
            checked = desk.resolve_download(BATCH_ID)
            assert sorted(f["rel_path"] for f in checked[1]) == [
                "alpha.txt", "nested/beta.bin",
            ]
        finally:
            database.close()


def test_crash_between_rename_and_registration_is_adopted(crash_dir):
    """A kill after the atomic rename but before the DB insert must not fail
    the batch.  Recovery adopts only declared paths whose bytes match the
    declaration and the id's content witness."""
    desk, database = _open_desk(crash_dir)
    try:
        info, _ = desk.create_batch(BATCH_ID, [("alpha.txt", 13)])
        uid = info["uid"]
        import io

        desk.write_upload(uid, "alpha.txt", io.BytesIO(b"alpha-payload"))
    finally:
        database.close()

    # Simulate the crash window: the final file exists, its row does not.
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(crash_dir / "desk.sqlite3")
    try:
        connection.execute("DELETE FROM batch_files WHERE batch_uid=?", (uid,))
        connection.commit()
    finally:
        connection.close()

    desk, database = _open_desk(crash_dir)
    try:
        result = desk.recover()
        adopted = [
            b for b in result["batches"]
            if any(d["code"] == "adopted_unrecorded_upload" for d in b["diagnostics"])
        ]
        assert adopted, result
        detail = desk.get_batch(uid)
        assert detail["failure_code"] is None
        assert [f["rel_path"] for f in detail["files"]] == ["alpha.txt"]
        # It is usable: the operator can seal or publish normally.
        from app.errors import NotReadyForPublish

        with pytest.raises(NotReadyForPublish):
            desk.publish(uid)  # records intent; not sealed yet
        sealed = desk.seal(uid)
        assert sealed["phase"] == "published"  # seal honours the recorded intent
        assert desk.get_batch(uid)["phase"] == "published"
        assert desk.resolve_download(BATCH_ID)[1]
    finally:
        database.close()


def test_unrecorded_file_with_wrong_size_is_not_adopted(crash_dir):
    """Adoption must stay narrow: unknown bytes become diagnostics, not rows."""
    desk, database = _open_desk(crash_dir)
    try:
        info, _ = desk.create_batch(BATCH_ID, [("alpha.txt", 13)])
        uid = info["uid"]
    finally:
        database.close()
    (crash_dir / "staging" / uid / "alpha.txt").write_bytes(b"wrong-size-bytes")
    desk, database = _open_desk(crash_dir)
    try:
        desk.recover()
        detail = desk.get_batch(uid)
        assert detail["files"] == []
        assert any(
            d["code"] == "staged_unrecorded_size_mismatch"
            for d in detail["diagnostics"]
        )
        assert detail["failure_code"]
    finally:
        database.close()
