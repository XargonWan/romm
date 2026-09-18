"""Pure installer-candidate detection for remote installs.

Given a flat list of files found under a ROM's directory (relative paths + sizes),
rank them so the client/UI can pick the most likely installer. Kept free of any
filesystem or DB access so it is trivially unit-testable.

Detection order (lower rank = higher priority), per product requirements:
  0. Known GOG/setup installer names: gog-*.exe, setup.exe, install.exe, setup*.exe
     (also .msi variants).
  1. Any executable installer in the top-level game folder (.exe/.msi/.bat).
  2. Any executable installer anywhere (recursive) in the game folder.
  3. Disc images (.iso/.cue/.chd/.ccd/.bin/.img/.mds/.mdf/.nrg).
  4. Archives (.zip/.7z/.rar/.tar/.gz/.tgz/.tbz2/.txz/.bz2/.xz).
  5. Linux installers (.sh/.run/.appimage).
When nothing matches, the caller falls back to a manual file picker.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import PurePosixPath

# Platform slugs (UniversalPlatformSlug values) considered installable Windows
# targets for the remote-install flow.
WINDOWS_INSTALLABLE_SLUGS: frozenset[str] = frozenset(
    ("win", "win3x", "win9x", "windows-apps")
)

# Rank buckets.
RANK_KNOWN_INSTALLER = 0
RANK_TOP_LEVEL_EXECUTABLE = 1
RANK_NESTED_EXECUTABLE = 2
RANK_DISC_IMAGE = 3
RANK_ARCHIVE = 4
RANK_LINUX_INSTALLER = 5

# Case-insensitive glob patterns for well-known Windows installer entry points.
KNOWN_INSTALLER_PATTERNS: tuple[str, ...] = (
    "gog-*.exe",
    "setup.exe",
    "install.exe",
    "setup*.exe",
    "gog-*.msi",
    "setup.msi",
    "install.msi",
    "setup*.msi",
)

EXECUTABLE_EXTENSIONS: frozenset[str] = frozenset((".exe", ".msi", ".bat"))
DISC_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    (".iso", ".cue", ".chd", ".ccd", ".bin", ".img", ".mds", ".mdf", ".nrg")
)
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".tbz2", ".txz", ".bz2", ".xz")
)
LINUX_INSTALLER_EXTENSIONS: frozenset[str] = frozenset((".sh", ".run", ".appimage"))


@dataclass(frozen=True, slots=True)
class DetectedFile:
    """A file discovered under a ROM directory, with a POSIX relative path."""

    path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class InstallerCandidate:
    path: str
    file_name: str
    file_size_bytes: int
    rank: int
    kind: str


def _matches_known_installer(name_lower: str) -> bool:
    return any(fnmatch.fnmatch(name_lower, pat) for pat in KNOWN_INSTALLER_PATTERNS)


def _classify(file: DetectedFile) -> InstallerCandidate | None:
    posix = PurePosixPath(file.path)
    name = posix.name
    name_lower = name.lower()
    ext = posix.suffix.lower()
    is_top_level = len(posix.parts) == 1

    if ext in EXECUTABLE_EXTENSIONS and _matches_known_installer(name_lower):
        return _make(file, RANK_KNOWN_INSTALLER, "known installer")

    if ext in EXECUTABLE_EXTENSIONS:
        if is_top_level:
            return _make(file, RANK_TOP_LEVEL_EXECUTABLE, "executable (top level)")
        return _make(file, RANK_NESTED_EXECUTABLE, "executable (nested)")

    if ext in DISC_IMAGE_EXTENSIONS:
        return _make(file, RANK_DISC_IMAGE, "disc image")

    if ext in ARCHIVE_EXTENSIONS:
        return _make(file, RANK_ARCHIVE, "archive")

    if ext in LINUX_INSTALLER_EXTENSIONS:
        return _make(file, RANK_LINUX_INSTALLER, "linux installer")

    return None


def _make(file: DetectedFile, rank: int, kind: str) -> InstallerCandidate:
    return InstallerCandidate(
        path=file.path,
        file_name=PurePosixPath(file.path).name,
        file_size_bytes=file.size_bytes,
        rank=rank,
        kind=kind,
    )


def detect_installer_candidates(files: list[DetectedFile]) -> list[InstallerCandidate]:
    """Rank installer candidates from a flat file listing.

    Results are sorted by rank (priority), then by descending size (bigger installers
    first within a bucket), then by path for stable ordering.
    """
    candidates = [c for c in (_classify(f) for f in files) if c is not None]
    candidates.sort(key=lambda c: (c.rank, -c.file_size_bytes, c.path))
    return candidates
