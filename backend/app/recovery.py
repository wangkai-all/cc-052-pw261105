"""On-demand inspection helpers usable by the API and by tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .service import ReleaseDesk


def _present(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def inspect_placement(desk: ReleaseDesk, batch: dict[str, Any]) -> dict[str, Any]:
    """Report where each manifest file physically lives right now."""
    uid = batch["uid"]
    staged = _present(desk.staging_batch_dir(uid))
    published = _present(desk.published_batch_dir(uid))
    expected = {f["rel_path"] for f in batch["files"]}
    return {
        "in_staging": len(staged & expected),
        "in_published": len(published & expected),
        "unexpected_in_staging": sorted(staged - expected),
        "unexpected_in_published": sorted(published - expected),
        "expected": len(expected),
    }
