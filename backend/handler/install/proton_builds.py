"""Registry and manager of Proton/Wine builds for the install sandbox.

The install-sandbox Docker image extracts each Proton build into its own
subdirectory under PROTON_INSTALL_ROOT (default /opt/proton). The manager
discovers them at runtime by scanning for an executable ``proton`` (or
``bin/wine``) binary in each subdirectory, so build identity is always tied to
what actually exists on disk — never to a stale env var, which was the root
cause of the ``bwrap: Can't find source path /opt/proton`` crash.

Builds not yet downloaded are still listed (sourced from GitHub release APIs)
so the frontend can offer a "download" affordance, mirroring protonup-qt.
Downloaded builds are extracted into the same PROTON_INSTALL_ROOT tree and
picked up on the next scan.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from config import INSTALL_DEFAULT_PROTON_BUILD, PROTON_INSTALL_ROOT
from handler.redis_handler import install_queue, redis_client
from logger.formatter import highlight as hl
from logger.logger import log

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProtonBuild:
    """One Proton build the server knows about."""

    id: str
    label: str
    installed: bool
    # Human-readable version string from the upstream release (e.g. "10-34").
    version: str | None = None
    # Where this build lives on disk right now, if installed. None when
    # installed=False.
    path: str | None = None
    # "runtime" = discovered on disk, "upstream" = only downloadable.
    source: str = "runtime"
    # Download URL for the release tarball, only set for upstream builds.
    download_url: str | None = None
    # Approximate tarball size in bytes, only set for upstream builds.
    size_bytes: int | None = None


# --------------------------------------------------------------------------- #
# Build discovery (runtime)
# --------------------------------------------------------------------------- #


def _scan_proton_root(root: str) -> list[ProtonBuild]:
    """Discover installed builds by scanning ``root`` one level deep.

    Each subdirectory that contains an executable ``proton`` launcher (Proton)
    or ``bin/wine`` (plain Wine) is treated as one build. The directory name
    is the build id, preserving backward-compatible ids like
    ``GE-Proton10-34`` and ``cachyos-latest``.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    builds: list[ProtonBuild] = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        proton_bin = entry / "proton"
        wine_bin = entry / "bin" / "wine"
        if proton_bin.exists() and os.access(proton_bin, os.X_OK):
            # Only surface builds whose full file tree is present — a partial
            # extraction (e.g. from an interrupted download) passes the
            # proton-binary check but is unusable at runtime.
            if not _is_proton_build_complete(entry):
                log.debug(
                    f"Skipping incomplete Proton build at {entry}"
                )
                continue
            builds.append(
                ProtonBuild(
                    id=entry.name,
                    label=entry.name,
                    installed=True,
                    path=str(proton_bin),
                    source="runtime",
                )
            )
        elif wine_bin.exists() and os.access(wine_bin, os.X_OK):
            builds.append(
                ProtonBuild(
                    id=entry.name,
                    label=entry.name,
                    installed=True,
                    path=str(wine_bin),
                    source="runtime",
                )
            )
    return builds


def _scan_legacy_envs() -> list[ProtonBuild]:
    """No-op kept for backward-compatible test mocking; no legacy env vars
    are consulted anymore — PROTON_INSTALL_ROOT is the sole source of truth."""
    return []


_REDIS_INSTALLED_KEY = "proton:worker:installed"


