"""Happy path, idempotency and conflict behaviour."""

from __future__ import annotations

import io

import pytest

from app.errors import BatchFinished, Conflict, UploadRejected

from conftest import make_published, upload


def test_happy_path_records_all_three_phases(desk):
    info, created = desk.create_batch("release-1", [("one.csv", 6), ("sub/two.txt", 3)])
    assert created is True
    uid = info["uid"]
    assert desk.get_batch(uid)["phase"] == "preparing"

    upload(desk, uid, "one.csv", b"1,2,3\n")
    upload(desk, uid, "sub/two.txt", b"abc")
    assert desk.get_batch(uid)["phase"] == "preparing"

    sealed = desk.seal(uid)
    assert sealed["phase"] == "file_ready"
    assert sealed["stage"] == "file_ready"
    assert sealed["file_count"] == 2
    assert sealed["total_bytes"] == 9

    published = desk.publish(uid)
    assert published["phase"] == "published"
    assert published["publish_state"] == "committed"
    assert published["downloadable"] is True
    assert {f["state"] for f in published["files"]} == {"moved"}

    detail = desk.get_batch(uid)
    stages = {s["stage"] for s in detail["snapshots"]}
    assert stages == {"staged", "file_ready", "moved", "published"}
    digests = {s["content_digest"] for s in detail["snapshots"]}
    assert len(digests) == 1

    assert not (desk.staging_batch_dir(uid)).exists()
    target = desk.published_batch_dir(uid)
    assert (target / "one.csv").read_bytes() == b"1,2,3\n"
    assert (target / "sub/two.txt").read_bytes() == b"abc"


def test_files_not_downloadable_before_publish(desk):
    info, _ = desk.create_batch("pending", [("x.txt", 3)])
    uid = info["uid"]
    upload(desk, uid, "x.txt", b"bad")
    desk.seal(uid)
    with pytest.raises(Exception) as exc:
        desk.resolve_download("pending")
    assert "not_published" in str(exc.value) or "published" in str(exc.value)


def test_download_after_publish(desk):
    batch = make_published(desk, "dl")
    checked = desk.resolve_download("dl")
    path = desk.download_file_path("dl", checked, "a.txt")
    assert path.read_bytes() == b"hello"
    with pytest.raises(Exception):
        desk.download_file_path("dl", checked, "missing.txt")
    with pytest.raises(Exception):
        desk.download_file_path("dl", checked, "../a.txt")


def test_create_is_idempotent_for_same_declaration(desk):
    declared = [("f.txt", 4)]
    first, created = desk.create_batch("same", declared)
    again, created_again = desk.create_batch("same", declared)
    assert created is True
    assert created_again is False
    assert first["uid"] == again["uid"]


def test_same_id_different_declaration_conflicts(desk):
    desk.create_batch("keep", [("f.txt", 4)])
    with pytest.raises(Conflict):
        desk.create_batch("keep", [("f.txt", 5)])
    with pytest.raises(Conflict):
        desk.create_batch("keep", [("other.txt", 4)])


