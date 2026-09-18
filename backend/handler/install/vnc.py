"""Per-session virtual display + VNC bridge for interactive installers.

Each install run gets its own headless X server (Xvfb), an x11vnc server bound
to loopback and protected by a random per-session password, and a websockify/
noVNC listener that browsers connect to. Everything is torn down when the run
ends. Ports are leased from a configured range so concurrent installs never
collide.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from config import (
    INSTALL_VNC_PORT_MAX,
    INSTALL_VNC_PORT_MIN,
    INSTALL_VNC_PUBLIC_BASE_URL,
)
from logger.formatter import highlight as hl
from logger.logger import log

# x11vnc listens on loopback - only websockify (same container, same network
# namespace) talks to it, so it's never reachable directly. websockify is
# the one thing that has to be reachable from *outside* the container (the
# backend's VNC proxy, see endpoints/roms/install.py's install_vnc_http/ws),
# so it binds every interface rather than loopback. Binding it to
# VNC_INTERNAL_HOST here instead is the exact bug that made "View install"
# fail with a connection reset regardless of any proxy config: the port was
# never actually listening on the container's real network interface.
VNC_INTERNAL_HOST = "127.0.0.1"
VNC_PUBLIC_HOST = "0.0.0.0"  # noqa: S104 - deliberately every interface, see above
VNC_PASSWORD_BYTES = 18
# Fixed, not matched to the browser's actual window: installers are small
# dialog-sized windows, not full desktops, and a fixed server-side
# resolution is simpler than resizing the real X server on the fly. noVNC
# scales this visually to fit its container (see _build_public_url's
# "resize=scale"), so the page itself still reads responsive.
INSTALL_VNC_RESOLUTION = "800x600x24"
# Xvfb starts asynchronously; give its socket this long to appear before
# giving up (x11vnc and the installer both need DISPLAY ready before they run).
XVFB_READY_TIMEOUT = 5.0
# Grace period after Xvfb's socket appears, before anything else connects to
# it (see start_vnc_session).
XVFB_SETTLE_DELAY = 0.5
# x11vnc and websockify are both started with subprocess.Popen (fire and
# forget) and both take a moment to actually bind their listening socket.
# Without waiting for that, the client can be handed a vnc_url that points
# at a port nothing is listening on yet - noVNC then sits on "Connecting..."
# forever (a plain connection failure, unlike a proper RFB rejection, isn't
# something it recovers from) instead of a fast, clear error.
VNC_PROCESS_READY_TIMEOUT = 5.0
# Module-level (not a constant) so tests can redirect it to a tmp_path.
X11_SOCKET_DIR = Path("/tmp/.X11-unix")
X11_LOCK_DIR = Path("/tmp")


@dataclass
class VncSession:
    """Handles for a running virtual display + VNC bridge."""

    display: str
    vnc_port: int
    web_port: int
    password: str
    public_url: str
    _procs: list[subprocess.Popen] = None  # type: ignore[assignment]
    _passwd_file: str | None = None

    def stop(self) -> None:
        """Terminate all child processes and remove the password file."""
        _terminate_all(self._procs or [])
        if self._passwd_file and Path(self._passwd_file).exists():
            Path(self._passwd_file).unlink(missing_ok=True)


def _lease_free_port(preferred: int) -> int:
    """Return a bindable TCP port at or after ``preferred`` within the range."""
    for port in range(preferred, INSTALL_VNC_PORT_MAX + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((VNC_INTERNAL_HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free VNC port available in the configured range")


def _display_number(web_port: int) -> str:
    """Derive a virtual display id from the leased web port to avoid clashes."""
    return f":{100 + (web_port - INSTALL_VNC_PORT_MIN)}"


def _clear_stale_x11_artifacts(display: str) -> None:
    """Remove a leftover lock file/socket for this display.

    A cancelled install stops its RQ job with an uncatchable SIGKILL to the
    whole process group, so a previous session's Xvfb dies without running
    its own cleanup and leaves /tmp/.X<N>-lock and the X11 socket behind.
    Xvfb refuses to start against a display that still has either, even
    though nothing is actually listening anymore. Safe to always do: display
    is derived from the just-leased (and therefore currently free) web port,
    so whatever last used this exact display is already gone.
    """
    display_num = display.lstrip(":")
    (X11_LOCK_DIR / f".X{display_num}-lock").unlink(missing_ok=True)
    (X11_SOCKET_DIR / f"X{display_num}").unlink(missing_ok=True)


def _wait_for_x11_socket(display: str, timeout: float = XVFB_READY_TIMEOUT) -> bool:
    """Poll for Xvfb's unix socket so nothing tries to connect before it exists."""
    socket_path = X11_SOCKET_DIR / f"X{display.lstrip(':')}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return True
        time.sleep(0.05)
    return socket_path.exists()


