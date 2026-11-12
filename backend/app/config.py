"""Runtime configuration for the batch release desk.

The staging and published directories must live on the same filesystem so that
moving a file is an atomic rename.  All paths may be overridden with
``BATCH_DESK_*`` environment variables (used by tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when the desk cannot start with the supplied configuration."""


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    data_root: Path
    staging_dir: Path
    published_dir: Path
    db_path: Path
    diagnostics_dir: Path
    tmp_dir: Path
    lock_dir: Path
    frontend_dir: Path
    crash_points: frozenset[str] = field(default_factory=frozenset)
    max_files: int = 2_000
    max_file_bytes: int = 10 * 1024 * 1024 * 1024
    max_batch_bytes: int = 20 * 1024 * 1024 * 1024
    cors_origins: tuple[str, ...] = ()
    verify_downloads: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        root = _env_path("BATCH_DESK_DATA_DIR", REPO_ROOT / "data")
        staging = _env_path("BATCH_DESK_STAGING_DIR", root / "staging")
        published = _env_path("BATCH_DESK_PUBLISHED_DIR", root / "published")
        db_path = _env_path("BATCH_DESK_DB_PATH", root / "batch_desk.sqlite3")
        diagnostics = _env_path("BATCH_DESK_DIAGNOSTICS_DIR", root / "diagnostics")
        tmp = _env_path("BATCH_DESK_TMP_DIR", root / ".tmp")
        locks = _env_path("BATCH_DESK_LOCK_DIR", root / ".locks")
        frontend = _env_path(
            "BATCH_DESK_FRONTEND_DIR", REPO_ROOT / "frontend" / "dist"
        )
        crash_points = frozenset(
            p.strip()
            for p in os.environ.get("BATCH_DESK_CRASH_POINTS", "").split(",")
            if p.strip()
        )
        origins = tuple(
            o.strip()
            for o in os.environ.get("BATCH_DESK_CORS_ORIGINS", "").split(",")
            if o.strip()
        )
        return cls(
            data_root=root,
            staging_dir=staging,
            published_dir=published,
            db_path=db_path,
            diagnostics_dir=diagnostics,
            tmp_dir=tmp,
            lock_dir=locks,
            frontend_dir=frontend,
            crash_points=crash_points,
            max_files=_env_int("BATCH_DESK_MAX_FILES", 2_000),
            max_file_bytes=_env_int("BATCH_DESK_MAX_FILE_BYTES", 10 * 1024**3),
            max_batch_bytes=_env_int("BATCH_DESK_MAX_BATCH_BYTES", 20 * 1024**3),
            cors_origins=origins,
            verify_downloads=os.environ.get("BATCH_DESK_VERIFY_DOWNLOADS", "1")
            not in {"0", "false", "no"},
        )

    def ensure_layout(self) -> None:
        """Create directories and verify the atomic-rename precondition."""
        for directory in (
            self.data_root,
            self.staging_dir,
            self.published_dir,
            self.diagnostics_dir,
            self.tmp_dir,
            self.lock_dir,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._assert_same_device()

    def _assert_same_device(self) -> None:
        devices: dict[str, int] = {}
        for label, directory in (
            ("staging", self.staging_dir),
            ("published", self.published_dir),
            ("diagnostics", self.diagnostics_dir),
            ("tmp", self.tmp_dir),
            ("database", self.db_path.parent),
        ):
            try:
                devices[label] = os.stat(directory).st_dev
            except OSError as exc:  # pragma: no cover - defensive
                raise ConfigError(f"cannot stat {label} directory {directory}: {exc}")
        if len(set(devices.values())) > 1:
            detail = ", ".join(f"{k}={v}" for k, v in devices.items())
            raise ConfigError(
                "staging, published, diagnostics and database must be on the same "
                f"filesystem so file moves are atomic renames; got {detail}"
            )


def batch_uid_is_valid(uid: str) -> bool:
    return (
        isinstance(uid, str)
        and 16 <= len(uid) <= 48
        and all(c.isalnum() or c in "-_" for c in uid)
    )
