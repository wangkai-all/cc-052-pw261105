"""The release desk state machine.

Phases
------
``preparing``  uploads are written into ``staging/<uid>/`` and recorded row by
              row in ``batch_files``.  File bytes become visible to the desk
              only after an atomic rename from ``.part`` to the final name.
``file_ready`` all declared files exist, hash to their recorded digests, and a
              snapshot is committed.  This is the pause point between "files
              written" and "files moved".
``published``  all files were renamed into ``published/<uid>/`` and verified
              there, and the final transaction committed.  Only this phase is
              downloadable.

``batches.publish_state`` refines the published phase across a crash:
``collecting`` -> ``ready`` -> ``moving`` -> ``committed``.

Crash safety
------------
The data move is a sequence of atomic renames within one filesystem.  After
each successful rename its ``batch_files`` row flips to ``moved``.  Recovery
therefore has two independent sources of truth — SQLite rows and the actual
contents of the staging/published trees — and can always tell where it stopped.
Recovery never re-registers work: snapshot writes and diagnostics are unique
per (batch, attempt, stage) / (batch, attempt, code, path, detail).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .crashpoints import CrashPoints
from .db import Database, utcnow
from .errors import (
    BatchFailed,
    BatchFinished,
    BatchNotFound,
    Conflict,
    DeskError,
    InvalidBatchId,
    LockBusy,
    NotReadyForPublish,
    UploadRejected,
)
from .locks import BatchLock
from .manifests import build_manifest, manifest_statistics, parse_manifest
from .storage import (
    atomic_write_bytes,
    fsync_dir,
    is_within,
    normalize_rel_path,
    remove_owned_path,
    remove_tree,
    sha256_bytes,
    sha256_file,
)


def _parse_uid(uid: str) -> str:
    if not isinstance(uid, str) or not (16 <= len(uid) <= 48):
        raise InvalidBatchId("batch uid is malformed")
    if not all(c.isalnum() or c in "-_" for c in uid):
        raise InvalidBatchId("batch uid is malformed")
    return uid


class ReleaseDesk:
    def __init__(self, settings: Settings, database: Database, crash: CrashPoints):
        self.settings = settings
        self.db = database
        self.crash = crash
        self.locks = BatchLock(settings.lock_dir)
        for directory in (
            self.settings.staging_dir,
            self.settings.published_dir,
            self.settings.diagnostics_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def staging_batch_dir(self, uid: str) -> Path:
        return self.settings.staging_dir / _parse_uid(uid)

    def _upload_tmp_dir(self, uid: str) -> Path:
        return self.settings.tmp_dir / "uploads" / _parse_uid(uid)

    def published_batch_dir(self, uid: str) -> Path:
        return self.settings.published_dir / _parse_uid(uid)

    # --------------------------------------------------------------- sessions
    def create_batch(
        self, batch_id: str, declared: list[tuple[str, int]]
    ) -> tuple[dict[str, Any], bool]:
        """Register a batch attempt.  Returns ``(info, created_now)``.

        Re-posting the same batch id with the same file names and sizes is
        idempotent and returns the existing attempt, which is what lets a
        caller retry a create request after a network hiccup.  Re-posting the
        same id with a different file set (or different declared sizes) is a
        conflict: an id names one immutable release.
        """
        batch_id = (batch_id or "").strip()
        if not batch_id or len(batch_id) > 120:
            raise DeskError(
                "batch_id must be 1..120 characters",
                code="invalid_batch_id",
                status=422,
            )
        normalized: list[tuple[str, int]] = []
        seen: set[str] = set()
        for rel_path_raw, size in declared:
            try:
                rel_path = normalize_rel_path(rel_path_raw)
            except ValueError as exc:
                raise UploadRejected(str(exc)) from exc
            if rel_path in seen:
                raise UploadRejected(f"duplicate file name in request: {rel_path}")
            seen.add(rel_path)
            if size < 0:
                raise UploadRejected(f"negative size for {rel_path}")
            normalized.append((rel_path, int(size)))
        if not normalized:
            raise UploadRejected("a batch needs at least one data file")
        if len(normalized) > self.settings.max_files:
            raise UploadRejected(
                f"at most {self.settings.max_files} files per batch"
            )

        with self.db.transaction():
            existing = self.db.query_one(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
            )
            if existing:
                previously = {
                    (r["rel_path"], r["declared_size"])
                    for r in self.db.query(
                        "SELECT rel_path, declared_size FROM batch_declarations "
                        "WHERE batch_uid=? AND attempt=?",
                        (existing["uid"], existing["attempt"]),
                    )
                }
                if previously != set(normalized):
                    raise Conflict(
                        f"batch id {batch_id!r} already names a different file "
                        "set; an existing batch id cannot change content"
                    )
                return self.get_batch(existing["uid"]), False

            uid = uuid.uuid4().hex
            now = utcnow()
            self.db.execute(
                """
                INSERT INTO batches
                    (uid, batch_id, attempt, phase, publish_state,
                     created_at, updated_at)
                VALUES (?, ?, 0, 'preparing', 'collecting', ?, ?)
                """,
                (uid, batch_id, now, now),
            )
            for rel_path, size in normalized:
                self.db.execute(
                    "INSERT INTO batch_declarations "
                    "(batch_uid, attempt, rel_path, declared_size) VALUES (?, 0, ?, ?)",
                    (uid, rel_path, size),
                )
        self.staging_batch_dir(uid).mkdir(parents=True, exist_ok=True)
        return self.get_batch(uid), True

    # --------------------------------------------------------------- uploads
    def write_upload(self, uid: str, rel_path_raw: str, stream) -> dict[str, Any]:
        uid = _parse_uid(uid)
        with self.locks.acquire(uid) as acquired:
            if not acquired:  # pragma: no cover - blocking acquisition
                raise LockBusy("could not acquire batch lock")
            return self._write_upload_locked(uid, rel_path_raw, stream)

    def _write_upload_locked(self, uid: str, rel_path_raw: str, stream) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        if batch["phase"] != "preparing":
            raise DeskError(
                f"uploads are closed: batch is already {batch['phase']}",
                code="upload_closed",
                status=409,
            )
        if batch["cancelled_at"]:
            raise DeskError(
                "batch was cancelled; uploads are closed",
                code="batch_cancelled",
                status=409,
            )
        try:
            rel_path = normalize_rel_path(rel_path_raw)
        except ValueError as exc:
            raise UploadRejected(str(exc)) from exc
        declared = {(r["rel_path"], r["declared_size"]) for r in self._declarations(uid, batch["attempt"])}
        declared_paths = {p for p, _ in declared}
        if rel_path not in declared_paths:
            raise UploadRejected(
                f"{rel_path!r} was not declared when the batch was created"
            )
        existing = self.db.query_one(
            "SELECT * FROM batch_files WHERE batch_uid=? AND attempt=? AND rel_path=?",
            (uid, batch["attempt"], rel_path),
        )
        if existing and existing["state"] == "staged":
            raise UploadRejected(
                f"{rel_path!r} was already uploaded; overwriting a visible "
                "staged file is not allowed"
            )

        final_path = self.staging_batch_dir(uid) / rel_path
        # In-flight bytes are staged outside the batch tree entirely.  A crash
        # can leave a .part in tmp/, but never an unrecorded file in staging/
        # that a verifier would trip over.  It also means a declared file may
        # legitimately be named "*.part" without colliding with our temp file.
        part_path = self._upload_tmp_dir(uid) / (sha256_bytes(rel_path.encode()) + ".part")
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.unlink(missing_ok=True)

        total = 0
        digest = ""
        try:
            with part_path.open("wb") as handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if isinstance(chunk, str):  # pragma: no cover - defensive
                        chunk = chunk.encode()
                    total += len(chunk)
                    if total > self.settings.max_file_bytes:
                        raise UploadRejected(
                            f"{rel_path!r} exceeds the per-file limit of "
                            f"{self.settings.max_file_bytes} bytes"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            digest = sha256_file(part_path)
            witness = self.db.query_one(
                "SELECT sha256 FROM batch_content_witness "
                "WHERE batch_uid=? AND rel_path=?",
                (uid, rel_path),
            )
            if witness and witness["sha256"] != digest:
                raise Conflict(
                    f"batch id already holds different bytes for {rel_path!r}; "
                    "an id cannot be re-used for different content"
                )
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, final_path)
            fsync_dir(final_path.parent)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise

        with self.db.transaction():
            batch = self._batch_by_uid(uid)
            if batch["phase"] != "preparing":
                raise DeskError(
                    "the batch closed while this upload was in flight",
                    code="upload_closed",
                    status=409,
                )
            row = self.db.query_one(
                "SELECT * FROM batch_files WHERE batch_uid=? AND attempt=? AND rel_path=?",
                (uid, batch["attempt"], rel_path),
            )
            if row and row["state"] == "staged":
                raise UploadRejected(f"{rel_path!r} was already uploaded")
            now = utcnow()
            if row:
                self.db.execute("DELETE FROM batch_files WHERE id=?", (row["id"],))
            self.db.execute(
                """
                INSERT INTO batch_content_witness
                    (batch_uid, rel_path, sha256, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(batch_uid, rel_path) DO NOTHING
                """,
                (uid, rel_path, digest, now),
            )
            self.db.execute(
                """
                INSERT INTO batch_files
                    (batch_uid, attempt, rel_path, size, sha256, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'staged', ?, ?)
                """,
                (uid, batch["attempt"], rel_path, total, digest, now, now),
            )
            totals = self.db.query_one(
                "SELECT COALESCE(SUM(size),0) AS bytes FROM batch_files "
                "WHERE batch_uid=? AND attempt=?",
                (uid, batch["attempt"]),
            )
            if totals["bytes"] > self.settings.max_batch_bytes:
                self.db.execute(
                    "DELETE FROM batch_files WHERE batch_uid=? AND attempt=? "
                    "AND rel_path=?",
                    (uid, batch["attempt"], rel_path),
                )
                final_path.unlink(missing_ok=True)
                raise UploadRejected(
                    "batch exceeds the total limit of "
                    f"{self.settings.max_batch_bytes} bytes"
                )
            self.db.execute(
                "UPDATE batches SET updated_at=? WHERE uid=?", (now, uid)
            )
            uploaded_paths = {
                r["rel_path"]
                for r in self.db.query(
                    "SELECT rel_path FROM batch_files WHERE batch_uid=? AND attempt=?",
                    (uid, batch["attempt"]),
                )
            }
            declared_paths = {d["rel_path"] for d in self._declarations(uid, batch["attempt"])}
        if uploaded_paths == declared_paths:
            # Every declared file now exists on disk and in SQLite.
            self.crash.hit("after_files_written")
        return {
            "rel_path": rel_path,
            "size": total,
            "sha256": digest,
            "state": "staged",
        }

    # -------------------------------------------------------------- sealing
    def seal(self, uid: str, expected_digest: str | None = None) -> dict[str, Any]:
        uid = _parse_uid(uid)
        with self.locks.acquire(uid) as acquired:
            if not acquired:  # pragma: no cover
                raise LockBusy("could not acquire batch lock")
            return self._seal_locked(uid, expected_digest)

    def _seal_locked(self, uid: str, expected_digest: str | None) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        if batch["phase"] == "published":
            raise BatchFinished("batch is already published")
        if batch["cancelled_at"]:
            raise DeskError(
                "batch was cancelled; it cannot be sealed or published",
                code="batch_cancelled",
                status=409,
            )
        self._reject_if_failed(batch)
        if batch["phase"] == "file_ready":
            # Idempotent re-seal: verify current placement and return the
            # existing snapshot.  No new snapshot row is written.
            if expected_digest and expected_digest != batch["content_digest"]:
                raise Conflict(
                    "announced digest does not match the sealed digest of this batch"
                )
            problems = self._diagnose_placement(batch)
            for problem in problems:
                self._record_diagnostic(uid, batch["attempt"], **problem)
            if problems:
                reason = "sealed batch no longer verifies cleanly: " + _summarize(problems)
                self._mark_failed_at(uid, reason, "staged_verification_failed")
                raise BatchFailed(reason, code="staged_verification_failed")
            return self._snapshot(batch, "file_ready", require=True)
        if batch["phase"] != "preparing":
            raise DeskError(
                f"cannot seal a batch in phase {batch['phase']!r}",
                code="invalid_phase",
                status=409,
            )

        files = self._files_for(batch)
        if not files:
            raise UploadRejected("cannot seal a batch with no uploaded files")
        declared = {(r["rel_path"], r["declared_size"]) for r in self._declarations(uid, batch["attempt"])}
        uploaded_by_path = {f["rel_path"]: f["size"] for f in files}
        declared_by_path = dict(declared)
        missing = sorted(set(declared_by_path) - set(uploaded_by_path))
        extra = sorted(set(uploaded_by_path) - set(declared_by_path))
        wrong_size = sorted(
            p for p in set(declared_by_path) & set(uploaded_by_path)
            if declared_by_path[p] != uploaded_by_path[p]
        )
        if missing:
            raise UploadRejected(
                "declared files have not been uploaded yet: " + ", ".join(missing),
                code="missing_uploads",
            )
        if extra:
            raise UploadRejected(
                "uploaded files were not declared: " + ", ".join(extra),
                code="undeclared_uploads",
            )
        if wrong_size:
            raise UploadRejected(
                "uploaded size differs from the declared size for: "
                + ", ".join(wrong_size),
                code="declared_size_mismatch",
            )

        problems = self._diagnose_placement(batch)
        for problem in problems:
            self._record_diagnostic(uid, batch["attempt"], **problem)
        if problems:
            reason = (
                "files failed verification; diagnostics were recorded. "
                "Re-upload the affected files or start a new attempt."
            )
            self._mark_failed_at(uid, reason, "staged_verification_failed")
            raise BatchFailed(reason, code="staged_verification_failed")

        manifest_json, digest = build_manifest(files)
        if expected_digest and digest != expected_digest:
            raise Conflict(
                "uploaded bytes do not match the content digest announced for "
                "this batch id; a batch id cannot change content"
            )
        if batch["content_digest"] and batch["content_digest"] != digest:
            # A previously sealed/published attempt for this batch id had
            # different bytes.  Same id, different content: reject.
            raise Conflict(
                f"batch id {batch['batch_id']!r} already has content digest "
                f"{batch['content_digest']}; uploaded bytes hash to {digest}"
            )

        now = utcnow()
        with self.db.transaction():
            batch = self._batch_by_uid(uid)
            if batch["phase"] == "published":
                raise BatchFinished("batch is already published")
            if batch["phase"] == "preparing":
                self.db.execute(
                    "UPDATE batches SET phase='file_ready', publish_state='ready', "
                    "content_digest=?, updated_at=? WHERE uid=?",
                    (digest, now, uid),
                )
                self._write_snapshot(uid, batch["attempt"], "staged", digest, manifest_json)
                self._write_snapshot(uid, batch["attempt"], "file_ready", digest, manifest_json)
        # Uploads are closed now; no in-flight temp file can ever be adopted.
        remove_tree(self._upload_tmp_dir(uid), "")
        self.crash.hit("after_seal")
        sealed = self._snapshot(self._batch_by_uid(uid), "file_ready", require=True)
        if self._batch_by_uid(uid)["publish_requested_at"]:
            # An early publish call recorded intent; honour it now that sealing
            # succeeded.  A crash between seal and this publish is handled by
            # recovery (file_ready + publish_requested_at).
            self._move_files(self._batch_by_uid(uid))
            self._finalize(self._batch_by_uid(uid))
            return self._snapshot(self._batch_by_uid(uid), "published", require=True)
        return sealed

    # ------------------------------------------------------------- publish
    def publish(self, uid: str, expected_digest: str | None = None) -> dict[str, Any]:
        uid = _parse_uid(uid)
        with self.locks.acquire(uid) as acquired:
            if not acquired:  # pragma: no cover
                raise LockBusy("could not acquire batch lock")
            return self._publish_locked(uid, expected_digest)

    def _publish_locked(self, uid: str, expected_digest: str | None) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        if batch["phase"] == "published":
            raise BatchFinished("batch is already published")
        if batch["cancelled_at"]:
            raise DeskError(
                "batch was cancelled and cannot be published",
                code="batch_cancelled",
                status=409,
            )
        self._reject_if_failed(batch)
        if batch["phase"] == "preparing":
            now = utcnow()
            self.db.execute(
                "UPDATE batches SET publish_requested_at=?, updated_at=? "
                "WHERE uid=? AND phase='preparing'",
                (now, now, uid),
            )
            raise NotReadyForPublish(
                "not all files are sealed yet; publish will continue after the "
                "file_ready phase is reached",
                code="publish_pending",
            )
        if batch["phase"] != "file_ready":
            raise DeskError(
                f"cannot publish from phase {batch['phase']!r}",
                code="invalid_phase",
                status=409,
            )
        if expected_digest and batch["content_digest"] != expected_digest:
            raise Conflict(
                "announced digest does not match the sealed contents of this batch"
            )
        self._move_files(batch)
        return self._finalize(batch)

    def _move_files(self, batch: Any) -> None:
        uid = batch["uid"]
        attempt = batch["attempt"]
        staged_root = self.staging_batch_dir(uid)
        published_root = self.published_batch_dir(uid)
        published_root.mkdir(parents=True, exist_ok=True)
        rows = self.db.query(
            "SELECT id, rel_path, size, sha256, state FROM batch_files "
            "WHERE batch_uid=? AND attempt=? ORDER BY rel_path",
            (uid, attempt),
        )
        if not rows:
            raise BatchFailed("batch has no recorded files", code="empty_manifest")

        with self.db.transaction():
            self.db.execute(
                "UPDATE batches SET publish_state='moving', updated_at=? "
                "WHERE uid=? AND phase='file_ready'",
                (utcnow(), uid),
            )

        for row in rows:
            if row["state"] == "moved":
                continue
            rel_path = row["rel_path"]
            source = staged_root / rel_path
            target = published_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # Evidence says this file already made it across in a prior,
                # crashed attempt.  Trust it only after hashing.
                if sha256_file(target) != row["sha256"]:
                    self._record_diagnostic(
                        uid, attempt, code="published_hash_mismatch",
                        rel_path=rel_path,
                        detail="a published file exists with the wrong digest",
                    )
                    raise BatchFailed(
                        f"{rel_path!r} already exists in the published tree "
                        "with different content",
                        code="published_verification_failed",
                    )
                source.unlink(missing_ok=True)
            else:
                if not source.is_file():
                    self._record_diagnostic(
                        uid, attempt, code="missing_before_move",
                        rel_path=rel_path,
                        detail="file is absent from staging when the move begins",
                    )
                    raise BatchFailed(
                        f"{rel_path!r} is missing from staging",
                        code="missing_before_move",
                    )
                if sha256_file(source) != row["sha256"]:
                    self._record_diagnostic(
                        uid, attempt, code="corrupt_before_move",
                        rel_path=rel_path,
                        detail="staged bytes no longer match the recorded digest",
                    )
                    raise BatchFailed(
                        f"{rel_path!r} is corrupt in staging; publish aborted",
                        code="corrupt_before_move",
                    )
                os.rename(source, target)
                fsync_dir(target.parent)
            with self.db.transaction():
                self.db.execute(
                    "UPDATE batch_files SET state='moved', updated_at=? WHERE id=?",
                    (utcnow(), row["id"]),
                )
        fsync_dir(published_root)
        self._remove_empty_staging(uid)
        self.crash.hit("after_move")

    def _finalize(self, batch: Any) -> dict[str, Any]:
        uid = batch["uid"]
        attempt = batch["attempt"]
        rows = self.db.query(
            "SELECT rel_path, size, sha256 FROM batch_files "
            "WHERE batch_uid=? AND attempt=? ORDER BY rel_path",
            (uid, attempt),
        )
        files = [
            {"rel_path": r["rel_path"], "size": r["size"], "sha256": r["sha256"]}
            for r in rows
        ]
        problems = self._diagnose_published(self.published_batch_dir(uid), files)
        for problem in problems:
            self._record_diagnostic(uid, attempt, **problem)
        if problems:
            reason = (
                "published files failed verification; the batch is not "
                "downloadable. Retry with fresh uploads after investigating."
            )
            self._mark_failed_at(uid, reason, "published_verification_failed")
            raise BatchFailed(reason, code="published_verification_failed")
        manifest_json, digest = build_manifest(files)
        self.crash.hit("before_commit")
        now = utcnow()
        with self.db.transaction():
            current = self._batch_by_uid(uid)
            if current["phase"] == "published":
                return self._snapshot(current, "published", require=True)
            if current["phase"] != "file_ready":
                raise DeskError(
                    f"cannot finalize from phase {current['phase']!r}",
                    code="invalid_phase",
                    status=409,
                )
            self.db.execute(
                "UPDATE batches SET phase='published', publish_state='committed', "
                "content_digest=?, updated_at=? WHERE uid=?",
                (digest, now, uid),
            )
            self._write_snapshot(uid, attempt, "moved", digest, manifest_json)
            self._write_snapshot(uid, attempt, "published", digest, manifest_json)
        self.crash.hit("after_commit")
        return self._snapshot(self._batch_by_uid(uid), "published", require=True)

    # -------------------------------------------------------------- cancel
    def cancel(self, uid: str) -> dict[str, Any]:
        uid = _parse_uid(uid)
        with self.locks.acquire(uid) as acquired:
            if not acquired:  # pragma: no cover
                raise LockBusy("could not acquire batch lock")
            return self._cancel_locked(uid)

    def _cancel_locked(self, uid: str) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        if batch["phase"] == "published":
            raise BatchFinished(
                "batch is already published and visible; it is not cancelled",
                code="already_published",
            )
        with self.db.transaction():
            batch = self._batch_by_uid(uid)
            if batch["phase"] == "published":
                # Another writer finished publishing while we waited for the
                # lock.  Published wins: cancel must not delete visible files.
                raise BatchFinished(
                    "batch became published while cancel waited; it stays visible",
                    code="already_published",
                )
            now = utcnow()
            self.db.execute(
                "UPDATE batches SET cancelled_at=?, failure_code='cancelled', "
                "failure_reason=?, failed_at=COALESCE(failed_at, ?), updated_at=? "
                "WHERE uid=? AND phase != 'published'",
                (now, "cancelled by operator before publication", now, now, uid),
            )
        # File rows are retained as an audit trail of what this batch owned;
        # cleanup below only touches those recorded paths.
        removed = self._cleanup_owned_files(uid)
        return {"uid": uid, "cancelled": True, "removed_files": removed}

    def _cleanup_owned_files(self, uid: str) -> int:
        """Delete only files this batch's manifest owns, in either tree."""
        owned = [
            r["rel_path"]
            for r in self.db.query(
                "SELECT rel_path FROM batch_files WHERE batch_uid=?", (uid,)
            )
        ]
        removed = 0
        for root in (self.staging_batch_dir(uid), self.published_batch_dir(uid)):
            if not root.exists():
                continue
            for rel in owned:
                if remove_owned_path(root, rel):
                    removed += 1
            _remove_empty_dirs(root)
        remove_tree(self._upload_tmp_dir(uid), "")
        return removed

    def _reject_if_failed(self, batch: Any) -> None:
        """A latched failure is terminal for this attempt.

        Without this, a batch that failed verification could still be sealed or
        published, producing a published batch that is not downloadable and
        cannot be retried.
        """
        if batch["failed_at"]:
            raise BatchFailed(
                batch["failure_reason"] or "batch is failed; start a new attempt",
                code=batch["failure_code"] or "batch_failed",
            )

    # ---------------------------------------------------------- verification
    def _diagnose_staged(self, root: Path, files: list[dict]) -> list[dict]:
        return self._diagnose_tree(root, files, "staged")

    def _diagnose_published(self, root: Path, files: list[dict]) -> list[dict]:
        return self._diagnose_tree(root, files, "published")

    @staticmethod
    def _diagnose_tree(root: Path, files: list[dict], prefix: str) -> list[dict]:
        problems: list[dict] = []
        if not root.exists():
            for entry in files:
                problems.append(
                    dict(
                        code=f"{prefix}_missing_file",
                        rel_path=entry["rel_path"],
                        detail=f"{prefix} directory is missing",
                    )
                )
            return problems
        expected = {entry["rel_path"] for entry in files}
        present = _present_files(root)
        for entry in files:
            rel = entry["rel_path"]
            path = root / rel
            if rel not in present or not path.is_file():
                problems.append(
                    dict(code=f"{prefix}_missing_file", rel_path=rel,
                         detail=f"{prefix} file is missing")
                )
                continue
            size = path.stat().st_size
            if size != entry["size"]:
                problems.append(
                    dict(code=f"{prefix}_size_mismatch", rel_path=rel,
                         detail=f"expected {entry['size']} bytes, found {size}")
                )
                continue
            digest = sha256_file(path)
            if digest != entry["sha256"]:
                problems.append(
                    dict(code=f"{prefix}_hash_mismatch", rel_path=rel,
                         detail=f"expected {entry['sha256']}, found {digest}")
                )
        for extra in sorted(present - expected):
            problems.append(
                dict(code=f"{prefix}_unexpected_file", rel_path=extra,
                     detail=f"{prefix} file has no manifest entry")
            )
        return problems

    def _diagnose_placement(self, batch: Any) -> list[dict]:
        """Verify a file_ready batch wherever its files currently are."""
        uid = batch["uid"]
        files = self._files_for(batch)
        published_root = self.published_batch_dir(uid)
        published_present = _present_files(published_root)
        problems: list[dict] = []
        staged_files: list[dict] = []
        for entry in files:
            rel = entry["rel_path"]
            if rel in published_present:
                target = published_root / rel
                if target.stat().st_size != entry["size"]:
                    problems.append(
                        dict(code="published_size_mismatch", rel_path=rel,
                             detail="published copy has the wrong size")
                    )
                elif sha256_file(target) != entry["sha256"]:
                    problems.append(
                        dict(code="published_hash_mismatch", rel_path=rel,
                             detail="published copy has the wrong digest")
                    )
            else:
                staged_files.append(entry)
        problems += self._diagnose_staged(self.staging_batch_dir(uid), staged_files)
        staged_expected = {f["rel_path"] for f in files}
        for extra in sorted(published_present - staged_expected):
            problems.append(
                dict(code="published_unexpected_file", rel_path=extra,
                     detail="published file has no manifest entry")
            )
        return problems

    # ------------------------------------------------------------ attempts
    def start_new_attempt(self, uid: str) -> dict[str, Any]:
        """Open a fresh upload attempt after a failed or damaged batch."""
        uid = _parse_uid(uid)
        with self.locks.acquire(uid) as acquired:
            if not acquired:  # pragma: no cover
                raise LockBusy("could not acquire batch lock")
            batch = self._batch_by_uid(uid)
            if batch["phase"] == "published":
                raise BatchFinished("published batches cannot be retried")
            if (
                batch["failure_code"] in {None, "cancelled"}
            ):
                if batch["failure_code"] == "cancelled":
                    raise DeskError(
                        "cancelled batches are terminal; create a new batch id",
                        code="batch_cancelled",
                        status=409,
                    )
                if batch["phase"] == "preparing":
                    raise NotReadyForPublish(
                        "batch is still collecting uploads; there is nothing to retry",
                        code="retry_not_available",
                    )
                raise NotReadyForPublish(
                    "batch is sealed and ready; publish it instead of retrying",
                    code="retry_not_available",
                )
            self._cleanup_owned_files(uid)
            with self.db.transaction():
                self.db.execute("DELETE FROM batch_files WHERE batch_uid=?", (uid,))
                now = utcnow()
                self.db.execute(
                    "UPDATE batches SET attempt=attempt+1, phase='preparing', "
                    "publish_state='collecting', publish_requested_at=NULL, "
                    "failure_code=NULL, failure_reason=NULL, failed_at=NULL, "
                    "updated_at=? WHERE uid=?",
                    (now, uid),
                )
                attempt = self._batch_by_uid(uid)["attempt"]
                self.db.execute(
                    "INSERT INTO batch_declarations "
                    "(batch_uid, attempt, rel_path, declared_size) "
                    "SELECT batch_uid, ?, rel_path, declared_size "
                    "FROM batch_declarations WHERE batch_uid=? AND attempt=? "
                    "ON CONFLICT(batch_uid, attempt, rel_path) DO NOTHING",
                    (attempt, uid, attempt - 1),
                )
            self.staging_batch_dir(uid).mkdir(parents=True, exist_ok=True)
            return self.get_batch(uid)

    # ------------------------------------------------------------- recovery
    def recover(self) -> dict[str, Any]:
        """Inspect every not-yet-published batch and resume from evidence."""
        started = utcnow()
        batches = self.db.query(
            "SELECT uid, batch_id, attempt, phase FROM batches "
            "WHERE phase != 'published' ORDER BY created_at"
        )
        results: list[dict[str, Any]] = []
        for row in batches:
            with self.locks.acquire(row["uid"]) as acquired:
                if not acquired:  # pragma: no cover
                    continue
                try:
                    result = self._recover_one(row["uid"])
                except BatchFailed as exc:
                    result = self._mark_failed_at(
                        row["uid"], str(exc), getattr(exc, "code", "batch_failed")
                    )
                except DeskError as exc:
                    result = {
                        "uid": row["uid"],
                        "batch_id": row["batch_id"],
                        "action": "failed",
                        "reason": str(exc),
                        "code": exc.code,
                        "diagnostics": self._diagnostics(row["uid"]),
                    }
                except Exception as exc:  # pragma: no cover - defensive
                    result = {
                        "uid": row["uid"],
                        "batch_id": row["batch_id"],
                        "action": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "diagnostics": self._diagnostics(row["uid"]),
                    }
                results.append(result)
        finished = utcnow()
        summary = {
            "checked": len(batches),
            "published": sum(1 for r in results if r.get("action") == "published"),
            "failed": sum(1 for r in results if r.get("action") in {"failed", "error"}),
            "left_alone": sum(
                1 for r in results if r.get("action") in {"waiting", "cancelled", "none"}
            ),
            "started_at": started,
            "finished_at": finished,
        }
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO recovery_runs
                    (started_at, finished_at, batches_checked,
                     published_resumed, failures_json, summary_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    started, finished, len(batches), summary["published"],
                    json.dumps([r for r in results if r.get("action") in {"failed", "error"}]),
                    json.dumps(summary),
                ),
            )
        return {"summary": summary, "batches": results}

    def _recover_one(self, uid: str) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        base = {"uid": uid, "batch_id": batch["batch_id"]}
        if batch["phase"] == "published":
            return {**base, "action": "none", "reason": "already published",
                    "diagnostics": []}
        if batch["failure_code"] == "cancelled":
            return {**base, "action": "cancelled",
                    "reason": "cancelled before publication", "diagnostics": []}
        if batch["failed_at"]:
            # A batch already recorded as failed stays failed.  Retrying
            # recovery must not silently resurrect it; the operator can
            # request a new attempt explicitly.
            return {**base, "action": "failed",
                    "reason": batch["failure_reason"] or "batch is failed",
                    "code": batch["failure_code"],
                    "diagnostics": self._diagnostics(uid)}
        if batch["phase"] == "preparing":
            return self._recover_preparing(batch, base)
        if batch["phase"] == "file_ready":
            return self._recover_after_seal(batch, base)
        return {**base, "action": "none",
                "reason": f"phase {batch['phase']!r} needs operator input",
                "diagnostics": []}

    def _recover_preparing(self, batch: Any, base: dict) -> dict[str, Any]:
        uid = batch["uid"]
        # A crash can land between the atomic rename of an upload into
        # staging/ and the transaction that records it.  The bytes were
        # fsynced before the rename, so recovery treats a declared-but-
        # unrecorded file as evidence of a completed upload rather than as
        # unexpected garbage; otherwise a normal crash window would destroy
        # the batch.
        # No upload can be in flight for this batch: the batch lock is held.
        # Any scratch file left in tmp/ is from an interrupted stream.
        remove_tree(self._upload_tmp_dir(uid), "")
        _, rejected = self._adopt_unrecorded_uploads(batch)
        files = self._files_for(batch)
        if rejected:
            return self._mark_failed(
                batch,
                "staging holds files this batch cannot account for: "
                + ", ".join(rejected),
                "staged_verification_failed",
                base=base,
            )
        if not files:
            if not batch["publish_requested_at"]:
                return {**base, "action": "waiting",
                        "reason": "no files uploaded yet; uploads remain open",
                        "diagnostics": self._diagnostics(uid)}
            return self._mark_failed(
                batch,
                "publish was requested but no files were ever recorded",
                "empty_batch",
                base=base,
            )
        # Files present in staging are trustworthy if their hashes match.
        problems = self._diagnose_staged(self.staging_batch_dir(uid), files)
        for problem in problems:
            self._record_diagnostic(uid, batch["attempt"], **problem)
        if problems:
            return self._mark_failed(
                batch,
                "staged files failed verification during recovery: "
                + _summarize(problems),
                "staged_verification_failed",
                base=base,
            )
        if not batch["publish_requested_at"]:
            # A crash during a normal upload leaves the batch open.  Do not
            # invent a seal the operator never asked for.
            return {**base, "action": "waiting",
                    "reason": "uploads remain open; no publish was requested",
                    "diagnostics": self._diagnostics(uid)}
        declared = {(d["rel_path"], d["declared_size"]) for d in self._declarations(uid, batch["attempt"])}
        uploaded = {(f["rel_path"], f["size"]) for f in files}
        not_yet_uploaded = sorted(p for p, _ in declared - uploaded)
        if not_yet_uploaded:
            return {**base, "action": "waiting",
                    "reason": "publish was requested but these declared files "
                              "have not been uploaded: " + ", ".join(not_yet_uploaded),
                    "diagnostics": self._diagnostics(uid)}
        try:
            self._seal_locked(uid, None)
        except DeskError as exc:
            return self._mark_failed(batch, str(exc), exc.code, base=base)
        refreshed = self._batch_by_uid(uid)
        if refreshed["phase"] == "published":
            # Seal honoured the recorded publish request and chained straight
            # through to publication.
            return self._published_result(uid, base)
        if refreshed["phase"] == "file_ready":
            return self._recover_after_seal(refreshed, base)
        return {**base, "action": "none", "reason": "seal did not advance the batch",
                "diagnostics": []}

    def _adopt_unrecorded_uploads(
        self, batch: Any
    ) -> tuple[list[str], list[str]]:
        """Register declared files that are on disk but missing a DB row.

        This is the post-rename/pre-insert crash window of an upload.  Adoption
        is narrow on purpose: only declared paths, only when the size matches
        the declaration, and only when the bytes agree with any witness
        recorded by an earlier upload for the same batch id.  A file that
        cannot be adopted is reported as rejected so recovery can fail the
        batch with a diagnosis instead of silently ignoring foreign bytes.
        """
        uid = batch["uid"]
        attempt = batch["attempt"]
        staged_root = self.staging_batch_dir(uid)
        if not staged_root.exists():
            return [], []
        declared = {
            d["rel_path"]: d["declared_size"]
            for d in self._declarations(uid, attempt)
        }
        recorded = {f["rel_path"] for f in self._files_for(batch)}
        adopted: list[str] = []
        rejected: list[str] = []
        for rel in sorted(_present_files(staged_root) - recorded):
            if rel not in declared:
                self._record_diagnostic(
                    uid, attempt, code="staged_unexpected_file", rel_path=rel,
                    detail="staging file was never declared and has no "
                           "recorded upload",
                )
                rejected.append(f"{rel} (undeclared)")
                continue
            path = staged_root / rel
            size = path.stat().st_size
            if size != declared[rel]:
                self._record_diagnostic(
                    uid, attempt, code="staged_unrecorded_size_mismatch",
                    rel_path=rel,
                    detail=f"unrecorded file has {size} bytes, declared "
                           f"{declared[rel]}",
                )
                rejected.append(f"{rel} (size {size} != declared {declared[rel]})")
                continue
            digest = sha256_file(path)
            witness = self.db.query_one(
                "SELECT sha256 FROM batch_content_witness "
                "WHERE batch_uid=? AND rel_path=?",
                (uid, rel),
            )
            if witness and witness["sha256"] != digest:
                self._record_diagnostic(
                    uid, attempt, code="staged_unrecorded_conflict",
                    rel_path=rel,
                    detail="unrecorded bytes differ from this batch id's "
                           "earlier content",
                )
                rejected.append(f"{rel} (content differs from this id's history)")
                continue
            now = utcnow()
            with self.db.transaction():
                self.db.execute(
                    """
                    INSERT INTO batch_files
                        (batch_uid, attempt, rel_path, size, sha256, state,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'staged', ?, ?)
                    ON CONFLICT(batch_uid, attempt, rel_path) DO NOTHING
                    """,
                    (uid, attempt, rel, size, digest, now, now),
                )
                self.db.execute(
                    """
                    INSERT INTO batch_content_witness
                        (batch_uid, rel_path, sha256, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(batch_uid, rel_path) DO NOTHING
                    """,
                    (uid, rel, digest, now),
                )
            self._record_diagnostic(
                uid, attempt, code="adopted_unrecorded_upload", rel_path=rel,
                detail="file was fully written before the process died; "
                       f"registered from disk evidence (size={size}, "
                       f"sha256={digest})",
            )
            adopted.append(rel)
        return adopted, rejected

    def _recover_after_seal(self, batch: Any, base: dict) -> dict[str, Any]:
        uid = batch["uid"]
        files = self._files_for(batch)
        published_root = self.published_batch_dir(uid)
        published_present = _present_files(published_root)
        expected = {f["rel_path"] for f in files}

        # Files are allowed to be in exactly one of the two trees.
        problems = self._diagnose_placement(batch)
        if problems:
            for problem in problems:
                self._record_diagnostic(uid, batch["attempt"], **problem)
            return self._mark_failed(
                batch,
                "files failed verification during recovery: " + _summarize(problems),
                "recovery_verification_failed",
                base=base,
            )

        if expected == published_present:
            # All renames happened; only the final DB commit is missing.
            try:
                self._finalize(batch)
            except DeskError as exc:
                return self._mark_failed(batch, str(exc), exc.code, base=base)
            return self._published_result(uid, base)

        # A sealed batch that nobody asked to publish, and whose move never
        # started, is a legitimate resting state (the operator paused after
        # sealing).  Recovery leaves it alone.  Resume only when the move had
        # begun (publish_state='moving') or a publish was requested.
        if (
            batch["phase"] == "file_ready"
            and batch["publish_state"] != "moving"
            and not batch["publish_requested_at"]
        ):
            return {
                **base,
                "action": "waiting",
                "reason": "sealed and awaiting an explicit publish request",
                "diagnostics": [],
            }
        try:
            self._move_files(batch)
            self._finalize(batch)
        except DeskError as exc:
            return self._mark_failed(batch, str(exc), exc.code, base=base)
        return self._published_result(uid, base)

    def _published_result(self, uid: str, base: dict) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        return {
            **base,
            "action": "published",
            "reason": "recovery completed the release",
            "diagnostics": self._diagnostics(uid),
            "phase": batch["phase"],
            "file_count": len(self._files_for(batch)),
        }

    def _mark_failed(
        self,
        batch: Any,
        reason: str,
        code: str,
        *,
        base: dict | None = None,
    ) -> dict[str, Any]:
        return self._mark_failed_at(batch["uid"], reason, code, base or {})

    def _mark_failed_at(
        self, uid: str, reason: str, code: str, base: dict | None = None
    ) -> dict[str, Any]:
        batch = self._batch_by_uid(uid)
        now = utcnow()
        with self.db.transaction():
            self.db.execute(
                "UPDATE batches SET failure_code=?, failure_reason=?, "
                "failed_at=?, updated_at=? WHERE uid=? AND phase != 'published' "
                "AND failure_code IS DISTINCT FROM 'cancelled' "
                "AND failed_at IS NULL",
                (code, reason, now, now, uid),
            )
        return {
            "uid": uid,
            "batch_id": batch["batch_id"],
            "action": "failed",
            "reason": reason,
            "code": code,
            "diagnostics": self._diagnostics(uid),
        }

    # ------------------------------------------------------------- detail
    def get_batch(self, uid: str) -> dict[str, Any]:
        uid = _parse_uid(uid)
        batch = self._batch_by_uid(uid)
        files = self._files_for(batch)
        declarations = [
            {"rel_path": r["rel_path"], "declared_size": r["declared_size"]}
            for r in self._declarations(uid, batch["attempt"])
        ]
        snapshots = [
            dict(r)
            for r in self.db.query(
                "SELECT stage, content_digest, file_count, total_bytes, created_at "
                "FROM batch_snapshots WHERE batch_uid=? AND attempt=? ORDER BY id",
                (uid, batch["attempt"]),
            )
        ]
        return {
            "uid": uid,
            "batch_id": batch["batch_id"],
            "attempt": batch["attempt"],
            "phase": batch["phase"],
            "publish_state": batch["publish_state"],
            "content_digest": batch["content_digest"],
            "publish_requested_at": batch["publish_requested_at"],
            "cancelled_at": batch["cancelled_at"],
            "failure_code": batch["failure_code"],
            "failure_reason": batch["failure_reason"],
            "failed_at": batch["failed_at"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "declarations": declarations,
            "files": files,
            "snapshots": snapshots,
            "diagnostics": self._diagnostics(uid, batch["attempt"]),
            "downloadable": batch["phase"] == "published"
            and not batch["failure_code"],
        }

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT b.uid, b.batch_id, b.attempt, b.phase, b.publish_state,
                   b.content_digest, b.failure_code, b.failure_reason,
                   b.created_at, b.updated_at,
                   COUNT(f.id) AS file_count,
                   COALESCE(SUM(f.size),0) AS total_bytes
            FROM batches b
            LEFT JOIN batch_files f
                ON f.batch_uid = b.uid AND f.attempt = b.attempt
            GROUP BY b.uid
            ORDER BY b.created_at DESC, b.batch_id
            """
        )
        return [
            {
                "uid": r["uid"],
                "batch_id": r["batch_id"],
                "attempt": r["attempt"],
                "phase": r["phase"],
                "publish_state": r["publish_state"],
                "content_digest": r["content_digest"],
                "failure_code": r["failure_code"],
                "failure_reason": r["failure_reason"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "file_count": r["file_count"],
                "total_bytes": r["total_bytes"],
                "downloadable": r["phase"] == "published"
                and not r["failure_code"],
            }
            for r in rows
        ]

    def list_recovery_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM recovery_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {
                "id": r["id"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "batches_checked": r["batches_checked"],
                "published_resumed": r["published_resumed"],
                "summary": json.loads(r["summary_json"]),
            }
            for r in rows
        ]

    # ----------------------------------------------------------- downloads
    def resolve_download(self, batch_id: str) -> tuple[Any, list[dict[str, Any]]]:
        """Authorise and verify a download of a published batch."""
        batch = self.db.query_one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise BatchNotFound(f"no batch named {batch_id!r}")
        if batch["phase"] != "published":
            raise DeskError(
                f"batch {batch_id!r} is {batch['phase']!r}; downloads require "
                "phase 'published'",
                code="batch_not_published",
                status=409,
            )
        if batch["failure_code"]:
            raise DeskError(
                f"batch {batch_id!r} has failure state {batch['failure_code']!r}",
                code="batch_failed",
                status=409,
            )
        files = self._files_for(batch)
        root = self.published_batch_dir(batch["uid"])
        problems = self._diagnose_published(root, files) if self.settings.verify_downloads else []
        for problem in problems:
            self._record_diagnostic(batch["uid"], batch["attempt"], **problem)
        if problems:
            raise DeskError(
                f"published files for {batch_id!r} failed verification: "
                + _summarize(problems),
                code="published_verification_failed",
                status=409,
            )
        return batch, files

    def download_entry(self, batch_id: str, rel_path: str) -> tuple[Path, dict[str, Any]]:
        """Look up a single manifest entry without re-hashing every other file.

        The whole-batch verification is performed when the download index is
        requested; individual file fetches validate only the requested entry so
        a large batch does not re-hash all files on every request.
        """
        batch = self.db.query_one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise BatchNotFound(f"no batch named {batch_id!r}")
        if batch["phase"] != "published" or batch["failure_code"]:
            raise DeskError(
                f"batch {batch_id!r} is not downloadable (phase="
                f"{batch['phase']}, failure={batch['failure_code']})",
                code="batch_not_published",
                status=409,
            )
        try:
            canonical = normalize_rel_path(rel_path)
        except ValueError as exc:
            raise DeskError(str(exc), code="invalid_file_path", status=400) from exc
        entry = next(
            (f for f in self._files_for(batch) if f["rel_path"] == canonical), None
        )
        if entry is None:
            raise DeskError(
                f"{canonical!r} is not part of batch {batch_id!r}",
                code="file_not_in_batch",
                status=404,
            )
        target = self.published_batch_dir(batch["uid"]) / canonical
        if not is_within(self.published_batch_dir(batch["uid"]), target) or not target.is_file():
            self._record_diagnostic(
                batch["uid"], batch["attempt"], code="published_missing_file",
                rel_path=canonical, detail="published file is missing at download time",
            )
            raise DeskError(
                "published file is missing",
                code="published_file_missing",
                status=410,
            )
        if self.settings.verify_downloads:
            size = target.stat().st_size
            digest = sha256_file(target)
            if size != entry["size"] or digest != entry["sha256"]:
                self._record_diagnostic(
                    batch["uid"], batch["attempt"],
                    code=("published_size_mismatch" if size != entry["size"] else "published_hash_mismatch"),
                    rel_path=canonical,
                    detail=f"expected {entry['size']} bytes / {entry['sha256']}",
                )
                raise DeskError(
                    f"published file {canonical!r} failed verification",
                    code="published_verification_failed",
                    status=409,
                )
        return target, entry

    def download_file_path(self, batch_id: str, checked: Any, rel_path: str) -> Path:
        """Resolve one file for streaming, using a prior ``resolve_download``."""
        return self.download_entry(batch_id, rel_path)[0]

    # ------------------------------------------------------------- helpers
    def _batch_by_uid(self, uid: str) -> Any:
        batch = self.db.query_one("SELECT * FROM batches WHERE uid=?", (uid,))
        if not batch:
            raise BatchNotFound(f"no batch with uid {uid!r}")
        return batch

    def _declarations(self, uid: str, attempt: int) -> list[Any]:
        return self.db.query(
            "SELECT rel_path, declared_size FROM batch_declarations "
            "WHERE batch_uid=? AND attempt=? ORDER BY rel_path",
            (uid, attempt),
        )

    def _files_for(self, batch: Any) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT rel_path, size, sha256, state FROM batch_files "
            "WHERE batch_uid=? AND attempt=? ORDER BY rel_path",
            (batch["uid"], batch["attempt"]),
        )
        return [
            {
                "rel_path": r["rel_path"],
                "size": r["size"],
                "sha256": r["sha256"],
                "state": r["state"],
            }
            for r in rows
        ]

    def _diagnostics(self, uid: str, attempt: int | None = None) -> list[dict[str, Any]]:
        if attempt is None:
            rows = self.db.query(
                "SELECT code, rel_path, detail, created_at FROM batch_diagnostics "
                "WHERE batch_uid=? ORDER BY id",
                (uid,),
            )
        else:
            rows = self.db.query(
                "SELECT code, rel_path, detail, created_at FROM batch_diagnostics "
                "WHERE batch_uid=? AND attempt=? ORDER BY id",
                (uid, attempt),
            )
        return [dict(r) for r in rows]

    def _snapshot(self, batch: Any, stage: str, *, require: bool) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM batch_snapshots WHERE batch_uid=? AND attempt=? AND stage=?",
            (batch["uid"], batch["attempt"], stage),
        )
        if not row:
            if require:
                raise DeskError(
                    f"missing {stage!r} snapshot for {batch['batch_id']!r}",
                    code="snapshot_missing",
                    status=500,
                )
            return {}
        manifest = parse_manifest(row["manifest_json"])
        return {
            "uid": batch["uid"],
            "batch_id": batch["batch_id"],
            "attempt": batch["attempt"],
            "phase": batch["phase"],
            "publish_state": batch["publish_state"],
            "stage": stage,
            "content_digest": row["content_digest"],
            "file_count": row["file_count"],
            "total_bytes": row["total_bytes"],
            "manifest": manifest,
            "files": self._files_for(batch),
            "downloadable": batch["phase"] == "published",
        }

    def _write_snapshot(
        self, uid: str, attempt: int, stage: str, digest: str, manifest_json: str
    ) -> None:
        entries = parse_manifest(manifest_json)
        count, total = manifest_statistics(entries)
        self.db.execute(
            """
            INSERT INTO batch_snapshots
                (batch_uid, attempt, stage, content_digest, file_count,
                 total_bytes, manifest_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_uid, attempt, stage) DO NOTHING
            """,
            (uid, attempt, stage, digest, count, total, manifest_json, utcnow()),
        )

    def _record_diagnostic(
        self, uid: str, attempt: int, *, code: str, rel_path: str, detail: str
    ) -> None:
        now = utcnow()
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO batch_diagnostics
                    (batch_uid, attempt, code, rel_path, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_uid, attempt, code, rel_path, detail) DO NOTHING
                """,
                (uid, attempt, code, rel_path, detail, now),
            )
        day = now[:10]
        diag_dir = self.settings.diagnostics_dir / day
        diag_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "batch_uid": uid,
            "attempt": attempt,
            "code": code,
            "rel_path": rel_path,
            "detail": detail,
            "at": now,
        }
        atomic_write_bytes(
            diag_dir
            / f"{uid}-{attempt}-{sha256_bytes((code + '|' + rel_path).encode())[:10]}.json",
            json.dumps(record, indent=2).encode(),
        )

    def _remove_empty_staging(self, uid: str) -> None:
        root = self.staging_batch_dir(uid)
        if root.exists():
            _remove_empty_dirs(root)


def _present_files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def _summarize(problems: Iterable[dict]) -> str:
    return "; ".join(f"{p.get('rel_path','')}: {p.get('code')}" for p in problems)