def _wait_for_tcp_port(
    host: str, port: int, timeout: float = VNC_PROCESS_READY_TIMEOUT
) -> bool:
    """Poll until something accepts connections on host:port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.05)
    return False


def _terminate_all(procs: list[subprocess.Popen]) -> None:
    for proc in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _write_vnc_password() -> tuple[str, str]:
    """Generate a random VNC password and store it via x11vnc's storepasswd."""
    password = secrets.token_urlsafe(VNC_PASSWORD_BYTES)
    fd, path = tempfile.mkstemp(prefix="vncpass_", suffix=".pw")
    Path(path).chmod(0o600)
    # storepasswd writes the obfuscated password file x11vnc expects.
    subprocess.run(
        ["x11vnc", "-storepasswd", password, path],
        check=True,
        capture_output=True,
    )
    return password, path


def _build_public_url(web_port: int, password: str) -> str:
    """Build the browser-facing noVNC URL for a leased web port."""
    if INSTALL_VNC_PUBLIC_BASE_URL:
        # Proxied mode: the browser never reaches this worker directly, it
        # goes through RomM's own backend, which proxies
        # /api/roms/install/vnc/<port>/... to this port on the install-worker
        # once it's confirmed the owning user's session is the one asking
        # (see endpoints/roms/install.py's install_vnc_http/install_vnc_ws).
        base = INSTALL_VNC_PUBLIC_BASE_URL.rstrip("/")
        # noVNC's own UI builds the websocket URL from document.location.host
        # plus a bare "path" setting (default "websockify"), not from the
        # page's own URL - it has no idea it's being served from a subpath
        # here rather than the domain root. Left at its default, the browser
        # tries to connect to ws://<host>/websockify (no proxy prefix at
        # all), which never reaches this session's proxied endpoint and
        # just sits on "Connecting..." forever. Overriding it via the
        # documented "path" query param points it at the right place.
        proxy_path = f"api/roms/install/vnc/{web_port}"
        return (
            f"{base}/{proxy_path}/vnc.html"
            f"?autoconnect=true&password={password}&path={proxy_path}/websockify"
            f"&resize=scale"
        )
    # No proxy configured (e.g. bare-metal/dev testing on one host): the
    # client connects to this worker's port directly, from the domain root,
    # where noVNC's "websockify" default path setting is already correct.
    return (
        f"http://127.0.0.1:{web_port}/vnc.html"
        f"?autoconnect=true&password={password}&resize=scale"
    )