def _discover_installed() -> list[ProtonBuild]:
    """All builds the manager can currently see on disk or registered by workers.

    In the normal dev setup the API runs in the romm-dev container while Proton
    builds live in the install-sandbox container (and possibly multiple workers).
    Workers register their installed builds in a Redis set on startup and after
    each download, so the API can surface them without a shared filesystem
    volume. Builds on the local disk (e.g. a host-mounted PROTON_INSTALL_ROOT)
    are also discovered directly.
    """
    builds: list[ProtonBuild] = []

    # Local disk scan (covers host-mounted volumes and same-container setups).
    builds.extend(_scan_proton_root(PROTON_INSTALL_ROOT))

    # Worker-registered builds from Redis (covers multi-container dev setups
    # where the API and worker don't share a filesystem for /opt/proton/).
    try:
        worker_builds = redis_client.smembers(_REDIS_INSTALLED_KEY) or set()
    except Exception as e:
        log.debug(f"Redis smembers for proton builds failed: {e}")
        worker_builds = set()

    # Deduplicate: if a build was both found on local disk AND registered in
    # Redis, don't include it twice. Local disk results take precedence
    # (they have the actual binary path). Decode bytes from Redis (the client
    # may not have decode_responses=True).
    local_ids = {b.id for b in builds}
    for build_id in worker_builds:
        build_id_str = build_id.decode() if isinstance(build_id, bytes) else build_id
        if build_id_str in local_ids:
            continue
        builds.append(
            ProtonBuild(
                id=build_id_str,
                label=build_id_str,
                installed=True,
                path=None,  # resolved by the worker at runtime
                source="runtime",
            )
        )
    return builds


# --------------------------------------------------------------------------- #
# Remote source discovery (downloadable builds)
# --------------------------------------------------------------------------- #

# (source_name, github_repo, asset_suffix, build_id)
# build_id is the canonical ID the rest of the system uses. For GE-Proton we
# use the tag name (e.g. "GE-Proton11-7") so users can pin a specific version;
# for CachyOS we use "cachyos-latest" since we always fetch the latest release
# and the ID stays stable as new releases appear.
_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("GE-Proton", "GloriousEggroll/proton-ge-custom", "tar.gz", ""),
    ("Proton-CachyOS", "CachyOS/proton-cachyos", "tar.xz", "cachyos-latest"),
)


