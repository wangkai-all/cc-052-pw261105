"""Canonical manifests and content digests.

A manifest is an ordered list of ``{rel_path, size, sha256}`` entries.  The
content digest hashes that canonical JSON, so the same set of files produces
the same digest regardless of upload order.  This digest is what makes
"same batch id, different content" detectable even after a crash.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


class FileEntry(dict):
    """Small typed view over a manifest entry."""


def build_manifest(entries: Iterable[dict[str, Any]]) -> tuple[str, str]:
    """Return ``(manifest_json, content_digest)`` for the supplied entries."""
    canonical = sorted(
        (
            {
                "rel_path": str(entry["rel_path"]),
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in entries
        ),
        key=lambda item: item["rel_path"],
    )
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    import hashlib

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, digest


def parse_manifest(manifest_json: str) -> list[dict[str, Any]]:
    data = json.loads(manifest_json)
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON list")
    parsed: list[dict[str, Any]] = []
    for entry in data:
        parsed.append(
            {
                "rel_path": str(entry["rel_path"]),
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
        )
    return parsed


def manifest_statistics(entries: Iterable[dict[str, Any]]) -> tuple[int, int]:
    files = list(entries)
    return len(files), sum(int(item["size"]) for item in files)
