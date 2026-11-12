"""HTTP-level tests: uploads, the download gate and error shapes."""

from __future__ import annotations

import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.service import ReleaseDesk
from app.crashpoints import CrashPoints
from app.db import Database


@pytest.fixture()
def api(tmp_path):
    settings = Settings(
        data_root=tmp_path,
        staging_dir=tmp_path / "staging",
        published_dir=tmp_path / "published",
        db_path=tmp_path / "desk.sqlite3",
        diagnostics_dir=tmp_path / "diagnostics",
        tmp_dir=tmp_path / ".tmp",
        lock_dir=tmp_path / ".locks",
        frontend_dir=tmp_path / "no-frontend",
    )
    application = create_app(settings)
    with TestClient(application) as client:
        yield client, application


def _create(client, batch_id, files):
    response = client.post(
        "/api/batches",
        json={
            "batch_id": batch_id,
            "files": [{"rel_path": name, "size": len(data)} for name, data in files.items()],
        },
    )
    return response


def _upload(client, batch_id, name, data):
    return client.post(
        f"/api/batches/{batch_id}/files",
        data={"rel_path": name},
        files={"file": (name, data, "application/octet-stream")},
    )


def test_full_release_over_http(api):
    client, _ = api
    files = {"one.txt": b"one", "deep/two.bin": b"two-two"}
    created = _create(client, "web", files)
    assert created.status_code == 201
    uid = created.json()["uid"]

    # Download endpoint refuses before publication.
    blocked = client.get("/api/downloads/web")
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "batch_not_published"

    for name, data in files.items():
        uploaded = _upload(client, "web", name, data)
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["uploaded"]["sha256"] == hashlib.sha256(data).hexdigest()

    sealed = client.post("/api/batches/web/seal", json={})
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["phase"] == "file_ready"

    # Still not downloadable while merely file_ready.
    assert client.get("/api/downloads/web").status_code == 409

    published = client.post("/api/batches/web/publish", json={})
    assert published.status_code == 200, published.text
    assert published.json()["snapshot"]["phase"] == "published"

    index = client.get("/api/downloads/web")
    assert index.status_code == 200
    body = index.json()
    assert body["phase"] == "published"
    assert sorted(f["rel_path"] for f in body["files"]) == ["deep/two.bin", "one.txt"]

    single = client.get("/api/downloads/web/files/one.txt")
    assert single.status_code == 200
    assert single.content == b"one"
    assert single.headers["etag"] == f'"sha256:{hashlib.sha256(b"one").hexdigest()}"'

    # Unknown or unowned paths are refused, not passed through.
    assert client.get("/api/downloads/web/files/nope.txt").status_code == 404
    assert client.get("/api/downloads/web/files/../../desk.sqlite3").status_code in (404, 409)
    assert client.get("/api/downloads/missing").status_code == 404

    listing = client.get("/api/batches").json()
    assert listing[0]["downloadable"] is True
    detail = client.get("/api/batches/web").json()
    assert [s["stage"] for s in detail["snapshots"]] == [
        "staged", "file_ready", "moved", "published",
    ]
    assert detail["downloadable"] is True


def test_staging_and_published_dirs_are_not_served(api):
    client, _ = api
    files = {"secret.txt": b"s3cret"}
    _create(client, "static-probe", files)
    _upload(client, "static-probe", "secret.txt", b"s3cret")
    # No static mount for staging; the published dir is not browsable either.
    for path in ("/staging", "/published", "/data", "/api/../published"):
        response = client.get(path)
        assert response.status_code in (404, 405, 503)


def test_http_error_codes(api):
    client, _ = api
    files = {"x.txt": b"x"}
    _create(client, "codes", files)
    response = client.post(
        "/api/batches/codes/files",
        data={"rel_path": "y.txt"},
        files={"file": ("y.txt", b"y", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "upload_rejected"

    declared_again = _create(client, "codes", {"other.txt": b"z"})
    assert declared_again.status_code == 409
    assert declared_again.json()["error"] == "content_conflict"

    same_again = _create(client, "codes", files)
    assert same_again.status_code == 200
    assert same_again.json()["created"] is False


def test_cancel_over_http_and_published_stays(api):
    client, _ = api
    _create(client, "cancelme", {"a.txt": b"a"})
    _upload(client, "cancelme", "a.txt", b"a")
    cancelled = client.post("/api/batches/cancelme/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    detail = client.get("/api/batches/cancelme").json()
    assert detail["failure_code"] == "cancelled"
    assert client.get("/api/downloads/cancelme").status_code == 409

    files = {"done.txt": b"done"}
    _create(client, "finished", files)
    _upload(client, "finished", "done.txt", b"done")
    client.post("/api/batches/finished/seal", json={})
    client.post("/api/batches/finished/publish", json={})
    late = client.post("/api/batches/finished/cancel")
    assert late.status_code == 409
    assert late.json()["error"] == "already_published"
    assert client.get("/api/downloads/finished/files/done.txt").content == b"done"


def test_cancel_and_publish_serialise_on_the_same_lock(api, monkeypatch):
    """If publish holds the lock, a concurrent cancel waits, then sees
    'published' and refuses to delete the visible files."""

    client, application = api
    desk = application.state.desk
    files = {"race.txt": b"race"}
    info, _ = desk.create_batch("race", [("race.txt", 4)])
    uid = info["uid"]
    desk.write_upload(uid, "race.txt", __import__("io").BytesIO(b"race"))
    desk.seal(uid)

    publish_started = threading.Event()
    publish_release = threading.Event()
    original_finalize = desk._finalize

    def slow_finalize(batch):
        publish_started.set()
        publish_release.wait(timeout=10)
        return original_finalize(batch)

    monkeypatch.setattr(desk, "_finalize", slow_finalize)
    outcomes: dict[str, object] = {}

    def do_publish():
        try:
            outcomes["publish"] = desk.publish(uid)["phase"]
        except Exception as exc:  # noqa: BLE001
            outcomes["publish"] = exc

    def do_cancel():
        publish_started.wait(timeout=10)
        try:
            outcomes["cancel"] = desk.cancel(uid)
        except Exception as exc:  # noqa: BLE001
            outcomes["cancel"] = exc

    publisher = threading.Thread(target=do_publish)
    canceller = threading.Thread(target=do_cancel)
    publisher.start()
    canceller.start()
    publish_started.wait(timeout=10)
    time.sleep(0.1)
    publish_release.set()
    publisher.join(timeout=30)
    canceller.join(timeout=30)

    assert outcomes["publish"] == "published"
    assert isinstance(outcomes["cancel"], Exception)
    assert "already" in str(outcomes["cancel"]).lower()
    assert (desk.published_batch_dir(uid) / "race.txt").read_bytes() == b"race"
    assert desk.resolve_download("race")[1]


def test_health_and_recovery_endpoint(api):
    client, _ = api
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["startup_recovery"]["summary"]["checked"] == 0
    result = client.post("/api/recover").json()
    assert result["summary"]["checked"] == 0
    assert client.get("/api/recoveries").json()[-1]["summary"]["checked"] == 0