def _fetch_github_releases(_source_name: str, repo: str, suffix: str, build_id: str = "") -> list[ProtonBuild]:
    """Query the GitHub releases API for a Proton fork and return builds
    that are downloadable but not yet installed.

    Runs synchronously — called from the API endpoint (fast, cached-ish) and
    from the download task itself (which needs the exact asset URL).
    """
    import httpx

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        resp = httpx.get(url, timeout=30, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
    except (httpx.HTTPError, ValueError) as e:
        log.debug(f"GitHub releases fetch failed for {repo}: {e}")
        return []

    data = resp.json()
    tag = data.get("tag_name", "")
    if not tag:
        return []

    # For sources with a stable build_id (e.g. "cachyos-latest"), use that.
    # Otherwise (GE-Proton), use the tag name so users can pin versions.
    resolved_build_id = build_id or tag

    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(suffix) and "x86_64" in name:
            # Avoid listing builds we already have on disk.
            if _build_dir_exists(resolved_build_id):
                continue
            return [
                ProtonBuild(
                    id=resolved_build_id,
                    label=tag,
                    installed=False,
                    version=tag,
                    source="upstream",
                    download_url=asset.get("browser_download_url"),
                    size_bytes=asset.get("size"),
                )
            ]
    return []


def _build_dir_exists(build_id: str) -> bool:
    """Whether a build with this id is already extracted under PROTON_INSTALL_ROOT."""
    return (Path(PROTON_INSTALL_ROOT) / build_id).is_dir()


def _is_proton_build_complete(build_dir: Path) -> bool:
    """Whether a Proton build directory has been fully extracted.

    Checks for the key files that Proton's launcher needs at startup: the
    ``proton`` binary, ``files/bin/wine``, and ``files/share/default_pfx/``.
    A partial extraction (e.g. from an interrupted download) will be missing
    at least one of these, so we treat the build as incomplete and re-download.
    """
    return (
        (build_dir / "proton").exists()
        and (build_dir / "files" / "bin" / "wine").exists()
        and (build_dir / "files" / "share" / "default_pfx").exists()
    )


def _fetch_downloadable() -> list[ProtonBuild]:
    """Query all upstream sources for builds not yet installed."""
    builds: list[ProtonBuild] = []
    for source_name, repo, suffix, build_id in _SOURCES:
        builds.extend(_fetch_github_releases(source_name, repo, suffix, build_id))
    return builds


# --------------------------------------------------------------------------- #
# Download task (runs as an RQ job on the install worker)
# --------------------------------------------------------------------------- #

# Redis key prefix for tracking per-build download progress.
_PROGRESS_KEY = "proton_download:progress:{build_id}"
_JOB_KEY = "proton_download:job:{build_id}"
# Redis key prefix for tracking per-build extraction progress (set to a
# non-zero fraction while the tarball is being unpacked, used by the
# frontend to show "Installing Proton…" instead of "Starting installer…").
_EXTRACT_KEY = "proton_download:extract:{build_id}"


def enqueue_proton_download(build_id: str) -> str:
    """Enqueue a Proton download on the install queue and return the RQ job id.

    Returns the existing job id if a download for this build is already in
    progress (idempotent — avoids duplicate concurrent downloads).
    """
    existing = redis_client.get(_JOB_KEY.format(build_id=build_id))
    if existing and redis_client.exists(_PROGRESS_KEY.format(build_id=build_id)):
        return existing.decode() if isinstance(existing, bytes) else existing

    job = install_queue.enqueue(
        _download_proton_build,
        build_id,
        job_timeout=600,  # 10 min: download + extract
        meta={"task_name": f"Download Proton {build_id}", "task_type": "proton_dl"},
    )
    redis_client.setex(_JOB_KEY.format(build_id=build_id), 3600, job.id)
    redis_client.setex(
        _PROGRESS_KEY.format(build_id=build_id), 3600, "0:0"
    )  # "downloaded:total"
    return job.id


def download_progress(build_id: str) -> float | None:
    """Download progress (0.0–1.0) for a build, or None if no download is
    in progress."""
    raw = redis_client.get(_PROGRESS_KEY.format(build_id=build_id))
    if not raw:
        return None
    decoded = raw.decode() if isinstance(raw, bytes) else raw
    try:
        downloaded, total = decoded.split(":")
        total_int = int(total)
        if total_int == 0:
            return None
        return min(1.0, int(downloaded) / total_int)
    except (ValueError, ZeroDivisionError):
        return None


def _download_proton_build(build_id: str) -> None:
    """RQ job body: download + extract one Proton build.

    Runs on the install-sandbox worker, which has network access (the bwrap
    sandbox itself is network-less). The download goes to a temp file, then
    is extracted into PROTON_INSTALL_ROOT/<build_id>/ — the manager's next scan
    will pick it up automatically.
    """
    import httpx

    # Find the download URL by re-querying upstream.
    all_downloadable = _fetch_downloadable()
    match = next((b for b in all_downloadable if b.id == build_id), None)
    if match is None or not match.download_url:
        log.error(f"Cannot download Proton build {build_id}: no matching release found")
        redis_client.delete(_JOB_KEY.format(build_id=build_id))
        redis_client.delete(_PROGRESS_KEY.format(build_id=build_id))
        return

    dest = Path(PROTON_INSTALL_ROOT) / build_id
    if dest.exists():
        # Check if the build is actually complete (not a partial extraction
        # from an interrupted download). A valid Proton build has at least
        # the `proton` binary and `files/share/default_pfx/`.
        if _is_proton_build_complete(dest):
            log.info(f"Proton build {build_id} already present at {dest}, skipping download")
            redis_client.delete(_JOB_KEY.format(build_id=build_id))
            redis_client.delete(_PROGRESS_KEY.format(build_id=build_id))
            return
        # Incomplete build from a previous interrupted download — clean up
        # and re-download from scratch.
        log.warning(f"Removing incomplete Proton build at {dest}")
        shutil.rmtree(dest, ignore_errors=True)

    # Extract into a temp dir first, then rename — this makes the download
    # atomic: if the job is killed mid-extraction, the temp dir is cleaned
    # up and the next run starts fresh instead of finding a half-extracted
    # build and skipping the extraction.
    tmp_extract_dir = dest.parent / f".extract-{build_id}-{os.getpid()}"
    tmp_extract_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = dest.parent / f".download-{build_id}.tmp"

    log.info(f"Downloading Proton {hl(build_id)} from {hl(match.download_url)}")
    total = match.size_bytes or 0
    downloaded = 0
    try:
        with httpx.stream("GET", str(match.download_url), timeout=600, follow_redirects=True) as resp:
            resp.raise_for_status()
            with tmp_file.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    redis_client.setex(
                        _PROGRESS_KEY.format(build_id=build_id),
                        3600,
                        f"{downloaded}:{total}",
                    )
    except Exception as e:
        log.error(f"Proton download {build_id} failed: {e}")
        tmp_file.unlink(missing_ok=True)
        tmp_extract_dir.rmdir()  # remove the empty temp dir we created
        redis_client.delete(_JOB_KEY.format(build_id=build_id))
        redis_client.delete(_PROGRESS_KEY.format(build_id=build_id))
        raise

    # Extract — Proton tarballs expand to a top-level dir like
    # "GE-Proton11-7-x86_64", but we need the binary at <dest>/proton,
    # so strip the first path component (equivalent to --strip-components=1
    # in the Dockerfile's `tar -xzf ... --strip-components=1`).
    log.info(f"Extracting Proton {hl(build_id)} into {hl(str(tmp_extract_dir))}")
    redis_client.setex(_EXTRACT_KEY.format(build_id=build_id), 3600, "1")
    try:
        with tarfile.open(tmp_file) as tar:
            members = []
            for m in tar.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) > 1:
                    m.name = parts[1]
                    members.append(m)
            tar.extractall(tmp_extract_dir, members=members, filter="data")
    except Exception:
        shutil.rmtree(tmp_extract_dir, ignore_errors=True)
        raise
    finally:
        tmp_file.unlink(missing_ok=True)

    # Atomically rename the complete extraction.
    tmp_extract_dir.rename(dest)

    redis_client.delete(_JOB_KEY.format(build_id=build_id))
    redis_client.delete(_PROGRESS_KEY.format(build_id=build_id))
    redis_client.delete(_EXTRACT_KEY.format(build_id=build_id))
    # Register the new build so the API container can surface it.
    redis_client.sadd(_REDIS_INSTALLED_KEY, build_id)
    log.info(f"Proton {hl(build_id)} installed successfully")


