"""Filesystem primitives: safe relative paths, hashing and atomic moves."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

CHUNK = 1024 * 1024


class PathRejected(ValueError):
    """A relative path was empty, absolute, or attempted directory traversal."""


def normalize_rel_path(raw: str) -> str:
    """Return a canonical POSIX-style relative path or raise ``PathRejected``."""
    if not isinstance(raw, str):
        raise PathRejected("file name must be a string")
    value = raw.replace("\\", "/").strip()
    if not value:
        raise PathRejected("file name is empty")
    if "\x00" in value:
        raise PathRejected("file name contains a NUL byte")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathRejected(f"unsafe relative path: {raw!r}")
    if parts[0].startswith("/") or (len(parts[0]) > 1 and parts[0][1] == ":"):
        raise PathRejected(f"absolute paths are not allowed: {raw!r}")
    normalized = "/".join(parts)
    if len(normalized) > 240:
        raise PathRejected("relative path is too long")
    return normalized


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_path(path: Path) -> None:
    """Best-effort fsync of a file (directories are fsynced separately)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def remove_owned_path(root: Path, rel_path: str) -> bool:
    """Delete a single batch-owned file plus empty parent directories."""
    target = root / rel_path
    if not is_within(root, target):
        return False
    removed = False
    try:
        target.unlink()
        removed = True
    except FileNotFoundError:
        pass
    parent = target.parent
    while parent != root and is_within(root, parent):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return removed


def remove_tree(root: Path, rel_or_empty: str = "") -> None:
    target = root / rel_or_empty if rel_or_empty else root
    if root != target and not is_within(root, target):
        return
    shutil.rmtree(target, ignore_errors=True)


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    fsync_dir(target.parent)
