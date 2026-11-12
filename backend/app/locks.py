"""Per-batch advisory locks.

Cancel and publish both take the same exclusive lock for a batch, so their
decisions are serialised even before the SQLite transaction starts.  The lock
file is keyed by batch_id and is never deleted (deleting it would allow two
processes to lock two different inodes under the same name).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class BatchLock:
    def __init__(self, lock_dir: Path):
        self._dir = lock_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, batch_id: str) -> Path:
        digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:32]
        return self._dir / f"{digest}.lock"

    @contextmanager
    def acquire(
        self, batch_id: str, *, blocking: bool = True, timeout: float | None = 10.0
    ) -> Iterator[bool]:
        """Yield True if the lock was acquired.

        With ``blocking=False`` the call returns immediately and yields False
        when another holder owns the lock (used by recovery/retry endpoints to
        avoid piling up work).
        """
        path = self._path(batch_id)
        handle = path.open("a+")
        acquired = False
        try:
            if blocking:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            else:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = False
            yield acquired
        finally:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass
            handle.close()

    def cleanup_stale(self, max_age_seconds: float = 86400.0) -> None:
        """Remove lock files that are old; only for tests/maintenance."""
        import time

        now = time.time()
        for path in self._dir.glob("*.lock"):
            try:
                if now - os.stat(path).st_mtime > max_age_seconds:
                    path.unlink()
            except OSError:
                pass
