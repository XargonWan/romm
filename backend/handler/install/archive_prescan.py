"""Extract-then-rescan for installer candidates that are themselves an
archive or disc image (a distributor .zip, a game ISO, ...) rather than a
directly-runnable file.

`installer_detection.detect_installer_candidates` only classifies files by
name/extension - it never looks inside an archive. This module bridges that
gap: extract the archive's full contents into a scratch directory, then run
the exact same detection logic against what's actually inside it, "come di
consueto" - reusing detection rather than inventing a second ranking scheme.

The scratch directory is a plain `tempfile.TemporaryDirectory()` (host /tmp
by default) - deliberately never under `INSTALL_CACHE_PATH`, since it holds
someone else's copy of the ROM's own archive contents, not the install's own
output. The caller owns its lifetime and must `.cleanup()` it once the
installer has actually run (the sandbox needs to keep reading from it for
the whole run).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from handler.filesystem.installer_detection import (
    ARCHIVE_EXTENSIONS,
    DISC_IMAGE_EXTENSIONS,
    DetectedFile,
    InstallerCandidate,
    detect_installer_candidates,
)
from logger.logger import log
from utils.archives import extract_archive_tree

_PRE_SCAN_EXTENSIONS = ARCHIVE_EXTENSIONS | DISC_IMAGE_EXTENSIONS


def is_archive_candidate(path: Path) -> bool:
    """Whether `path` needs extraction before it can be searched for an
    installer, rather than being runnable/openable as-is."""
    return path.suffix.lower() in _PRE_SCAN_EXTENSIONS


def _list_files_flat(root: Path) -> list[DetectedFile]:
    detected: list[DetectedFile] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        detected.append(
            DetectedFile(path=p.relative_to(root).as_posix(), size_bytes=size)
        )
    return detected


def extract_and_rescan(
    archive_path: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path, InstallerCandidate] | None:
    """Extract `archive_path` and rank installer candidates inside it.

    Returns `(temp_dir, extract_root, top_candidate)` on success - the
    caller owns `temp_dir` and must clean it up once done with it; the
    installer to run is `extract_root / top_candidate.path`. Returns None
    (with the temp dir already cleaned up) if extraction failed or nothing
    installer-like was found inside.
    """
    temp_dir = tempfile.TemporaryDirectory(prefix="romm-install-extract-")
    extract_root = Path(temp_dir.name)

    if not extract_archive_tree(archive_path, extract_root):
        log.error(f"Failed to extract archive contents from {archive_path}")
        temp_dir.cleanup()
        return None

    candidates = detect_installer_candidates(_list_files_flat(extract_root))
    if not candidates:
        log.error(f"No installer found inside extracted archive {archive_path}")
        temp_dir.cleanup()
        return None

    return temp_dir, extract_root, candidates[0]