def start_vnc_session(session_id: int, novnc_web_root: str) -> VncSession:
    """Start Xvfb + x11vnc + websockify for a session and return its handles."""
    web_port = _lease_free_port(INSTALL_VNC_PORT_MIN)
    vnc_port = _lease_free_port(max(INSTALL_VNC_PORT_MIN, web_port + 1))
    display = _display_number(web_port)
    password, passwd_file = _write_vnc_password()

    procs: list[subprocess.Popen] = []

    _clear_stale_x11_artifacts(display)

    # Headless X server for the installer's GUI.
    procs.append(
        subprocess.Popen(
            [
                "Xvfb",
                display,
                "-screen",
                "0",
                INSTALL_VNC_RESOLUTION,
                "-nolisten",
                "tcp",
                # Explicit, not assumed default: content that isn't
                # currently redrawing has been observed to render
                # unreliably (blank/transparent) without it.
                "+bs",
            ],
        )
    )

    def abort(reason: str) -> None:
        _terminate_all(procs)
        if Path(passwd_file).exists():
            Path(passwd_file).unlink(missing_ok=True)
        raise RuntimeError(reason)

    # Xvfb starts asynchronously; x11vnc and the installer both need DISPLAY
    # reachable before they run, or they fail (or hang) trying to connect.
    if not _wait_for_x11_socket(display):
        abort(f"Xvfb did not become ready on display {display}")

    # The socket existing only means Xvfb has bound it, not that the server
    # is done with its own internal setup - x11vnc's initial connection/grab
    # below and Wine's own connection (once the caller launches it) have been
    # observed to race a not-quite-ready Xvfb into a bad handshake. A short
    # settle delay here is cheaper than debugging that race at every call site.
    time.sleep(XVFB_SETTLE_DELAY)

    # A window manager is not optional here: without one, Wine's installer
    # windows (Inno Setup wizards especially) have been observed to map at
    # their placeholder 1x1 geometry and never get resized - nothing ever
    # negotiates their real size, since that's normally the WM's job. Started
    # before x11vnc so it's already claimed the screen by the time a client
    # can connect, and well before the installer itself launches.
    # --config: without it, a brand-new dialog window never gets real X11
    # input focus on this fresh session (icewm's own default only auto-
    # focuses a dialog/transient window if some *other* window already had
    # focus - nothing ever has, on a session this young). Unfocused, neither
    # the auto-advance clicker's nor a real user's keystrokes reach it at
    # all. See docker/icewm-preferences for the actual override.
    procs.append(
        subprocess.Popen(
            ["icewm", "--config", "/etc/icewm-preferences"],
            env={**os.environ, "DISPLAY": display},
        )
    )

    # Compositor: Inno Setup/InstallShield wizards commonly draw their pages
    # as layered (WS_EX_LAYERED) windows for rounded corners/gradients - a
    # layered window renders fully transparent (i.e. shows nothing at all,
    # indistinguishable from a blank window) unless something is actually
    # compositing it. xcompmgr (the minimal reference compositor) wasn't
    # enough here - the window kept painting opaque black instead of its
    # real content; picom's more complete backend actually resolves it.
    # xrender backend: no GPU/GLX in this sandbox. Shadows/fading/blur/vsync
    # all off - only actual compositing is needed, not desktop eye candy.
    procs.append(
        subprocess.Popen(
            [
                "picom",
                "--backend",
                "xrender",
                "--no-vsync",
                "--no-fading-openclose",
            ],
            env={**os.environ, "DISPLAY": display},
        )
    )

    # VNC server bound to loopback, password-protected, shared, view+control.
    procs.append(
        subprocess.Popen(
            [
                "x11vnc",
                "-display",
                display,
                "-rfbport",
                str(vnc_port),
                "-rfbauth",
                passwd_file,
                "-localhost",
                "-forever",
                "-shared",
                "-noxdamage",
            ],
        )
    )
    # Without this, a client that connects before x11vnc has actually bound
    # its port gets a bare TCP RST from nothing listening yet; noVNC has no
    # way to recover from that and just sits on "Connecting..." forever.
    if not _wait_for_tcp_port(VNC_INTERNAL_HOST, vnc_port):
        abort(f"x11vnc did not become ready on port {vnc_port}")

    # Websocket proxy serving the noVNC client - its listen side has to be
    # reachable from outside this container (the backend's VNC proxy), its
    # upstream side is x11vnc, in this same container, on loopback.
    procs.append(
        subprocess.Popen(
            [
                "websockify",
                "--web",
                novnc_web_root,
                f"{VNC_PUBLIC_HOST}:{web_port}",
                f"{VNC_INTERNAL_HOST}:{vnc_port}",
            ],
        )
    )
    # Same reasoning as the x11vnc wait above, for the port the browser
    # actually connects to.
    if not _wait_for_tcp_port(VNC_INTERNAL_HOST, web_port):
        abort(f"websockify did not become ready on port {web_port}")

    public_url = _build_public_url(web_port, password)

    log.info(
        f"Started VNC session for install {hl(str(session_id))} on "
        f"display {hl(display)} (web port {hl(str(web_port))})"
    )

    return VncSession(
        display=display,
        vnc_port=vnc_port,
        web_port=web_port,
        password=password,
        public_url=public_url,
        _procs=procs,
        _passwd_file=passwd_file,
    )
