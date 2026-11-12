"""End-to-end test against a real uvicorn process.

The server is started with a crash point armed, asked to publish, and killed
by ``os._exit`` inside the request.  Then a second uvicorn process is started
against the same data directory; its startup recovery must finish the release
and the download endpoints must serve complete files.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def env_for(data_dir: Path, crash_points: str = "") -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATCH_DESK_")}
    env.update(
        {
            "BATCH_DESK_DATA_DIR": str(data_dir),
            "BATCH_DESK_FRONTEND_DIR": str(BACKEND.parent / "frontend" / "dist"),
            "BATCH_DESK_CRASH_POINTS": crash_points,
            "PYTHONPATH": str(BACKEND),
        }
    )
    return env


class Server:
    def __init__(self, data_dir: Path, crash_points: str = ""):
        self.port = free_port()
        self.data_dir = data_dir
        self.crash_points = crash_points
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "Server":
        self._log = open(self.data_dir / "server.log", "ab")
        self.process = subprocess.Popen(
            [
                PYTHON, "-I", "-m", "uvicorn", "app.main:create_app",
                "--factory",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--log-level", "info",
            ],
            cwd=str(BACKEND),
            env=env_for(self.data_dir, self.crash_points),
            stdout=self._log,
            stderr=self._log,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.process.poll() is not None:
                self._log.flush()
                log = (self.data_dir / "server.log").read_text(errors="replace")
                raise RuntimeError(f"server exited early:\n{log[-2000:]}")
            try:
                with httpx.Client(timeout=1.0) as client:
                    if client.get(self.url("/api/health")).status_code == 200:
                        return self
            except httpx.HTTPError:
                time.sleep(0.15)
        raise RuntimeError("server did not become healthy in time")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __exit__(self, *exc_info) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.process.kill()
        if getattr(self, "_log", None):
            self._log.close()


def test_ui_assets_served_from_fastapi(tmp_path):
    dist = BACKEND.parent / "frontend" / "dist"
    if not dist.is_dir():
        pytest.skip("frontend not built")
    with Server(tmp_path) as server:
        with httpx.Client(timeout=10) as client:
            root = client.get(server.url("/"))
            assert root.status_code == 200
            assert "文件发布台" in root.text


def test_real_process_kill_and_restart_then_download(tmp_path):
    files = {
        "metrics/daily.csv": b"date,value\n2026-11-12,42\n",
        "notes/readme.txt": b"release notes",
    }
    declared = [{"rel_path": name, "size": len(data)} for name, data in files.items()]

    with Server(tmp_path) as server:
        with httpx.Client(timeout=30) as client:
            created = client.post(server.url("/api/batches"), json={"batch_id": "e2e", "files": declared})
            assert created.status_code == 201, created.text
            for name, data in files.items():
                response = client.post(
                    server.url("/api/batches/e2e/files"),
                    data={"rel_path": name},
                    files={"file": (Path(name).name, data, "application/octet-stream")},
                )
                assert response.status_code == 200, response.text
            sealed = client.post(server.url("/api/batches/e2e/seal"), json={})
            assert sealed.status_code == 200, sealed.text

    # Restart with a kill point armed after all renames, then request publish.
    with Server(tmp_path, "after_move") as server:
        with httpx.Client(timeout=30) as client:
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                client.post(server.url("/api/batches/e2e/publish"), json={})
        process = server.process
        assert process is not None
        process.wait(timeout=30)
        # os._exit(75) from the crash point
        assert process.returncode == 75

    # A third process starts with no crash points; startup recovery runs.
    with Server(tmp_path) as server:
        with httpx.Client(timeout=30) as client:
            health = client.get(server.url("/api/health")).json()
            recovery = health["startup_recovery"]
            assert recovery["summary"]["published"] == 1, recovery

            detail = client.get(server.url("/api/batches/e2e")).json()
            assert detail["phase"] == "published"
            assert detail["downloadable"] is True

            index = client.get(server.url("/api/downloads/e2e")).json()
            assert sorted(f["rel_path"] for f in index["files"]) == sorted(files)

            for name, data in files.items():
                downloaded = client.get(server.url(f"/api/downloads/e2e/files/{name}"))
                assert downloaded.status_code == 200
                assert downloaded.content == data
                assert downloaded.headers["x-content-sha256"] == hashlib.sha256(data).hexdigest()

    # Restart again: recovery is idempotent, no duplicate snapshot rows.
    with Server(tmp_path) as server:
        with httpx.Client(timeout=30) as client:
            health = client.get(server.url("/api/health")).json()
            assert health["startup_recovery"]["summary"]["checked"] == 0
            detail = client.get(server.url("/api/batches/e2e")).json()
            assert [s["stage"] for s in detail["snapshots"]] == [
                "staged", "file_ready", "moved", "published",
            ]
            runs = client.get(server.url("/api/recoveries")).json()
            # Four process starts; runs are listed newest first: this
            # idempotency check, the successful resumer, the crashed-at-move
            # process, and the initial upload session.
            assert len(runs) == 4
            assert [r["published_resumed"] for r in runs] == [0, 1, 0, 0]
            assert runs[0]["batches_checked"] == 0


def test_corrupt_file_after_publish_is_not_downloaded(tmp_path):
    data = b"payload"
    with Server(tmp_path) as server:
        with httpx.Client(timeout=30) as client:
            created = client.post(
                server.url("/api/batches"),
                json={"batch_id": "corrupt", "files": [{"rel_path": "f.bin", "size": len(data)}]},
            )
            uid = created.json()["uid"]
            client.post(
                server.url("/api/batches/corrupt/files"),
                data={"rel_path": "f.bin"},
                files={"file": ("f.bin", data, "application/octet-stream")},
            )
            client.post(server.url("/api/batches/corrupt/seal"), json={})
            client.post(server.url("/api/batches/corrupt/publish"), json={})
            (tmp_path / "published" / uid / "f.bin").write_bytes(b"tampered")

            blocked = client.get(server.url("/api/downloads/corrupt"), follow_redirects=False)
            assert blocked.status_code == 409
            assert blocked.json()["error"] == "published_verification_failed"

            detail = client.get(server.url("/api/batches/corrupt")).json()
            codes = {d["code"] for d in detail["diagnostics"]}
            assert any("size_mismatch" in code or "hash_mismatch" in code for code in codes)