def test_same_id_different_content_conflicts_after_reseal_attempt(desk):
    # Start a failed attempt, then swap bytes under a fresh attempt: the
    # sealed digest must still win.
    info, _ = desk.create_batch("content", [("f.txt", 3)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"aaa")
    desk.seal(uid)
    with pytest.raises(Conflict):
        desk.seal(uid, expected_digest="0" * 64)
    desk.publish(uid)
    with pytest.raises(BatchFinished):
        desk.seal(uid)


def test_repeat_seal_and_publish_are_idempotent_no_duplicate_snapshots(desk):
    info, _ = desk.create_batch("twice", [("f.txt", 2)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"ok")
    desk.seal(uid)
    desk.seal(uid)
    desk.publish(uid)
    detail = desk.get_batch(uid)
    stages = [s["stage"] for s in detail["snapshots"]]
    assert stages == ["staged", "file_ready", "moved", "published"]
    assert stages.count("file_ready") == 1


def test_publish_before_seal_records_request_and_returns_pending(desk):
    info, _ = desk.create_batch("early", [("f.txt", 1)])
    uid = info["uid"]
    with pytest.raises(Exception, match="sealed|file_ready|publish"):
        desk.publish(uid)
    assert desk.get_batch(uid)["publish_requested_at"] is not None


def test_undeclared_upload_rejected(desk):
    info, _ = desk.create_batch("decl", [("known.txt", 1)])
    with pytest.raises(UploadRejected):
        upload(desk, info["uid"], "unknown.txt", b"z")


def test_duplicate_upload_rejected(desk):
    info, _ = desk.create_batch("dup", [("f.txt", 2)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"ab")
    with pytest.raises(UploadRejected):
        upload(desk, uid, "f.txt", b"ab")


def test_traversal_and_absolute_paths_rejected(desk):
    info, _ = desk.create_batch("paths", [("ok.txt", 1)])
    uid = info["uid"]
    for bad in ("../escape.txt", "/etc/passwd", "a/../../b"):
        with pytest.raises(UploadRejected):
            upload(desk, uid, bad, b"x")


def test_seal_requires_all_declared_files(desk):
    info, _ = desk.create_batch("partial", [("a.txt", 1), ("b.txt", 1)])
    uid = info["uid"]
    upload(desk, uid, "a.txt", b"a")
    with pytest.raises(UploadRejected, match="not been uploaded"):
        desk.seal(uid)


def test_seal_detects_declared_size_mismatch(desk):
    info, _ = desk.create_batch("sized", [("a.txt", 5)])
    uid = info["uid"]
    upload(desk, uid, "a.txt", b"abc")
    with pytest.raises(UploadRejected, match="declared size|size"):
        desk.seal(uid)


def test_cancel_removes_only_owned_files_and_blocks_later_publish(desk):
    info, _ = desk.create_batch("c1", [("a.txt", 1), ("b.txt", 1)])
    uid = info["uid"]
    upload(desk, uid, "a.txt", b"a")
    result = desk.cancel(uid)
    assert result["cancelled"] is True
    assert not (desk.staging_batch_dir(uid) / "a.txt").exists()
    detail = desk.get_batch(uid)
    assert detail["failure_code"] == "cancelled"
    assert detail["downloadable"] is False
    with pytest.raises(Exception, match="cancelled"):
        desk.publish(uid)


def test_cancel_after_publish_reports_already_published(desk):
    batch = make_published(desk, "done")
    with pytest.raises(BatchFinished, match="already published"):
        desk.cancel(batch["uid"])
    keep = desk.download_file_path(
        "done", desk.resolve_download("done"), "a.txt"
    )
    assert keep.exists()
    assert keep.read_bytes() == b"hello"


def test_cancel_does_not_leak_unrelated_staging_files(desk):
    info, _ = desk.create_batch("mine", [("mine.txt", 1)])
    uid = info["uid"]
    upload(desk, uid, "mine.txt", b"m")
    desk.create_batch("theirs", [("theirs.txt", 1)])
    theirs_uid = desk.list_batches()[0]["uid"]
    # Upload into theirs too; cancel of "mine" must not touch it.
    upload(desk, theirs_uid, "theirs.txt", b"t")
    desk.cancel(uid)
    assert (desk.staging_batch_dir(theirs_uid) / "theirs.txt").exists()


def test_verification_failure_persists_and_enables_retry(desk):
    info, _ = desk.create_batch("damage", [("f.txt", 3)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"abc")
    # Corrupt the staged bytes behind the desk's back.
    (desk.staging_batch_dir(uid) / "f.txt").write_bytes(b"zzz")
    with pytest.raises(Exception, match="verification"):
        desk.seal(uid)
    detail = desk.get_batch(uid)
    assert detail["failure_code"] == "staged_verification_failed"
    assert detail["failed_at"] is not None
    # Retry is offered and works.
    retried = desk.start_new_attempt(uid)
    assert retried["phase"] == "preparing"
    assert retried["attempt"] == 1


def test_failed_batch_stays_failed_across_repeated_recovery(desk):
    info, _ = desk.create_batch("stays", [("f.txt", 1)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"x")
    (desk.staging_batch_dir(uid) / "f.txt").unlink()
    desk.recover()
    first = desk.get_batch(uid)
    assert first["failure_code"]
    desk.recover()
    desk.recover()
    again = desk.get_batch(uid)
    assert again["failure_code"] == first["failure_code"]
    assert again["failure_reason"] == first["failure_reason"]
    assert len(again["diagnostics"]) == len(first["diagnostics"])


def test_download_entry_rejects_bad_paths_without_stopiteration(desk):
    make_published(desk, "safe")
    with pytest.raises(Exception, match="not part|invalid|escape"):
        desk.download_entry("safe", "unknown.tx")
    with pytest.raises(Exception):
        desk.download_entry("safe", "/etc/passwd")
    target, entry = desk.download_entry("safe", "a.txt")
    assert target.read_bytes() == b"hello"
    assert entry["sha256"]


def test_publish_request_before_seal_is_honoured_by_seal(desk):
    info, _ = desk.create_batch("auto", [("f.txt", 3)])
    uid = info["uid"]
    with pytest.raises(Exception, match="sealed|file_ready|publish"):
        desk.publish(uid)
    upload(desk, uid, "f.txt", b"abc")
    sealed = desk.seal(uid)
    assert sealed["phase"] == "published"
    assert sealed["downloadable"] is True
    assert desk.resolve_download("auto")[1]


def test_declared_file_named_part_is_an_ordinary_file(desk):
    """The upload temp suffix lives in tmp/, so "*.part" is a valid data name."""
    info, _ = desk.create_batch("dots", [("notes.part", 4), ("a.part/b.part", 3)])
    uid = info["uid"]
    upload(desk, uid, "notes.part", b"data")
    upload(desk, uid, "a.part/b.part", b"raw")
    sealed = desk.seal(uid)
    assert sealed["phase"] == "file_ready"
    desk.publish(uid)
    assert desk.resolve_download("dots")[1]
    target, _ = desk.download_entry("dots", "notes.part")
    assert target.read_bytes() == b"data"


def test_failed_batch_cannot_be_published_or_resealed(desk, monkeypatch):
    """A latched failure must not slip through to published-but-undownloadable."""
    from app.errors import BatchFailed

    info, _ = desk.create_batch("latched", [("f.txt", 3)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"abc")
    (desk.staging_batch_dir(uid) / "f.txt").write_bytes(b"zzz")
    with pytest.raises(Exception, match="verification"):
        desk.seal(uid)
    assert desk.get_batch(uid)["failed_at"]
    # Repair the bytes: the batch stays failed until an explicit new attempt.
    (desk.staging_batch_dir(uid) / "f.txt").write_bytes(b"abc")
    with pytest.raises(BatchFailed):
        desk.seal(uid)
    with pytest.raises(BatchFailed):
        desk.publish(uid)
    assert desk.get_batch(uid)["phase"] != "published"
    retried = desk.start_new_attempt(uid)
    assert retried["failure_code"] is None


def test_same_id_different_content_rejected_before_first_seal(desk):
    """The guard must not depend on a digest having been persisted by a seal."""
    from app.errors import Conflict

    info, _ = desk.create_batch("witness", [("f.txt", 3)])
    uid = info["uid"]
    upload(desk, uid, "f.txt", b"aaa")
    (desk.staging_batch_dir(uid) / "f.txt").unlink()
    desk.recover()  # records a failure; content_digest was never set
    assert desk.get_batch(uid)["content_digest"] is None
    desk.start_new_attempt(uid)
    with pytest.raises(Conflict, match="different content"):
        upload(desk, uid, "f.txt", b"bbb")
    upload(desk, uid, "f.txt", b"aaa")
    desk.seal(uid)
    desk.publish(uid)
    assert desk.get_batch(uid)["phase"] == "published"