def remove_proton_build(build_id: str) -> None:
    """Remove a runtime-downloaded Proton build from disk.

    Only removes builds under PROTON_INSTALL_ROOT; never touches legacy
    env-var paths (those are in the Docker image layer and managed at build
    time).
    """
    dest = Path(PROTON_INSTALL_ROOT) / build_id
    if not dest.exists():
        return
    shutil.rmtree(dest)
    redis_client.srem(_REDIS_INSTALLED_KEY, build_id)
    log.info(f"Removed Proton build {hl(build_id)}")


# --------------------------------------------------------------------------- #
# Public API (backward-compatible signatures)
# --------------------------------------------------------------------------- #

# Module-level singleton — recomputed on each call so new downloads are visible
# without a process restart. The discovery work is cheap (a directory scan).
def _build_manager() -> "_ProtonBuildManager":
    return _ProtonBuildManager()


def list_proton_builds() -> tuple[ProtonBuild, ...]:
    """Every build the server knows about: installed (discovered on disk)
    plus downloadable (from upstream release APIs)."""
    return _build_manager().list_builds()


def resolve_proton_path(build_id: str | None) -> str | None:
    """The binary to run for a chosen build id.

    Returns None when the id is unset, unknown, or not installed — callers
    fall back to the server default (Wine) rather than failing the install.
    """
    return _build_manager().resolve_path(build_id)


