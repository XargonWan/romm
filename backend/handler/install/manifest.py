"""File manifest for a completed remote install.

Once an install (Windows sandbox run or direct stream-copy) has produced its
files, this hashes and lists them so a client can download and verify each
one individually. The manifest is a small JSON file dropped next to the
installed files, inside the session's own cache directory; every path in it
is relative to that directory, which is also what the download endpoint
resolves against (see endpoints/roms/install.py).

Kept free of DB/session access so it's trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

# Dropped alongside the installed files; excluded from the manifest's own
# listing and never served through the files endpoint.
MANIFEST_FILENAME = ".romm-install-manifest.json"

# Dropped alongside the installed files while the install is still running;
# deleted once MANIFEST_FILENAME is written, so "does this exist" cleanly
# means "still installing" (see scan_live_manifest below).
LIVE_MANIFEST_FILENAME = ".romm-install-manifest.live.json"

# Read/copy in chunks so a multi-GB game never gets loaded into memory whole.
# Also the piece size for the live manifest's incremental hashing - a client
# only ever needs to re-fetch one CHUNK_SIZE-sized piece to repair a
# corrupted spot in an otherwise-good multi-GB file.
CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One installed file, addressable by its path relative to the session's
    cache directory, with the sha1 a client uses to verify its own copy."""

    path: str
    size_bytes: int
    sha1: str


