"""Direct file copy for ROMs that don't need an installer (non-Windows
platforms, `stream_copy` in InstallCandidatesSchema): the ROM's own files are
copied byte-for-byte into the session cache, so a connected client streams
from the same place a Windows install's produced files come from.

Pure I/O helper: path resolution against the library lives in
handler/filesystem/roms_handler.py; this only moves bytes and reports
progress. Kept free of DB/session access so it's trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Matches manifest.py's chunk size; both walk the same files once each.
CHUNK_SIZE = 4 * 1024 * 1024


def copy_files(
    pairs: list[tuple[Path, Path]],
    *,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Copy each (src, dst) pair, chunked. Returns total bytes copied.

    `on_progress` is called with the cumulative bytes copied so far after
    each chunk, across all pairs (not reset per file).
    """
    copied = 0
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fsrc, dst.open("wb") as fdst:
            while chunk := fsrc.read(CHUNK_SIZE):
                fdst.write(chunk)
                copied += len(chunk)
                if on_progress:
                    on_progress(copied)
    return copied
