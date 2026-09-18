"""Orchestrates a single server-side install run inside an RQ job.

Flow: mark the session INSTALLING, spin up a virtual display + VNC bridge,
launch the installer under bubblewrap via Proton/Wine, and wait on it while
the user drives it themselves through the VNC session. Two background
threads run alongside it: one just keeps the current dialog focused so it
actually renders and receives their input (see ``_focus_maintenance_loop``),
the other periodically snapshots whatever the installer has written so far
into a best-effort live manifest clients can already start pulling finished
pieces from (see ``_live_manifest_loop``). Once the installer exits, hash
everything it produced into the real, fully-verified manifest (STREAMING),
and finalize as DONE. Any failure marks the session FAILED and always tears
the sandbox down.

This module runs in the RQ worker process, not the web process.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from config import (
    INSTALL_PROTON_CACHYOS_PATH,
    INSTALL_PROTON_PATH,
    INSTALL_SANDBOX_ENABLED,
    INSTALL_TIMEOUT,
)
from handler.database import db_install_session_handler, db_rom_handler
from handler.filesystem import fs_rom_handler
from handler.install.archive_prescan import extract_and_rescan, is_archive_candidate
from handler.install.manifest import (
    LiveManifestEntry,
    delete_live_manifest,
    hash_files,
    manifest_total_bytes,
    scan_live_manifest,
    write_live_manifest,
    write_manifest,
)
from handler.install.progress import ThrottledProgress
from handler.install.proton_builds import resolve_proton_path
from handler.install.sandbox import SandboxSpec, build_bwrap_command
from handler.install.vnc import VncSession, start_vnc_session
from handler.install.windows_output import (
    collect_windows_install_files,
    resolve_install_root,
    snapshot_windows_content_files,
)
from handler.redis_handler import install_queue
from logger.formatter import highlight as hl
from logger.logger import log
from models.install_session import InstallSessionState
from tasks.tasks import TaskType
from utils.install_cache import ensure_session_cache_dir

# noVNC static assets shipped in the sandbox image.
NOVNC_WEB_ROOT = "/usr/share/novnc"

# How often the focus-maintenance loop re-checks which window should have
# input focus - frequent enough that a newly appeared dialog (the user just
# clicked "Next") gets real focus, and therefore actually renders, within a
# fraction of a second of appearing.
FOCUS_MAINTENANCE_INTERVAL = 1.0

# How often the live-manifest loop rescans the installer's output - matches
# ThrottledProgress's cadence elsewhere in this module, frequent enough that
# a client sees a large file's sealed_bytes grow in near-real-time without
# rehashing so often it competes with the installer for disk I/O.
LIVE_MANIFEST_INTERVAL = 1.0


def enqueue_install(install_session_id: int) -> str:
    """Enqueue an install run on the dedicated install queue and return its job id."""
    job = install_queue.enqueue(
        run_install,
        install_session_id,
        job_timeout=INSTALL_TIMEOUT + 300,
        meta={"task_name": "Remote install", "task_type": TaskType.GENERIC},
    )
    return job.id


def _proton_prefix_dir(work_dir: Path) -> Path:
    """Per-session Proton/Wine prefix, isolated inside the session cache."""
    prefix = work_dir / "prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    return prefix


def _uses_wine(installer_abs_path: str) -> bool:
    """Native .sh/.run installers run directly; everything else goes through
    Proton/Wine."""
    return not installer_abs_path.lower().endswith((".sh", ".run"))


def _wine_or_proton(proton_build: str | None = None) -> str:
    """The Proton/Wine binary this install runs Windows installers under.

    ``proton_build`` is the session's chosen build id (see
    handler.install.proton_builds); an unset, unknown, or not-actually-
    installed id falls back to the server default (INSTALL_PROTON_PATH),
    same as if no choice had been made at all. The sandbox image bundles
    GE-Proton by default; INSTALL_PROTON_PATH can point at a different
    build, or be cleared to fall back to plain Wine."""
    return resolve_proton_path(proton_build) or INSTALL_PROTON_PATH or "wine"


def _is_proton(proton_or_wine: str) -> bool:
    """Distinguish Proton (its own `run <exe>` verb, STEAM_COMPAT_* env)
    from plain Wine (`wine <exe>` directly, WINEPREFIX) - Proton's own
    launcher script is always named exactly "proton"."""
    return Path(proton_or_wine).name == "proton"


def _build_inner_command(installer_abs_path: str, proton_or_wine: str) -> list[str]:
    """Command that Proton/Wine uses to run the installer.

    Proton's "run" and "waitforexitandrun" verbs both end up calling the
    exact same session.run(), but protonfixes.execute() - which applies
    Proton's own library of per-game/per-installer compatibility fixes -
    only actually does anything for "waitforexitandrun"; its own
    check_conditions() gates on that exact string, so "run" silently skips
    every fix (logged as "Skipping fix execution. We are probably running a
    unit test.", easy to miss). "waitforexitandrun" is also what real
    non-Steam launchers (umu-launcher, Faugus) use for exactly this reason.
    """
    if not _uses_wine(installer_abs_path):
        return ["/bin/sh", installer_abs_path]
    if _is_proton(proton_or_wine):
        return [proton_or_wine, "waitforexitandrun", installer_abs_path]
    return [proton_or_wine, installer_abs_path]


def _wine_drive_c_root(prefix_dir: Path, proton_or_wine: str) -> Path:
    """Where this run's "drive_c" actually ends up. Proton nests its real
    Wine prefix under a "pfx" subdirectory of STEAM_COMPAT_DATA_PATH; plain
    Wine uses WINEPREFIX (the session prefix dir) directly."""
    if _is_proton(proton_or_wine):
        return prefix_dir / "pfx"
    return prefix_dir


# A brand-new prefix's implicit bootstrap (registry hives, drive_c layout,
# fonts) on the very first "wine <installer>.exe" call is slow and, under
# this headless/sandboxed setup, occasionally races with launching the
# installer's own window in the same process and fails outright ("could not
# load kernel32.dll"). Booting the prefix as its own step first, before the
# installer or the focus-maintenance loop ever start, makes that failure
# attributable and avoids the race.
WINE_PREFIX_INIT_TIMEOUT = 60

# Even booted as its own step, this has been observed to fail intermittently
# ("could not load kernel32.dll") under CPU/IO contention on the host - a few
# retries clear it without a long fixed delay that would slow down the
# common, non-flaky case.
WINE_PREFIX_INIT_ATTEMPTS = 3
WINE_PREFIX_INIT_RETRY_DELAY = 3.0

# How much of a failed attempt's stderr to keep in the error message; wine's
# own diagnostic noise (ole/RPC warnings, winediag lines) is otherwise lost
# since the subprocess output is captured, not streamed to the job log.
_STDERR_TAIL_CHARS = 800


def _init_wine_prefix(
    proton_or_wine: str,
    *,
    installer_abs: str,
    work_dir: str,
    prefix_dir: str,
    display: str,
    extra_env: tuple[tuple[str, str], ...] = (),
) -> None:
    is_proton = _is_proton(proton_or_wine)
    label = "Proton" if is_proton else "Wine"
    wineboot = (
        [proton_or_wine, "run", "wineboot", "--init"]
        if is_proton
        else [proton_or_wine, "wineboot", "--init"]
    )
    argv = _wrap_for_sandbox(
        wineboot,
        installer_abs=installer_abs,
        work_dir=work_dir,
        proton_prefix=prefix_dir,
        display=display,
        extra_env=extra_env,
    )
    last_error: Exception | None = None
    for attempt in range(1, WINE_PREFIX_INIT_ATTEMPTS + 1):
        try:
            subprocess.run(
                argv,
                timeout=WINE_PREFIX_INIT_TIMEOUT,
                check=True,
                capture_output=True,
            )
            return
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[-_STDERR_TAIL_CHARS:]
            last_error = RuntimeError(
                f"Failed to initialize the {label} prefix (exit {e.returncode}): {stderr}"
            )
        except subprocess.TimeoutExpired:
            last_error = RuntimeError(f"Timed out initializing the {label} prefix")

        if attempt < WINE_PREFIX_INIT_ATTEMPTS:
            log.warning(
                f"Wine prefix init attempt {attempt} failed, retrying: {last_error}"
            )
            time.sleep(WINE_PREFIX_INIT_RETRY_DELAY)

    assert last_error is not None
    raise last_error


def run_install(install_session_id: int) -> None:
    """Entry point enqueued on the RQ worker for one install session."""
    session = db_install_session_handler.get_session(install_session_id)
    if session is None:
        log.error(f"Install session {install_session_id} not found; aborting")
        return

    rom = db_rom_handler.get_rom(session.rom_id)
    if rom is None:
        # "Game", not "ROM": this message is user-facing (session.error), and
        # this path only ever runs for the Windows install flow.
        _fail(install_session_id, "Game no longer exists")
        return

    if not session.installer_path:
        _fail(install_session_id, "No installer selected for this session")
        return

    try:
        installer_abs = fs_rom_handler.resolve_installer_abs_path(
            rom, session.installer_path
        )
    except (ValueError, FileNotFoundError) as e:
        _fail(install_session_id, str(e))
        return

    # The picked "installer" is itself an archive/disc image (a
    # distributor .zip, a game ISO, ...) rather than something directly
    # runnable - extract it into a scratch dir (never the install cache -
    # this holds a copy of the ROM's own archive contents, not the
    # install's own output) and search inside it exactly like the ROM's
    # own files would be searched, "come di consueto".
    extract_temp_dir: TemporaryDirectory[str] | None = None
    if is_archive_candidate(Path(installer_abs)):
        result = extract_and_rescan(Path(installer_abs))
        if result is None:
            _fail(
                install_session_id,
                "Installer archive didn't contain anything recognizable as "
                "an installer",
            )
            return
        extract_temp_dir, extract_root, top_candidate = result
        installer_abs = str(extract_root / top_candidate.path)

    work_dir = ensure_session_cache_dir(install_session_id)
    prefix_dir = _proton_prefix_dir(work_dir)
    proton_or_wine = _wine_or_proton(session.proton_build)
    is_proton = _is_proton(proton_or_wine)

    extra_env: tuple[tuple[str, str], ...] = ()
    if is_proton:
        # Proton computes its own real Wine prefix from
        # STEAM_COMPAT_DATA_PATH (as "<that>/pfx", not WINEPREFIX directly)
        # and needs STEAM_COMPAT_CLIENT_INSTALL_PATH to exist, even though
        # nothing here is a real Steam install - a session-local, already-
        # bound-in directory satisfies it.
        steam_client_dir = work_dir / "steam-client"
        steam_client_dir.mkdir(parents=True, exist_ok=True)
        extra_env = (
            ("STEAM_COMPAT_DATA_PATH", str(prefix_dir)),
            ("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(steam_client_dir)),
        )

    vnc: VncSession | None = None
    try:
        vnc = start_vnc_session(install_session_id, NOVNC_WEB_ROOT)
        db_install_session_handler.update_session(
            install_session_id,
            {
                "state": InstallSessionState.INSTALLING,
                "cache_path": str(work_dir),
                "vnc_url": vnc.public_url,
                "vnc_web_port": vnc.web_port,
            },
        )

        windows_baseline: frozenset[Path] = frozenset()
        if _uses_wine(installer_abs):
            _init_wine_prefix(
                proton_or_wine,
                installer_abs=installer_abs,
                work_dir=str(work_dir),
                prefix_dir=str(prefix_dir),
                display=vnc.display,
                extra_env=extra_env,
            )
            # Wine's own bootstrap seeds Program Files with real stock stub
            # apps (wmplayer.exe, iexplore.exe, Common Files DLLs, ...) -
            # snapshot them now, before the installer ever runs, so they
            # don't get mistaken for what it wrote (see windows_output.py).
            windows_baseline = snapshot_windows_content_files(
                _wine_drive_c_root(prefix_dir, proton_or_wine)
            )

        inner = _build_inner_command(installer_abs, proton_or_wine)
        argv = _wrap_for_sandbox(
            inner,
            installer_abs=installer_abs,
            work_dir=str(work_dir),
            proton_prefix=str(prefix_dir),
            display=vnc.display,
            extra_env=extra_env,
        )

        log.info(
            f"Launching installer for session {hl(str(install_session_id))} "
            f"via {hl('Proton' if is_proton else 'Wine')}"
        )

        # Only meaningful for the Windows/Wine path - collect_windows_install_files
        # is the same "Program Files" discovery already used at finalize time,
        # just run repeatedly instead of once after the fact. Native .sh/.run
        # installers have no such discovery target either way.
        stop_live_manifest = threading.Event()
        live_manifest_thread: threading.Thread | None = None
        if _uses_wine(installer_abs):
            live_manifest_thread = threading.Thread(
                target=_live_manifest_loop,
                args=(
                    work_dir,
                    _wine_drive_c_root(prefix_dir, proton_or_wine),
                    windows_baseline,
                    stop_live_manifest,
                ),
                daemon=True,
            )
            live_manifest_thread.start()
        try:
            _run_installer(argv, vnc.display)
        finally:
            stop_live_manifest.set()
            if live_manifest_thread is not None:
                live_manifest_thread.join(timeout=2)

        # Installer exited: it's done writing, VNC is no longer needed. Move
        # to STREAMING while we hash the output so clients see it as "still
        # working" rather than DONE early.
        db_install_session_handler.update_session(
            install_session_id,
            {
                "state": InstallSessionState.STREAMING,
                "vnc_url": None,
                "vnc_web_port": None,
            },
        )
        _finalize_install(
            install_session_id,
            _wine_drive_c_root(prefix_dir, proton_or_wine),
            work_dir,
            windows_baseline,
        )
    except subprocess.TimeoutExpired:
        _fail(install_session_id, "Installer timed out")
    except Exception as e:  # noqa: BLE001 - surface any runner failure to the UI
        log.error(f"Install session {install_session_id} failed: {e}")
        _fail(install_session_id, str(e))
    finally:
        if vnc is not None:
            vnc.stop()
        # Covers every exit path (done, failed, timed out) uniformly - a
        # successful run's own _finalize_install has already removed it by
        # the time we get here, so this is just a no-op then.
        delete_live_manifest(work_dir)
        if extract_temp_dir is not None:
            extract_temp_dir.cleanup()


def _files_under(files: list[Path], root: Path) -> list[Path]:
    """Filter to files actually under `root` - `Path.relative_to` raises for
    anything else, so a straggler discovered after `root` has been frozen
    (see resolve_install_root) is safer to just omit than to crash on."""
    kept = []
    for f in files:
        try:
            f.relative_to(root)
        except ValueError:
            continue
        kept.append(f)
    return kept


def _live_manifest_loop(
    work_dir: Path,
    prefix_dir: Path,
    windows_baseline: frozenset[Path],
    stop: threading.Event,
) -> None:
    """Periodically snapshot the installer's in-progress output so a client
    can start pulling finished pieces of a file before the whole install (and
    its real, fully-verified manifest) is done - see
    handler.install.manifest.scan_live_manifest for the sealing rule this
    relies on to never hand out a chunk that might still change."""
    drive_c = prefix_dir / "drive_c"
    state: dict[str, LiveManifestEntry] = {}
    # Resolved once, the first scan that finds anything, then held fixed for
    # the rest of the run - recomputing it every scan risks it moving
    # mid-session and silently resetting every already-sealed file's
    # continuity key (see resolve_install_root's own docstring).
    root: Path | None = None
    while True:
        try:
            candidates = collect_windows_install_files(prefix_dir, windows_baseline)
            if root is None and candidates:
                root = resolve_install_root(drive_c, candidates)
            if root is not None:
                state = scan_live_manifest(root, _files_under(candidates, root), state)
                write_live_manifest(work_dir, state)
        except OSError as e:
            log.warning(f"Live manifest scan failed, will retry: {e}")
        if stop.wait(LIVE_MANIFEST_INTERVAL):
            return


def _finalize_install(
    install_session_id: int,
    prefix_dir: Path,
    work_dir: Path,
    windows_baseline: frozenset[Path] = frozenset(),
) -> None:
    """Hash whatever the installer produced and mark the session DONE.

    Raises if nothing was found, so the caller's except-block routes it to
    FAILED instead of finishing "successfully" with an empty manifest.
    """
    files = collect_windows_install_files(prefix_dir, windows_baseline)
    if not files:
        raise RuntimeError(
            "Installer finished but no files were found under drive_c "
            "(it may have installed to a blacklisted or unrecognized path)"
        )

    # One-time computation over the complete final file set - no flip-flop
    # risk the way the live loop's per-scan version would have, since this
    # only ever runs once (see resolve_install_root).
    root = resolve_install_root(prefix_dir / "drive_c", files)
    files = _files_under(files, root)

    report = ThrottledProgress(
        lambda hashed: db_install_session_handler.update_session(
            install_session_id, {"bytes_written": hashed}
        )
    )
    entries = hash_files(files, root=root, on_progress=report)
    report.finish(manifest_total_bytes(entries))
    write_manifest(work_dir, entries)

    db_install_session_handler.update_session(
        install_session_id,
        {
            "state": InstallSessionState.DONE,
            "bytes_written": manifest_total_bytes(entries),
            "bytes_total": manifest_total_bytes(entries),
        },
    )


def _wrap_for_sandbox(
    inner: list[str],
    *,
    installer_abs: str,
    work_dir: str,
    proton_prefix: str,
    display: str,
    extra_env: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Wrap the inner command in bubblewrap unless the sandbox is disabled."""
    if not INSTALL_SANDBOX_ENABLED:
        log.warning("Install sandbox is DISABLED; running installer unconfined")
        return inner
    # Bind in every installed Proton/Wine build's own directory, not just the
    # server default - a session can pick either one (see proton_builds.py),
    # and bwrap only exposes what's explicitly listed here.
    ro_binds = tuple(
        {
            str(Path(path).resolve().parent)
            for path in (INSTALL_PROTON_PATH, INSTALL_PROTON_CACHYOS_PATH)
            if path
        }
    )
    spec = SandboxSpec(
        installer_path=installer_abs,
        work_dir=work_dir,
        proton_prefix=proton_prefix,
        display=display,
        ro_binds=ro_binds,
        extra_env=extra_env,
    )
    return build_bwrap_command(spec, inner)


def _run_installer(argv: list[str], display: str) -> None:
    """Run the installer argv with a hard timeout, keeping it usable meanwhile.

    The user drives the installer themselves through the VNC session - this
    just waits on the process, while a background thread keeps whatever
    dialog is currently showing actually focused (see
    ``_focus_maintenance_loop`` for why that's needed at all).
    """
    proc = subprocess.Popen(argv)
    stop_focus_loop = threading.Event()
    focus_thread = threading.Thread(
        target=_focus_maintenance_loop, args=(display, stop_focus_loop), daemon=True
    )
    focus_thread.start()
    try:
        proc.wait(timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        stop_focus_loop.set()
        focus_thread.join(timeout=2)


def _focus_maintenance_loop(display: str, stop: threading.Event) -> None:
    """Keep whatever installer dialog is currently on screen actually usable.

    This never clicks or types anything on the user's behalf - the user
    drives the installer themselves through the VNC session (see
    InstallVncOverlay), the same way they'd click through any other
    installer. What it does do: without real X11 input focus, a freshly
    mapped Wine/Proton window has been observed not just to ignore
    keystrokes, but to never paint its own content at all (an empty/black
    client area indistinguishable from "not rendered"). IceWM's own
    focus-on-map policy doesn't reliably cover a session's very first
    window (see docker/icewm-preferences), and every later dialog in the
    same wizard needs the exact same treatment as it replaces the last one.
    Runs unsandboxed (it never touches the installer's files, only the
    shared X display) since xdotool talks to the X server directly
    regardless of which mount/pid namespace the installer's client process
    is in.
    """
    env = {**os.environ, "DISPLAY": display}
    _focus_installer_window(env)
    while not stop.wait(FOCUS_MAINTENANCE_INTERVAL):
        _focus_installer_window(env)


# IceWM's own window furniture on a session used for nothing but a single
# installer - excluded so _focus_installer_window never grabs focus onto the
# WM itself (or one of its per-window decoration wrappers) instead of the
# actual dialog. "Frame"/"Container"/"TitleBar"/"SysMenu"/"Lower"/"Close" are
# IceWM's own reparenting sub-windows, generic to *every* managed window
# (including the real one) - without excluding them too, their geometry can
# tie or beat the actual client's and win the largest-area pick instead.
# Exact names observed on IceWM 3.3.1's default theme; harmless if a future
# theme adds more (worst case, that extra window becomes a focus candidate).
_ICEWM_CHROME_NAMES = frozenset(
    {
        "IceTopWin",
        "IceBottom",
        "IceEdge",
        "IceRootProxy",
        "YXTrayProxy",
        "Default IME",
        "Input",
        "Frame",
        "Container",
        "TitleBar",
        "SysMenu",
        "Lower",
        "Close",
    }
)


def _focus_installer_window(env: dict) -> None:
    """Force real X11 input focus onto the installer's own window.

    IceWM's automatic focus-on-map policy (FocusOnMapTransient, see
    docker/icewm-preferences) is meant to cover this, but has been observed
    not to reliably transfer focus to a fresh session's very first dialog -
    unfocused, a Wine/Proton window has been observed not just to ignore the
    real user's keystrokes but to never paint its own content at all. Belt
    and suspenders: explicitly (re-)grab focus on whatever looks like the
    actual app window on every check, rather than trusting the WM got it
    right once at map time.
    """
    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "."],
            env=env,
            check=False,
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return

    best_id: str | None = None
    best_area = 0
    for window_id in result.stdout.split():
        try:
            name = subprocess.run(
                ["xdotool", "getwindowname", window_id],
                env=env,
                check=False,
                capture_output=True,
                timeout=2,
                text=True,
            ).stdout.strip()
            if not name or name in _ICEWM_CHROME_NAMES or name.startswith("IceWM "):
                continue
            geometry = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", window_id],
                env=env,
                check=False,
                capture_output=True,
                timeout=2,
                text=True,
            ).stdout
            dims = dict(
                line.split("=", 1) for line in geometry.splitlines() if "=" in line
            )
            area = int(dims.get("WIDTH", 0)) * int(dims.get("HEIGHT", 0))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        if area > best_area:
            best_area = area
            best_id = window_id

    if best_id is not None:
        subprocess.run(
            ["xdotool", "windowfocus", "--sync", best_id],
            env=env,
            check=False,
            capture_output=True,
            timeout=2,
        )


def _fail(install_session_id: int, error: str) -> None:
    db_install_session_handler.update_session(
        install_session_id,
        {
            "state": InstallSessionState.FAILED,
            "error": error,
            "vnc_url": None,
            "vnc_web_port": None,
        },
    )
