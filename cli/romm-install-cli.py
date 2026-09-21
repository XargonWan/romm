#!/usr/bin/env python3
"""romm-install-cli: minimal CLI client to drive RomM's stream-install feature.

Pure stdlib (urllib, json, argparse, sys). No third-party deps.

Usage:
python cli/romm-install-cli.py --base http://localhost:5100 \\
      --user admin --pass admin --rom-id 123 --out /tmp/installed

Just run it - the server does the rest, the same way it would for the web
UI's own "Install" button (single source of truth: POST /{romId}/install
resolves everything server-side, no client has to replicate the logic):
  - Already installed (a cache from a prior run is still on disk)? Streamed
    immediately, nothing is (re)installed.
  - Not installed, and the server can confidently auto-pick an installer
    (a well-known name like setup.exe, or there's only one candidate)? It
    starts running right away, no interaction needed.
  - Otherwise - "manual mode": nobody (human or, someday, an OCR-driven
    "auto mode") has confirmed which file to run, or the installer itself
    needs someone to click through its own dialogs. The session sits in
    AWAITING_INSTALLER and the response carries `manual_install_url` - this
    CLI prints it and stops; open it in a browser, finish the install there
    (the VNC session), then just re-run this same command to stream the
    result.

Flow:
  1. login (HTTP Basic) -> session cookie
  2. GET /api/roms/{romId}/install - already DONE? skip straight to step 6,
     no worker needed at all
  3. GET /api/install/worker-status
  4. GET /api/roms/{romId}/install/candidates (informational only - the
     server does its own auto-pick, this is just to print the options)
  5. POST /api/roms/{romId}/install {installer_path, proton_build, ttl_seconds}
     - installer_path is optional; omit it and let the server decide.
     AWAITING_INSTALLER in the response means manual mode - stop and print
     `manual_install_url`
  6. poll GET /api/roms/{romId}/install/stream/manifest every 3s, started in
     a background thread concurrently with step 7's own polling - designed
     to work *while* an install is still running too (that's the whole
     "stream install" point), not just once it's DONE, though only the
     finished-install path has actually been verified end-to-end so far
  7. poll GET /api/roms/{romId}/install every 3s until state != active
  8. stream each file via Range GET /api/roms/{romId}/install/stream/{path}
  9. cancel: POST /api/roms/{romId}/install/cancel
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

POLL_INTERVAL = 3
MANIFEST_INTERVAL = 3
PROTON_POLL_INTERVAL = 2

ACTIVE_STATES = {"detecting", "awaiting_installer", "installing", "streaming"}


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr, flush=True)


def basic_header(user: str, pw: str) -> str:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return f"Basic {token}"


class Client:
    def __init__(self, base: str, user: str, pw: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.user = user
        self.pw = pw
        self.timeout = timeout
        self.cookie: str | None = None

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json", "User-Agent": "romm-install-cli/1.0"}
        if self.user and self.pw:
            h["Authorization"] = basic_header(self.user, self.pw)
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    def _store_cookie(self, headers: dict) -> None:
        # urllib stores cookies via http.cookiejar; we manually capture Set-Cookie.
        set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        if not set_cookie:
            return
        # Keep only the session cookie name=value up to the first ';'.
        pair = set_cookie.split(";", 1)[0].strip()
        if "=" in pair:
            name, _, value = pair.partition("=")
            if name.strip() == "romm_session":
                self.cookie = f"{name.strip()}={value.strip()}"

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None, raw: bool = False,
                timeout: float | None = None) -> tuple[int, bytes, dict]:
        url = self.base + path
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers = headers or {}
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                content = resp.read()
                self._store_cookie(resp.headers)
                return resp.status, content, dict(resp.headers)
        except urllib.error.HTTPError as e:
            content = e.read()
            self._store_cookie(e.headers)
            return e.code, content, dict(e.headers)
        except urllib.error.URLError as e:
            raise RuntimeError(f"connection error: {e}") from e

    def get_json(self, path: str, **kw) -> tuple[int, dict]:
        status, content, _ = self.request("GET", path, **kw)
        try:
            return status, json.loads(content.decode() or "null")
        except json.JSONDecodeError:
            return status, {"_raw": content.decode(errors="replace")}

    def post_json(self, path: str, body: dict, **kw) -> tuple[int, dict]:
        status, content, _ = self.request("POST", path, body=body, **kw)
        try:
            return status, json.loads(content.decode() or "null")
        except json.JSONDecodeError:
            return status, {"_raw": content.decode(errors="replace")}

    def delete_json(self, path: str, **kw) -> tuple[int, dict]:
        status, content, _ = self.request("DELETE", path, **kw)
        try:
            return status, json.loads(content.decode() or "null")
        except json.JSONDecodeError:
            return status, {"_raw": content.decode(errors="replace")}


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


def bar(pct: float, width: int = 30) -> str:
    filled = int(width * max(0.0, min(1.0, pct)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


_UNSAFE_DIRNAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def safe_dirname(name: str) -> str:
    """A ROM's display name, made safe to use as a single local directory
    component - strips characters that are path separators or otherwise
    reserved on common filesystems (mainly a Windows-host concern, since
    `name` itself is free text with no such restriction), and falls back to
    something non-empty if that leaves nothing usable."""
    cleaned = _UNSAFE_DIRNAME_CHARS.sub("_", name).strip(" .")
    return cleaned or "game"


def extract_error(content: bytes, status: int) -> str:
    try:
        data = json.loads(content.decode() or "null")
    except Exception:
        return f"HTTP {status}: {content.decode(errors='replace')[:300]}"
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict):
            return f"HTTP {status}: {detail.get('msg', detail)}"
        if detail:
            return f"HTTP {status}: {detail}"
        return f"HTTP {status}: {data}"
    return f"HTTP {status}: {content.decode(errors='replace')[:300]}"


class RommClient:
    def __init__(self, c: Client):
        self.c = c

    # -- auth / discovery -------------------------------------------------
    def login(self) -> bool:
        status, content, _ = self.c.request("POST", "/api/login")
        if status not in (200, 204, 303):
            warn(extract_error(content, status))
            return False
        log(f"logged in as {self.c.user}")
        return True

    def rom_info(self, rom_id: int) -> dict:
        status, data = self.c.get_json(f"/api/roms/{rom_id}")
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def worker_status(self) -> bool:
        status, data = self.c.get_json("/api/roms/install/worker-status")
        if status != 200:
            warn(extract_error(json.dumps(data).encode(), status))
            return False
        return bool(data.get("available", False))

    def candidates(self, rom_id: int) -> dict:
        status, data = self.c.get_json(f"/api/roms/{rom_id}/install/candidates")
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def proton_builds(self) -> list:
        status, data = self.c.get_json("/api/roms/install/proton-builds")
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data.get("builds", [])

    def proton_download(self, build_id: str) -> str:
        status, data = self.c.post_json(f"/api/roms/install/proton/{build_id}/download", {})
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data.get("job_id")

    def proton_progress(self, build_id: str) -> tuple[float | None, bool]:
        status, data = self.c.get_json(f"/api/roms/install/proton/{build_id}/progress")
        if status != 200:
            return None, False
        return data.get("progress"), bool(data.get("extracting", False))

    # -- session ----------------------------------------------------------
    def start_session(self, rom_id: int, installer_path: str | None,
                      proton_build: str | None, ttl: int | None) -> dict:
        body = {"installer_path": installer_path, "proton_build": proton_build}
        if ttl is not None:
            body["ttl_seconds"] = ttl
        status, data = self.c.post_json(f"/api/roms/{rom_id}/install", body)
        if status not in (200, 201):
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def get_session(self, rom_id: int) -> dict:
        status, data = self.c.get_json(f"/api/roms/{rom_id}/install")
        if status == 404:
            return {}
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def cancel_session(self, rom_id: int) -> dict:
        status, data = self.c.post_json(f"/api/roms/{rom_id}/install/cancel", {})
        if status not in (200, 204):
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def clear_cache(self, rom_id: int) -> dict:
        status, data = self.c.delete_json(f"/api/roms/{rom_id}/install")
        if status not in (200, 204):
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    # -- streaming --------------------------------------------------------
    def stream_manifest(self, rom_id: int) -> dict | None:
        """Live view of the install's output so far, or None if there's
        nothing to show yet.

        The backend 404s for two different reasons: no session exists at
        all, or one does but hasn't written any manifest yet (right after
        starting, or right after a fresh install was kicked off - see
        get_install_stream_manifest's own docstring). Either way, from a
        client streaming concurrently with the install, this just means
        "keep waiting" - not an error worth alarming anyone with.
        """
        status, data = self.c.get_json(f"/api/roms/{rom_id}/install/stream/manifest")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(extract_error(json.dumps(data).encode(), status))
        return data

    def stream_file(self, rom_id: int, path: str, out_dir: Path,
                    speed_limit: int | None = None) -> int:
        """Range-stream whatever is currently available for one file,
        resuming from wherever the local copy left off. Returns the file's
        total size on disk after this call (not just what was added now).

        Deliberately never blocks waiting for *more* to become available -
        drains everything already sealed in a tight loop, then returns the
        moment there's nothing further right now (a 416, or a 206 with an
        empty body). The very first version of this internally slept and
        retried on exactly those cases instead, which meant one file the
        installer hadn't started writing yet (still 0 bytes) stalled the
        whole per-file loop in download_all_files forever - every other
        file in the same manifest, including ones already fully sealed and
        ready, never even got a chance to be requested. The caller's own
        manifest-repoll loop (MANIFEST_INTERVAL) is what retries a file
        that isn't ready yet, on its next pass through the whole list.
        """
        encoded = urllib.parse.quote(path, safe="/")
        endpoint = f"/api/roms/{rom_id}/install/stream/{encoded}"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        if dest.exists():
            written = dest.stat().st_size

        while True:
            headers = {}
            if written > 0:
                headers["Range"] = f"bytes={written}-"
            status, content, resp_headers = self.c.request(
                "GET", endpoint, headers=headers, timeout=60.0)
            if status in (200, 206):
                mode = "ab" if written > 0 else "wb"
                with open(dest, mode) as f:
                    f.write(content)
                written += len(content)
                if status == 200 and not headers.get("Range"):
                    return written
                cr = resp_headers.get("Content-Range") or resp_headers.get("content-range")
                if cr and "/" in cr:
                    total_s = cr.rsplit("/", 1)[1]
                    if total_s.isdigit() and int(total_s) > 0:
                        if written >= int(total_s):
                            return written
                # 206 with nothing new this time - that's everything
                # currently available; stop here rather than wait for more.
                if len(content) == 0:
                    return written
                continue
            if status == 416:
                # Nothing sealed yet (or nothing beyond what we already
                # have) - not an error, just nothing more to give right now.
                return written
            raise RuntimeError(extract_error(content, status))


def download_all_files(rom: RommClient, rom_id: int, out_dir: Path,
                       speed_limit: int | None = None,
                       stop_event: threading.Event | None = None) -> int:
    """Poll manifest and stream every file to out_dir. Returns total bytes.

    Works the same whether the install is still running or already DONE -
    `stream/manifest` serves a best-effort live view while it's in progress
    (see handler.install.manifest) and the real, final one once it's not, so
    this loop just keeps polling either way until every listed file reports
    `complete`. That's what lets the caller start this concurrently with the
    install itself (a background thread) instead of waiting for it to
    finish first - the whole point of "stream install".

    `stop_event`, when given, is checked between polls so a caller running
    this in a background thread can ask it to give up early (e.g. the
    install failed - nothing more will ever seal) instead of retrying a
    dead session forever. A fresh one is used when not given, so the
    single-threaded (sequential, post-DONE) call site needs no special case.
    """
    stop_event = stop_event or threading.Event()
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    done_paths: set[str] = set()
    # Bytes already counted into `total` for each path so far, across
    # however many passes it's taken - stream_file now returns the file's
    # current on-disk size (not just what it added this call, since it no
    # longer blocks until a file is entirely done), so `total` has to track
    # the delta itself instead of just summing every return value.
    progress: dict[str, int] = {}
    last_manifest = 0.0
    while not stop_event.is_set():
        now = time.time()
        if now - last_manifest < MANIFEST_INTERVAL and done_paths:
            if stop_event.wait(MANIFEST_INTERVAL - (now - last_manifest)):
                break
        last_manifest = time.time()
        try:
            manifest = rom.stream_manifest(rom_id)
        except RuntimeError as e:
            warn(str(e))
            if stop_event.wait(MANIFEST_INTERVAL):
                break
            continue
        if manifest is None:
            # Nothing to show yet - the install (or the sandbox job behind
            # it) hasn't written a single byte so far, totally normal right
            # after starting. Not an error, so no WARN - just wait and
            # check again next pass.
            if stop_event.wait(MANIFEST_INTERVAL):
                break
            continue
        files = manifest.get("files", [])
        viewers = manifest.get("viewer_count", 0)
        limit = manifest.get("download_speed_limit_bytes_per_sec") or speed_limit
        log(f"manifest: {len(files)} file(s), {viewers} viewer(s)"
            + (f", limit {fmt_bytes(limit)}/s" if limit else ""))
        new_done = 0
        for f in files:
            if stop_event.is_set():
                return total
            path = f["path"]
            size = f.get("size_bytes", 0)
            complete = f.get("complete", False)
            if path in done_paths:
                continue
            # Already have it in full from a previous run of this CLI (the
            # common case once "already installed" just re-streams a cache
            # every time) - skip the network round-trip entirely. Without
            # this, stream_file would ask for bytes past what's already on
            # disk and just get a 416 back for its trouble.
            local_path = out_dir / path
            if complete and local_path.is_file() and local_path.stat().st_size >= size:
                delta = size - progress.get(path, 0)
                total += delta
                progress[path] = size
                done_paths.add(path)
                new_done += 1
                continue
            # Grabs whatever is currently available and returns - does not
            # block waiting for more (see stream_file's own docstring for
            # why that matters: a file the installer hasn't started writing
            # yet must never stall every other file in this same pass).
            try:
                written = rom.stream_file(rom_id, path, out_dir, speed_limit=limit)
            except RuntimeError as e:
                # A connection drop here would otherwise crash this
                # background thread with an unhandled traceback - the
                # server going away mid-download isn't something retrying
                # the very next file in this same pass can fix either, so
                # stop cleanly and let the caller's own poll loop notice.
                warn(str(e))
                return total
            delta = written - progress.get(path, 0)
            if delta > 0:
                log(f"  streaming {path} ({fmt_bytes(written)} / {fmt_bytes(size)}"
                    + (" DONE" if complete and written >= size else ")"))
            total += delta
            progress[path] = written
            if complete and written >= size:
                done_paths.add(path)
                new_done += 1
            elif complete and written < size:
                # The install itself is fully done, yet this file's own
                # download still fell short - worth flagging, unlike a
                # plain "not sealed yet" shortfall mid-install.
                warn(f"  {path}: only got {fmt_bytes(written)} of {fmt_bytes(size)}")
        if files and new_done == 0 and all(f.get("complete") for f in files):
            log("all files complete")
            break
        if not files:
            if stop_event.wait(MANIFEST_INTERVAL):
                break
    return total


def poll_session(rom: RommClient, rom_id: int, proton_build: str | None,
                 timeout: float = 600.0) -> dict:
    """Poll session state until terminal or timeout. Prints progress.

    AWAITING_INSTALLER (server-side auto-pick couldn't confidently resolve
    an installer - "manual mode") is reported and returned immediately, not
    retried: nothing else is ever going to move this state along except a
    human finishing it through the web UI's VNC session, so waiting out any
    part of `timeout` here would just be dead time. `main()` normally
    catches this right after starting the session, before ever calling this
    function - the check stays here too for whoever calls this directly.
    """
    deadline = time.time() + timeout
    last_state = None
    announced_install_page = False
    install_page_url = f"{rom.c.base}/rom/{rom_id}/install"
    while time.time() < deadline:
        session = rom.get_session(rom_id)
        if not session:
            warn("no active session")
            time.sleep(POLL_INTERVAL)
            continue
        state = session.get("state")
        vnc_url = session.get("vnc_url")
        bytes_written = session.get("bytes_written", 0)
        bytes_total = session.get("bytes_total", 0)
        if state != last_state:
            log(f"state: {state}")
            last_state = state
        if state == "awaiting_installer":
            url = session.get("manual_install_url")
            warn("session needs a manual installer pick - finish it in a browser:")
            if url:
                log(f"  {url}")
            warn("re-run this CLI once it's running there to stream the result")
            return session
        if state in ACTIVE_STATES:
            if bytes_total:
                pct = bytes_written / bytes_total if bytes_total else 0.0
                log(f"  {bar(pct)} {fmt_bytes(bytes_written)}/{fmt_bytes(bytes_total)}")
            # Announced once, not every poll - it's the same link for the
            # life of the session. Points at the web Install page (not the
            # raw vnc_url iframe target, which carries a session-scoped
            # token meant for embedding, not for a human to open directly)
            # so the user can watch/drive the installer through the normal
            # UI once the VNC bridge comes up.
            if vnc_url and not announced_install_page:
                log(f"  installer is running - the wizard itself still needs "
                    f"someone to click through it (no unattended \"auto mode\" "
                    f"yet), open this to do that:")
                log(f"  {install_page_url}")
                announced_install_page = True
            # Proton download progress during bootstrapping.
            if proton_build and state == "installing" and not vnc_url:
                prog, extracting = rom.proton_progress(proton_build)
                if prog is not None:
                    label = "extracting" if extracting else "downloading"
                    log(f"  proton {label} {bar(prog)} {prog * 100:.0f}%")
            time.sleep(POLL_INTERVAL)
            continue
        # terminal
        if state == "done":
            log("session DONE")
        elif state == "failed":
            warn(f"session FAILED: {session.get('error', '')}")
        elif state == "expired":
            warn("session EXPIRED")
        else:
            log(f"session state: {state}")
        return session
    warn("polling timed out")
    return rom.get_session(rom_id)


def main() -> int:
    p = argparse.ArgumentParser(description="RomM stream-install CLI client")
    p.add_argument("--base", required=True, help="RomM base URL, e.g. http://localhost:5100")
    p.add_argument("--user", default="admin")
    p.add_argument("--pass", dest="password", default="admin")
    p.add_argument("--rom-id", type=int, required=True)
    p.add_argument("--installer-path", default=None,
                   help="optional: relative path of installer inside the ROM dir, "
                        "to override the server's own auto-pick. Usually not "
                        "needed - omit it and let the server decide (or fall back "
                        "to manual mode if it can't).")
    p.add_argument("--proton-build", default=None,
                   help="proton build id (default: server)")
    p.add_argument("--ttl", type=int, default=None, help="cache TTL seconds")
    p.add_argument("--out", default="/tmp/romm-install",
                   help="base output dir - files land under a subfolder named "
                        "after the game, e.g. --out ~/roms -> "
                        "~/roms/<game name>/...")
    p.add_argument("--no-download", action="store_true",
                   help="start + poll, but do not stream files")
    p.add_argument("--cancel", action="store_true",
                   help="cancel an active session instead of starting")
    p.add_argument("--clear", action="store_true",
                   help="clear cache + session")
    p.add_argument("--list-proton", action="store_true",
                   help="list proton builds and exit")
    p.add_argument("--download-proton", default=None,
                   help="download a proton build by id and watch progress")
    p.add_argument("--timeout", type=float, default=900.0)
    args = p.parse_args()

    client = Client(args.base, args.user, args.password)
    rom = RommClient(client)

    try:
        return _run(args, rom)
    except RuntimeError as e:
        msg = str(e)
        if "connection error" in msg:
            warn(f"cannot reach {args.base} - is the RomM server running?")
        else:
            warn(msg)
        return 1
    except KeyboardInterrupt:
        warn("interrupted")
        return 130


def _run(args: argparse.Namespace, rom: RommClient) -> int:
    if not rom.login():
        return 1

    if args.list_proton:
        builds = rom.proton_builds()
        for b in builds:
            print(f"{b.get('id'):30s} installed={b.get('installed')} "
                  f"version={b.get('version')} source={b.get('source')}")
        return 0

    if args.download_proton:
        job_id = rom.proton_download(args.download_proton)
        log(f"proton download job: {job_id}")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            prog, extracting = rom.proton_progress(args.download_proton)
            if prog is None:
                time.sleep(PROTON_POLL_INTERVAL)
                continue
            label = "extracting" if extracting else "downloading"
            log(f"  {label} {bar(prog)} {prog * 100:.0f}%")
            if prog >= 1.0 and not extracting:
                log("proton download complete")
                return 0
            time.sleep(PROTON_POLL_INTERVAL)
        warn("proton download timed out")
        return 1

    if args.clear:
        rom.clear_cache(args.rom_id)
        log("cache cleared")
        return 0

    if args.cancel:
        rom.cancel_session(args.rom_id)
        log("session cancelled")
        return 0

    # Files land under a per-game subfolder of --out (e.g. --out ~/roms +
    # "Jazz Jackrabbit 2" -> ~/roms/Jazz Jackrabbit 2/...), not dumped flat -
    # a manifest's own paths already carry whatever installer-specific
    # top-level folders happened to end up in it (a vendor folder like
    # "GOG Games", a stray "users/..." shortcut, ...), which isn't a name a
    # human picked for this game and gets confusing fast with more than one
    # ROM sharing the same --out.
    info = rom.rom_info(args.rom_id)
    game_name = info.get("name") or info.get("fs_name_no_ext") or f"rom-{args.rom_id}"
    out_dir = Path(args.out) / safe_dirname(game_name)

    # Step 1: already installed? A DONE session means its cache is still on
    # disk - stream straight from it, no worker/candidates/start needed at
    # all (see start_install_session's own docstring for why POSTing here
    # would just hand the same session back anyway; skipping the POST
    # entirely also means an already-cached game can still be streamed even
    # if the install worker itself isn't running right now).
    existing = rom.get_session(args.rom_id)
    if existing and existing.get("state") == "done":
        log(f"already installed: session id={existing.get('id')} - streaming cached files")
        session = existing
    else:
        if not rom.worker_status():
            warn("install worker is not connected")
            return 1

        cands = rom.candidates(args.rom_id)
        log(f"candidates: {len(cands.get('candidates', []))} "
            f"needs_manual_pick={cands.get('needs_manual_pick')} "
            f"stream_copy={cands.get('stream_copy')}")
        for c in cands.get("candidates", []):
            log(f"  - {c['file_name']} ({fmt_bytes(c['file_size_bytes'])}) "
                f"rank={c['rank']} kind={c['kind']}")

        # Step 2: --installer-path stays a supported override, but is no
        # longer required - omit it and the server auto-picks the same way
        # the web UI's own "Install" button does (see
        # start_install_session's docstring). Only a genuinely ambiguous
        # pick falls back to "manual mode", handled below.
        session = rom.start_session(args.rom_id, args.installer_path,
                                    args.proton_build, args.ttl)
        log(f"session started: id={session.get('id')} state={session.get('state')}")

        # Step 3: manual mode - the server couldn't confidently resolve an
        # installer (or, for a title that needs it, nobody has clicked
        # through the installer's own dialogs yet). Send the human to the
        # web UI's VNC session and stop - nothing here can finish this.
        if session.get("state") == "awaiting_installer":
            url = session.get("manual_install_url")
            warn("this install needs a manual pick - finish it in a browser:")
            if url:
                log(f"  {url}")
            warn("re-run this CLI once it's running there to stream the result")
            return 1

    # Step 4: stream. Started concurrently with the install actually running
    # (a background thread), not after it finishes - "stream install" means
    # a client can already start pulling finished pieces of a file before
    # the whole install (and its final manifest) is done, see
    # download_all_files's own docstring. Skipped entirely for a session
    # already DONE only in the sense that there's nothing left to wait for -
    # download_all_files itself handles both cases identically.
    stop_event = threading.Event()
    stream_result: dict[str, int] = {}
    stream_thread: threading.Thread | None = None
    if not args.no_download:
        def _stream() -> None:
            stream_result["total"] = download_all_files(
                rom, args.rom_id, out_dir, stop_event=stop_event)

        stream_thread = threading.Thread(target=_stream, daemon=True)
        stream_thread.start()

    session = poll_session(rom, args.rom_id, args.proton_build,
                           timeout=args.timeout)
    state = session.get("state")

    if stream_thread is not None:
        if state != "done":
            # failed/expired/timed out - nothing more will ever seal, don't
            # let the streaming loop keep retrying a dead session forever.
            stop_event.set()
            stream_thread.join(timeout=30.0)
        else:
            # Let it finish naturally (it stops on its own once every listed
            # file reports complete) - the overall --timeout budget doubles
            # as a safety net so this can't hang forever either.
            stream_thread.join(timeout=args.timeout)
        total = stream_result.get("total", 0)
        if state == "done":
            log(f"downloaded {fmt_bytes(total)} to {out_dir}")
    elif state == "done":
        log("install done; files are cached on server, pass --no-download false to fetch")

    if state == "failed":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
