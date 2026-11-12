"""FastAPI application for the batch release desk.

Downloads are exposed only as per-file endpoints validated against a published
batch's manifest.  ``published/`` and ``staging/`` are never mounted as static
directories, so no URL can enumerate or fetch unreleased content.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .crashpoints import CrashPoints
from .db import Database
from .errors import BatchNotFound, DeskError
from .service import ReleaseDesk


class CreateFile(BaseModel):
    rel_path: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=0)


class CreateBatchRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=120)
    files: list[CreateFile] = Field(min_length=1, max_length=2000)
    content_digest: str | None = Field(default=None, max_length=64)


class SealRequest(BaseModel):
    content_digest: str | None = Field(default=None, max_length=64)


class PublishRequest(BaseModel):
    content_digest: str | None = Field(default=None, max_length=64)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_layout()
    database = Database(settings.db_path)
    desk = ReleaseDesk(settings, database, CrashPoints(settings.crash_points))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup recovery: the previous process may have died at any point.
        app.state.recovery = desk.recover()
        yield
        database.close()

    app = FastAPI(title="Batch Release Desk", version="0.1.0", lifespan=lifespan)
    app.state.desk = desk
    app.state.settings = settings
    app.state.recovery = None

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.exception_handler(DeskError)
    async def desk_error_handler(_: Request, exc: DeskError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "message": str(exc), "status": exc.status},
        )

    def uid_for(batch_id: str) -> str:
        row = database.query_one(
            "SELECT uid FROM batches WHERE batch_id=?", (batch_id,)
        )
        if not row:
            raise BatchNotFound(f"no batch named {batch_id!r}")
        return row["uid"]

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "staging_dir": str(settings.staging_dir),
            "published_dir": str(settings.published_dir),
            "database": str(settings.db_path),
            "crash_points": sorted(settings.crash_points),
            "startup_recovery": app.state.recovery,
        }

    @app.get("/api/recoveries")
    def recoveries(limit: int = 20) -> list[dict[str, Any]]:
        return desk.list_recovery_runs(min(max(limit, 1), 100))

    @app.post("/api/recover")
    def run_recovery() -> dict[str, Any]:
        result = desk.recover()
        app.state.recovery = result
        return result

    # --------------------------------------------------------------- batches
    @app.get("/api/batches")
    def list_batches() -> list[dict[str, Any]]:
        return desk.list_batches()

    @app.get("/api/batches/{batch_id}")
    def batch_detail(batch_id: str) -> dict[str, Any]:
        return desk.get_batch(uid_for(batch_id))

    @app.post("/api/batches", status_code=201)
    def create_batch(payload: CreateBatchRequest) -> JSONResponse:
        info, created = desk.create_batch(
            payload.batch_id, [(f.rel_path, f.size) for f in payload.files]
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "created": created,
                **info,
                "upload_url": f"/api/batches/{info['batch_id']}/files",
            },
        )

    @app.post("/api/batches/{batch_id}/files")
    def upload_file(
        batch_id: str,
        file: UploadFile = File(...),
        rel_path: str = Form(...),
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "uploaded": desk.write_upload(uid_for(batch_id), rel_path, file.file),
        }

    @app.post("/api/batches/{batch_id}/seal")
    def seal(batch_id: str, payload: SealRequest) -> dict[str, Any]:
        return desk.seal(uid_for(batch_id), payload.content_digest)

    @app.post("/api/batches/{batch_id}/publish")
    def publish(batch_id: str, payload: PublishRequest) -> dict[str, Any]:
        snapshot = desk.publish(uid_for(batch_id), payload.content_digest)
        return {
            "snapshot": snapshot,
            "batch_id": batch_id,
            "download_url": f"/api/downloads/{batch_id}",
        }

    @app.post("/api/batches/{batch_id}/cancel")
    def cancel(batch_id: str) -> dict[str, Any]:
        result = desk.cancel(uid_for(batch_id))
        result["batch_id"] = batch_id
        return result

    @app.post("/api/batches/{batch_id}/retry")
    def retry(batch_id: str) -> dict[str, Any]:
        return desk.start_new_attempt(uid_for(batch_id))

    # ------------------------------------------------------------- downloads
    @app.get("/api/downloads/{batch_id}")
    def download_index(batch_id: str) -> dict[str, Any]:
        batch, files = desk.resolve_download(batch_id)
        return {
            "batch_id": batch["batch_id"],
            "uid": batch["uid"],
            "phase": batch["phase"],
            "content_digest": batch["content_digest"],
            "published_at": batch["updated_at"],
            "files": [
                {
                    **entry,
                    "url": f"/api/downloads/{batch_id}/files/{entry['rel_path']}",
                }
                for entry in files
            ],
        }

    @app.get("/api/downloads/{batch_id}/files/{rel_path:path}")
    def download_file(batch_id: str, rel_path: str) -> FileResponse:
        path, entry = desk.download_entry(batch_id, rel_path)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={
                "ETag": f'"sha256:{entry["sha256"]}"',
                "X-Content-SHA256": entry["sha256"],
                "Cache-Control": "private, no-cache",
            },
        )

    # ------------------------------------------------------------------- ui
    frontend = settings.frontend_dir
    if frontend.is_dir() and (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(frontend), html=True), name="ui")
    else:

        @app.get("/")
        def ui_missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "frontend_not_built",
                    "message": f"build the frontend into {frontend} "
                    "(cd frontend && npm ci && npm run build)",
                },
            )

    return app
