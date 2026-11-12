"""Deliberate, configurable process-death points used to exercise recovery.

Set ``BATCH_DESK_CRASH_POINTS`` to a comma separated list of point names.  When
a point is reached the process calls ``os._exit`` immediately, simulating the
service being killed rather than shutting down cleanly.  Nothing is flushed by
atexit handlers, which is the point.
"""

from __future__ import annotations

import os
from typing import Iterable

KNOWN_CRASH_POINTS = {
    # after every upload has been written and recorded in SQLite
    "after_files_written",
    # after the file_ready bookkeeping/snapshot has been committed
    "after_seal",
    # after all staging renames into the published directory completed
    "after_move",
    # immediately before the final published transaction commits
    "before_commit",
    # immediately after the final published transaction commits
    "after_commit",
}


class CrashPoints:
    def __init__(self, points: Iterable[str]):
        unknown = set(points) - KNOWN_CRASH_POINTS
        if unknown:
            raise ValueError(
                "unknown crash point(s): "
                + ", ".join(sorted(unknown))
                + "; valid: "
                + ", ".join(sorted(KNOWN_CRASH_POINTS))
            )
        self._points = frozenset(points)

    def hit(self, point: str) -> None:
        """Terminate the process if this crash point is armed."""
        if point in self._points:
            os._exit(75)

    def armed(self, point: str) -> bool:
        return point in self._points