def resolve_effective_build(explicit_id: str | None) -> str | None:
    """The Proton build id a session will actually run under, decided as early
    as session creation rather than left implicit inside the worker.

    Mirrors handler.install.runner's own fallback chain (explicit choice, then
    the configured default, then whatever's already installed) but returns the
    id even when it isn't downloaded yet - the worker auto-downloads on first
    use either way, and persisting the id here (instead of leaving the
    session's proton_build NULL) is what lets the client poll
    /install/proton/{id}/progress and show "Downloading Proton X…" instead of
    an unexplained stall for however long the download+extract takes. None
    only when nothing at all is known (no explicit choice, no default
    configured, nothing installed) - the runner then falls back to plain Wine.
    """
    if explicit_id:
        return explicit_id
    if INSTALL_DEFAULT_PROTON_BUILD:
        return INSTALL_DEFAULT_PROTON_BUILD
    for build in _discover_installed():
        if build.installed:
            return build.id
    return None


def enqueue_download(build_id: str) -> str:
    """Enqueue a Proton build download. See :func:`enqueue_proton_download`."""
    return enqueue_proton_download(build_id)


def get_download_progress(build_id: str) -> float | None:
    """Download progress (0.0–1.0) or None. See :func:`download_progress`."""
    return download_progress(build_id)


def is_extracting(build_id: str) -> bool:
    """Whether a Proton build is currently in the extraction phase (tarball
    already downloaded, files being unpacked). The progress API returns
    ``extracting: true`` with ``progress: 0.0`` during this phase so the
    frontend can show "Installing Proton…" instead of a stale 100%."""
    return bool(redis_client.exists(_EXTRACT_KEY.format(build_id=build_id)))


def remove_build(build_id: str) -> None:
    """Remove a runtime-downloaded Proton build. See :func:`remove_proton_build`."""
    remove_proton_build(build_id)


# --------------------------------------------------------------------------- #
# Manager class
# --------------------------------------------------------------------------- #

# Cache downloads list for this many seconds to avoid hammering the GitHub API
# on every /proton-builds request.
_DOWNLOADABLE_CACHE_TTL = 300
_DOWNLOADABLE_CACHE: tuple[float, list[ProtonBuild]] | None = None


class _ProtonBuildManager:
    """Discovers installed Proton builds at runtime and lists downloadable ones.

    Module-level functions delegate to a fresh instance on each call (the
    discovery is cheap, and avoids stale state after a download completes).
    """

    def list_builds(self) -> tuple[ProtonBuild, ...]:
        installed = _discover_installed()
        installed_ids = {b.id for b in installed}
        downloadable = _cached_downloadable()
        # For builds that are both installed AND available upstream (same id),
        # merge the upstream version/label into the installed entry so the
        # frontend can show e.g. "cachyos-11.0-20260703-slr (installed)" rather
        # than just "cachyos-latest". Downloadable-only builds are appended as-is.
        downloadable_by_id = {b.id: b for b in downloadable}
        merged: list[ProtonBuild] = []
        for build in installed:
            upstream = downloadable_by_id.get(build.id)
            if upstream and not build.version:
                merged.append(
                    ProtonBuild(
                        id=build.id,
                        label=build.label,
                        installed=True,
                        version=upstream.version,
                        path=build.path,
                        source="runtime",
                        download_url=upstream.download_url,
                        size_bytes=upstream.size_bytes,
                    )
                )
            else:
                merged.append(build)
        # Append downloadable-only builds (id not in installed set).
        merged.extend(b for b in downloadable if b.id not in installed_ids)
        return tuple(merged)

    def resolve_path(self, build_id: str | None) -> str | None:
        if build_id is None:
            return None
        for build in _discover_installed():
            if build.id == build_id and build.installed:
                return build.path
        return None


def _cached_downloadable() -> list[ProtonBuild]:
    """Fetch downloadable builds, cached for _DOWNLOADABLE_CACHE_TTL seconds."""
    import time

    global _DOWNLOADABLE_CACHE
    now = time.monotonic()
    if _DOWNLOADABLE_CACHE is not None:
        ts, cached = _DOWNLOADABLE_CACHE
        if now - ts < _DOWNLOADABLE_CACHE_TTL:
            return cached
    cached = _fetch_downloadable()
    _DOWNLOADABLE_CACHE = (now, cached)
    return cached