def hash_file_sha1(path: Path) -> str:
    """Stream-hash a file's contents."""
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def hash_files(
    paths: Iterable[Path],
    root: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> list[ManifestEntry]:
    """Hash each path, recording it relative to `root`.

    `on_progress` is called with the cumulative bytes hashed so far after
    each file, so a caller can mirror it into the session's bytes_written.
    """
    entries: list[ManifestEntry] = []
    hashed = 0
    for path in sorted(paths):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sha1 = hash_file_sha1(path)
        entries.append(
            ManifestEntry(
                path=path.relative_to(root).as_posix(),
                size_bytes=size,
                sha1=sha1,
            )
        )
        hashed += size
        if on_progress:
            on_progress(hashed)
    return entries


def build_manifest(
    root: Path,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> list[ManifestEntry]:
    """Hash every file under `root` (the stream-copy case: everything copied
    into the session cache directory is the install's output)."""
    files = (
        p
        for p in root.rglob("*")
        if p.is_file() and p.name != MANIFEST_FILENAME
    )
    return hash_files(files, root, on_progress=on_progress)


def total_size(paths: Iterable[Path]) -> int:
    """Sum of file sizes, skipping anything that disappears mid-scan."""
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def manifest_total_bytes(entries: list[ManifestEntry]) -> int:
    return sum(e.size_bytes for e in entries)


def write_manifest(cache_dir: Path, entries: list[ManifestEntry]) -> None:
    payload = {"files": [asdict(e) for e in entries]}
    (cache_dir / MANIFEST_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def read_manifest(cache_dir: Path) -> list[ManifestEntry] | None:
    """Read back a previously written manifest, or None if there isn't one
    yet (install still running, or nothing was produced)."""
    manifest_path = cache_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [ManifestEntry(**f) for f in payload["files"]]


def find_manifest_entry(
    entries: list[ManifestEntry], rel_path: str
) -> ManifestEntry | None:
    """Look up one manifest entry by its exact relative path.

    Only an exact, listed entry resolves — this is also the traversal guard
    for the download endpoint: a path that isn't in the manifest never
    reaches the filesystem.
    """
    for entry in entries:
        if entry.path == rel_path:
            return entry
    return None


# ── Live manifest: best-effort visibility into a still-running install ──
#
# The functions above only ever run once, after the installer has already
# exited (see runner._finalize_install) - that pass remains the single
# authoritative, fully-verified manifest. Everything below is a *best-effort*
# accelerant computed while the installer is still writing, so a client can
# start pulling finished pieces of a file before the whole install (and the
# real manifest) is done. It is never trusted the way MANIFEST_FILENAME is.


@dataclass(frozen=True, slots=True)
class ChunkEntry:
    """One hashed, sealed CHUNK_SIZE-sized piece of a still-growing file."""

    index: int
    sha1: str


@dataclass(frozen=True, slots=True)
class LiveManifestEntry:
    """One file's live state: how much of it exists, how much of that is
    hash-verified and safe to serve, and whether it's actually finished."""

    path: str
    size_bytes: int  # current on-disk size; grows over time
    sealed_bytes: int  # highest offset that is hash-verified and servable
    chunks: tuple[ChunkEntry, ...]
    complete: bool  # true once the whole file is confirmed final


def _sealable_chunk_count(size_bytes: int) -> int:
    """How many whole CHUNK_SIZE pieces are fully covered by `size_bytes`."""
    return size_bytes // CHUNK_SIZE


def _hash_range(path: Path, start: int, length: int) -> str:
    """Stream-hash exactly `length` bytes starting at `start`."""
    digest = hashlib.sha1()
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(remaining, CHUNK_SIZE))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def scan_live_manifest(
    root: Path,
    candidates: Iterable[Path],
    previous: Mapping[str, LiveManifestEntry] | None = None,
) -> dict[str, LiveManifestEntry]:
    """One incremental pass over the installer's in-progress output.

    A chunk only becomes sealed (hashed, safe to serve) once its size
    threshold has already held for one full scan interval - comparing this
    scan's coverage against `previous`'s recorded size is the two-scan
    confirmation, cheap protection against hashing bytes the installer might
    still be actively writing into. A sealed chunk is never re-hashed; a
    later call carries it forward from `previous` unchanged.
    """
    previous = previous or {}
    result: dict[str, LiveManifestEntry] = {}
    for path in candidates:
        try:
            size = path.stat().st_size
        except OSError:
            continue

        rel_path = path.relative_to(root).as_posix()
        prior = previous.get(rel_path)
        sealed_chunks: list[ChunkEntry] = list(prior.chunks) if prior else []

        # Only seal up to what was ALREADY sealable on the previous scan -
        # a chunk that just became coverable this scan waits one more
        # interval before it's trusted not to change further.
        prior_sealable = _sealable_chunk_count(prior.size_bytes) if prior else 0
        while len(sealed_chunks) < prior_sealable:
            index = len(sealed_chunks)
            sha1 = _hash_range(path, index * CHUNK_SIZE, CHUNK_SIZE)
            sealed_chunks.append(ChunkEntry(index=index, sha1=sha1))

        result[rel_path] = LiveManifestEntry(
            path=rel_path,
            size_bytes=size,
            sealed_bytes=len(sealed_chunks) * CHUNK_SIZE,
            chunks=tuple(sealed_chunks),
            complete=False,
        )
    return result


def live_view_of_final_manifest(
    entries: list[ManifestEntry],
) -> dict[str, LiveManifestEntry]:
    """Present an already-finished install's real manifest in the same shape
    as a live one, so a client can poll one endpoint regardless of state."""
    return {
        e.path: LiveManifestEntry(
            path=e.path,
            size_bytes=e.size_bytes,
            sealed_bytes=e.size_bytes,
            chunks=(),  # already whole-file verified; no per-chunk hashes needed
            complete=True,
        )
        for e in entries
    }


def write_live_manifest(
    cache_dir: Path, entries: Mapping[str, LiveManifestEntry]
) -> None:
    """Write the live manifest atomically - it's read concurrently by the web
    process while this keeps overwriting it every scan interval, so a plain
    in-place write could hand a reader a half-written JSON body."""
    payload = {
        "files": [
            {
                "path": e.path,
                "size_bytes": e.size_bytes,
                "sealed_bytes": e.sealed_bytes,
                "complete": e.complete,
                "chunks": [asdict(c) for c in e.chunks],
            }
            for e in entries.values()
        ]
    }
    tmp_path = cache_dir / f"{LIVE_MANIFEST_FILENAME}.tmp"
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(cache_dir / LIVE_MANIFEST_FILENAME)


def read_live_manifest(cache_dir: Path) -> dict[str, LiveManifestEntry] | None:
    """Read back the live manifest, or None if there isn't one (install not
    running, finished, or never got as far as writing one)."""
    manifest_path = cache_dir / LIVE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    entries: dict[str, LiveManifestEntry] = {}
    for f in payload.get("files", []):
        chunks = tuple(ChunkEntry(**c) for c in f.get("chunks", []))
        entries[f["path"]] = LiveManifestEntry(
            path=f["path"],
            size_bytes=f["size_bytes"],
            sealed_bytes=f["sealed_bytes"],
            chunks=chunks,
            complete=f.get("complete", False),
        )
    return entries


def delete_live_manifest(cache_dir: Path) -> None:
    """Best-effort cleanup once the real manifest is written (or the install
    failed) - the live manifest has no further reason to exist either way."""
    (cache_dir / LIVE_MANIFEST_FILENAME).unlink(missing_ok=True)
    (cache_dir / f"{LIVE_MANIFEST_FILENAME}.tmp").unlink(missing_ok=True)
