import asyncio
from pathlib import Path
from typing import Annotated

import aiohttp
from fastapi import Body, HTTPException, Response, WebSocket
from fastapi import Path as PathVar
from fastapi import Request, status
from pydantic import BaseModel
from rq.command import send_stop_job_command
from starlette.authentication import requires
from starlette.responses import FileResponse, StreamingResponse
from starlette.websockets import WebSocketState

from config import DEV_MODE, INSTALL_MAX_CONCURRENCY, INSTALL_WORKER_HOST, ROMM_BASE_URL
from decorators.auth import protected_route
from endpoints.responses.install import (
    InstallCandidatesSchema,
    InstallCandidateSchema,
    InstallDashboardEntrySchema,
    InstallDashboardSchema,
    InstallFileSchema,
    InstallFilesSchema,
    InstallSessionSchema,
    InstallStreamFileSchema,
    InstallStreamManifestSchema,
    InstallWorkerStatusSchema,
    ProtonBuildSchema,
    ProtonBuildsSchema,
    ProtonDownloadProgressSchema,
    ProtonDownloadResponseSchema,
)
from exceptions.endpoint_exceptions import (
    InstallConcurrencyLimitException,
    InstallSessionNotActiveException,
    InstallSessionNotFoundException,
    InstallSessionRunningException,
    InstallWorkerUnavailableException,
    ProtonBuildNotFoundException,
    RomNotFoundInDatabaseException,
)
from handler.auth.constants import Scope
from handler.auth.dependencies import assert_rom_visible
from handler.database import db_install_session_handler, db_rom_handler
from handler.filesystem import fs_rom_handler
from handler.filesystem.installer_detection import (
    WINDOWS_INSTALLABLE_SLUGS,
    pick_confident_installer,
)
from handler.install import bandwidth, stream_presence
from handler.install.manifest import (
    find_manifest_entry,
    live_view_of_final_manifest,
    manifest_total_bytes,
    read_live_manifest,
    read_manifest,
)
from handler.install.proton_builds import list_proton_builds, resolve_effective_build
from handler.install.queue_status import has_install_worker
from handler.install.runner import enqueue_install
from handler.install.stream_copy import enqueue_stream_copy
from handler.redis_handler import redis_client
from logger.logger import log
from models.install_session import (
    ACTIVE_INSTALL_STATES,
    RUNNING_INSTALL_STATES,
    InstallSession,
    InstallSessionState,
)
from utils.install_cache import clear_session_cache, resolve_expires_at, session_cache_dir
from utils.nginx import FileRedirectResponse
from utils.router import APIRouter

router = APIRouter()


class InstallStartForm(BaseModel):
    """Request body to start (or restart) an install session for a ROM."""

    # Relative path of the installer to run. Required when the ROM needs an
    # installer and none was auto-detected (needs_manual_pick was true).
    installer_path: str | None = None
    # Proton build id to run this installer under (see
    # handler.install.proton_builds). None (or an unknown/uninstalled id)
    # falls back to the server default.
    proton_build: str | None = None
    # Cache lifetime in seconds. ``None`` uses the configured default TTL,
    # a value <= 0 means unlimited (never auto-evict).
    ttl_seconds: int | None = None


def _session_schema(rom_id: int, session: InstallSession) -> InstallSessionSchema:
    """Build the response schema, filling in `manual_install_url` for a
    session that's actually waiting on one (see the field's own docstring) -
    every endpoint that returns a session goes through this so no client
    (this includes polling `GET /{id}/install`, not just the initial POST)
    ever sees AWAITING_INSTALLER without also getting the URL to send a
    human to.
    """
    schema = InstallSessionSchema.model_validate(session)
    if schema.state == InstallSessionState.AWAITING_INSTALLER:
        schema.manual_install_url = f"{ROMM_BASE_URL}/rom/{rom_id}/install"
    return schema


@protected_route(
    router.get,
    "/{id}/install/candidates",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def get_install_candidates(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> InstallCandidatesSchema:
    """Detect installer candidates for a Windows ROM.

    Returns a ranked list of files that could serve as the installer entry
    point. When nothing is detected the client must present a manual file
    picker. Non-Windows platforms are flagged for direct stream-copy instead.
    """
    if not has_install_worker():
        raise InstallWorkerUnavailableException()

    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    is_windows = rom.platform.slug in WINDOWS_INSTALLABLE_SLUGS

    candidates = fs_rom_handler.get_installer_candidates(rom)

    return InstallCandidatesSchema(
        rom_id=rom.id,
        candidates=[
            InstallCandidateSchema(
                path=c.path,
                file_name=c.file_name,
                file_size_bytes=c.file_size_bytes,
                rank=c.rank,
                kind=c.kind,
            )
            for c in candidates
        ],
        needs_manual_pick=is_windows and len(candidates) == 0,
        # Non-Windows ROMs don't need an installer; the client stream-copies them.
        stream_copy=not is_windows,
    )


@protected_route(
    router.post,
    "/{id}/install",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def start_install_session(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
    data: Annotated[InstallStartForm, Body()],
) -> InstallSessionSchema:
    """Create (or return) the install session for a ROM - the single
    entrypoint every client (web UI, CLI, ...) drives to get from "nothing
    yet" to "streamable", so they all get the exact same behavior for free
    instead of each re-implementing it:

    - Already running (INSTALLING/STREAMING)? Handed back as-is - never
      starts a second run.
    - Already installed (latest session is DONE)? A plain POST here still
      starts a genuinely new attempt (a fresh session, a fresh cache
      directory) - there's no separate "Reinstall" concept, pressing
      Install always tries to install. A client that wants "already have
      it, just stream what's there, don't touch the worker at all" (the
      CLI's own default behavior) checks `GET /{id}/install` itself first
      and only calls this when that isn't already DONE - see
      cli/romm-install-cli.py's own docstring. A client that wants the old
      cache gone first calls `DELETE /{id}/install` before this, same as
      always.
    - No installer chosen? Tries to resolve one itself first
      (`pick_confident_installer`, the same "well-known installer name"
      threshold the web UI used to apply client-side) before ever asking a
      human. Only when that fails does the session sit in
      AWAITING_INSTALLER with `manual_install_url` set - "manual mode": a
      person has to pick a file (and watch the installer's own dialogs)
      through the web Install page. There's no "auto mode" (e.g. OCR-driven,
      clicking through it unattended) yet - AWAITING_INSTALLER is the only
      outcome for now when auto-pick can't confidently decide.
    - Otherwise (Windows with a resolved path, or any non-Windows ROM): the
      sandbox runner (or stream-copy) is enqueued immediately.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    latest = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if latest and latest.state in RUNNING_INSTALL_STATES:
        return _session_schema(rom.id, latest)
    # DONE/FAILED/EXPIRED (or None) has nothing running worth protecting -
    # a fresh POST always starts a genuinely new attempt for any of them.
    # DETECTING/AWAITING_INSTALLER (never enqueued) is the only case
    # actually resumed below, updated in place rather than left to
    # accumulate a duplicate row per retry.
    existing = latest if latest and latest.state in ACTIVE_INSTALL_STATES else None

    is_windows = rom.platform.slug in WINDOWS_INSTALLABLE_SLUGS

    installer_path = data.installer_path
    if is_windows and installer_path is None:
        candidates = fs_rom_handler.get_installer_candidates(rom)
        confident = pick_confident_installer(candidates)
        if confident is not None:
            installer_path = confident.path
    needs_manual_pick = is_windows and not installer_path

    # Resolved now (not left NULL for the worker to decide implicitly) so the
    # client can poll /install/proton/{id}/progress and show "Downloading
    # Proton X…" instead of a silent stall while the worker auto-downloads it
    # on first use - see resolve_effective_build's own docstring.
    proton_build = resolve_effective_build(data.proton_build) if is_windows else None

    initial_state = (
        InstallSessionState.AWAITING_INSTALLER
        if needs_manual_pick
        else InstallSessionState.DETECTING
    )
    if existing:
        # Never enqueued, and this call has something new to offer (a
        # resolved path this time, or at least a fresh detection attempt) -
        # update in place rather than creating a second row for the same
        # in-progress attempt.
        session = db_install_session_handler.update_session(
            existing.id,
            {
                "installer_path": installer_path,
                "proton_build": proton_build,
                "state": initial_state,
            },
        )
    else:
        # Every session starts non-running regardless of platform: the
        # concurrency check below must count sessions already running, not
        # this brand-new one. Flipping straight to STREAMING here for
        # non-Windows ROMs would count this session against itself and
        # reject every stream-copy install outright once
        # INSTALL_MAX_CONCURRENCY sessions (default 1) exist anywhere,
        # including itself.
        session = db_install_session_handler.add_session(
            InstallSession(
                rom_id=rom.id,
                user_id=request.user.id,
                state=initial_state,
                installer_path=installer_path,
                proton_build=proton_build,
                expires_at=resolve_expires_at(data.ttl_seconds),
            )
        )

    # A Windows ROM with a resolved installer can start running immediately;
    # one still awaiting a manual pick stays in AWAITING_INSTALLER. A
    # non-Windows ROM has nothing to run, so it goes straight to copying.
    if needs_manual_pick:
        return _session_schema(rom.id, session)

    if db_install_session_handler.count_running_sessions() >= INSTALL_MAX_CONCURRENCY:
        db_install_session_handler.delete_session(session.id)
        raise InstallConcurrencyLimitException(INSTALL_MAX_CONCURRENCY)

    # Without this, enqueueing onto an unattended queue leaves the session
    # stuck "installing"/"streaming" forever: nothing ever touches its job to
    # trigger a failure, so the client polls indefinitely for a state change
    # that will never come.
    if not has_install_worker():
        db_install_session_handler.delete_session(session.id)
        raise InstallWorkerUnavailableException()

    if is_windows:
        job_id = enqueue_install(session.id)
        session = db_install_session_handler.update_session(
            session.id,
            {"state": InstallSessionState.INSTALLING, "job_id": job_id},
        )
    else:
        job_id = enqueue_stream_copy(session.id)
        session = db_install_session_handler.update_session(
            session.id,
            {"state": InstallSessionState.STREAMING, "job_id": job_id},
        )

    return _session_schema(rom.id, session)


@protected_route(
    router.get,
    "/{id}/install",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def get_install_session(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> InstallSessionSchema:
    """Return the latest install session for this user+ROM, if any."""
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)
    return _session_schema(rom.id, session)


@protected_route(
    router.delete,
    "/{id}/install",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def clear_install_session(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> None:
    """Clear the install cache and remove the latest session for this user+ROM.

    Backs the "Clear Install Cache" action. Deletes the on-disk cache directory
    and the session row so a fresh install can start clean.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)
    if session.state in RUNNING_INSTALL_STATES:
        raise InstallSessionRunningException(id)

    clear_session_cache(session.id)
    db_install_session_handler.delete_session(session.id)


@protected_route(
    router.post,
    "/{id}/install/cancel",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def cancel_install_session(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> InstallSessionSchema:
    """Abort a running install and clear whatever partial cache it left.

    Best-effort: asks RQ to stop the job if a worker is still actually
    working on it, then unconditionally marks the session FAILED and clears
    its cache directory regardless of whether that reached a live worker -
    RQ kills the job's whole process group outright (SIGKILL), so there's no
    graceful in-job cleanup to wait for; this call is the cleanup.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)
    if session.state not in ACTIVE_INSTALL_STATES:
        raise InstallSessionNotActiveException(id)

    if session.job_id:
        try:
            send_stop_job_command(redis_client, session.job_id)
        except Exception as e:  # noqa: BLE001 - the job may already be gone
            log.debug(f"Couldn't send stop command for job {session.job_id}: {e}")

    clear_session_cache(session.id)
    session = db_install_session_handler.update_session(
        session.id,
        {
            "state": InstallSessionState.FAILED,
            "error": "Cancelled by user",
            "vnc_url": None,
            "vnc_web_port": None,
        },
    )
    return InstallSessionSchema.model_validate(session)


@protected_route(
    router.get,
    "/install/worker-status",
    [Scope.ROMS_INSTALL],
)
async def install_worker_status(request: Request) -> InstallWorkerStatusSchema:
    """Whether an install-sandbox worker is currently connected.

    Drives whether the client offers "Install" at all; checked live rather
    than gated behind a static setting (see has_install_worker).
    """
    return InstallWorkerStatusSchema(available=has_install_worker())


@protected_route(
    router.get,
    "/install/proton-builds",
    [Scope.PLATFORMS_WRITE],
)
async def get_proton_builds(request: Request) -> ProtonBuildsSchema:
    """Proton builds this server knows about, installed or not.

    Installed builds are discovered at runtime by the ProtonBuildManager
    scanning PROTON_INSTALL_ROOT. Not-yet-installed builds come from upstream
    release APIs and can be downloaded via POST /install/proton/{id}/download.
    Only an installed build can be used for an install session - the frontend
    marks non-installed builds as disabled/selectable-for-download so the user
    can set them as the default (they auto-download on first use on the worker).
    """
    return ProtonBuildsSchema(
        builds=[
            ProtonBuildSchema(
                id=b.id,
                label=b.label,
                installed=b.installed,
                version=b.version,
                path=b.path,
                source=b.source,
                size_bytes=b.size_bytes,
            )
            for b in list_proton_builds()
        ]
    )


@protected_route(
    router.post,
    "/install/proton/{build_id}/download",
    [Scope.ROMS_INSTALL],
)
async def download_proton_build(
    request: Request,
    build_id: str,
) -> ProtonDownloadResponseSchema:
    """Enqueue a Proton build download on the install worker.

    Returns the RQ job id immediately (202-style); the frontend polls
    GET /install/proton/{build_id}/progress for completion. Only builds that
    are listed as not-installed (source="upstream") can be downloaded.
    """
    builds = list_proton_builds()
    match = next((b for b in builds if b.id == build_id), None)
    if match is None or match.installed:
        raise ProtonBuildNotFoundException(build_id)

    from handler.install.proton_builds import enqueue_download

    job_id = enqueue_download(build_id)
    return ProtonDownloadResponseSchema(job_id=job_id)


@protected_route(
    router.get,
    "/install/proton/{build_id}/progress",
    [Scope.ROMS_INSTALL],
)
async def get_proton_download_progress(
    request: Request,
    build_id: str,
) -> ProtonDownloadProgressSchema:
    """Download progress (0.0-1.0) for a Proton build, or None if not in progress.

    Returns ``extracting: true`` (progress=0.0) while the tarball is being
    unpacked, so the client can show "Installing Proton…" instead of a stale
    100 % from the completed download phase.
    """
    from handler.install.proton_builds import get_download_progress, is_extracting

    progress = get_download_progress(build_id)
    extracting = is_extracting(build_id)
    return ProtonDownloadProgressSchema(progress=progress, extracting=extracting)


@protected_route(
    router.delete,
    "/install/proton/{build_id}",
    [Scope.ROMS_INSTALL],
)
async def delete_proton_build(
    request: Request,
    build_id: str,
) -> dict[str, str]:
    """Remove a runtime-downloaded Proton build from disk.

    Only builds under PROTON_INSTALL_ROOT that were downloaded at runtime
    can be removed; baked-in image builds are not affected.
    """
    from handler.install.proton_builds import remove_build

    remove_build(build_id)
    return {"message": f"Proton build {build_id} removed"}


@protected_route(
    router.get,
    "/install/dashboard",
    [Scope.ROMS_INSTALL],
)
async def install_dashboard(request: Request) -> InstallDashboardSchema:
    """This user's install sessions worth surfacing on the Home page.

    One entry per ROM: still-active sessions and finished ones with a cache
    still on disk. Backs the "Active Installers" widget, which only renders
    when this list is non-empty. Entries for a ROM the caller can no longer
    see (a permission changed after the session was created) are dropped.
    """
    from handler.auth.dependencies import get_permissions

    perms = get_permissions(request)
    sessions = db_install_session_handler.get_dashboard_sessions_for_user(
        request.user.id
    )
    entries = []
    for session in sessions:
        rom = db_rom_handler.get_rom(session.rom_id)
        if not rom or not perms.can_see_rom(rom.id, rom.platform_id):
            continue
        entries.append(
            InstallDashboardEntrySchema(
                session=InstallSessionSchema.model_validate(session),
                rom_id=rom.id,
                rom_name=rom.name,
                platform_slug=rom.platform_slug,
                path_cover_small=rom.path_cover_small,
            )
        )
    return InstallDashboardSchema(entries=entries)


# Request headers that must never be forwarded upstream (RFC 7230 6.1
# hop-by-hop headers, plus Host/Content-Length which are wrong once the
# target host/body framing changes, and our own auth headers, which the
# install-worker has no use for and shouldn't see).
_VNC_PROXY_DROP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "cookie",
    "authorization",
}
# Response headers that must never be echoed back to the client for the
# same reason (aiohttp/Starlette manage framing and connection state
# themselves; letting the upstream's values through corrupts both) - plus
# Server/Date, which our own response object sets itself, so passing the
# upstream's through as well would just duplicate them.
_VNC_PROXY_DROP_RESPONSE_HEADERS = _VNC_PROXY_DROP_REQUEST_HEADERS | {
    "content-encoding",
    "server",
    "date",
}


def _assert_owns_vnc_port(user_id: int, port: int) -> None:
    """Confirm ``user_id`` actually owns the install running its VNC bridge
    on ``port`` before proxying anything through to it - the sandbox
    worker's HTTP/WS server itself has no auth of its own."""
    session = db_install_session_handler.get_running_session_for_user_port(
        user_id, port
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


@protected_route(
    router.get,
    "/install/vnc/{port}/{path:path}",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_403_FORBIDDEN: {}},
)
async def install_vnc_http(
    request: Request,
    port: Annotated[int, PathVar(ge=1, le=65535)],
    path: str,
) -> Response:
    """Proxy the noVNC static page/assets for a running install's VNC bridge
    through to the install-worker, gated on the owning user's session. The
    actual VNC pixel data goes over install_vnc_ws instead; this only ever
    serves vnc.html and its JS/CSS.
    """
    _assert_owns_vnc_port(request.user.id, port)

    upstream_url = f"http://{INSTALL_WORKER_HOST}:{port}/{path}"
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _VNC_PROXY_DROP_REQUEST_HEADERS
    }

    session = aiohttp.ClientSession()
    try:
        upstream_response = await session.request(
            request.method,
            upstream_url,
            params=list(request.query_params.multi_items()),
            headers=forward_headers,
            data=await request.body(),
        )
    except aiohttp.ClientError as e:
        await session.close()
        raise InstallWorkerUnavailableException() from e

    async def body_stream():
        try:
            async for chunk in upstream_response.content.iter_any():
                yield chunk
        finally:
            upstream_response.close()
            await session.close()

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _VNC_PROXY_DROP_RESPONSE_HEADERS
    }
    # noVNC static assets are small and only change when the worker image
    # is rebuilt — but a stale browser cache of an older ui.js/vnc.html
    # (e.g. from a different noVNC version) causes confusing crashes like
    # "Cannot read properties of null (reading 'addEventListener')" because
    # the cached JS references DOM elements the cached HTML doesn't define.
    response_headers["Cache-Control"] = "no-store"
    return StreamingResponse(
        body_stream(),
        status_code=upstream_response.status,
        headers=response_headers,
        media_type=upstream_response.content_type,
    )


# protected_route can't be used here: it injects FastAPI Security()
# dependencies (OAuth2PasswordBearer, HTTPBasic) meant for HTTP requests,
# which crash with a TypeError when resolved for a websocket connection (no
# Request to give them). requires() alone is what actually enforces the
# scope for either kind of route (see decorators/auth.py and
# starlette.authentication.requires' own websocket/request branching) - a
# bearer/basic Authorization header isn't usable from a websocket handshake
# in a browser anyway, so nothing is lost; cookie-session auth (the normal
# case for a same-origin page like the VNC overlay) still applies.
@router.websocket("/install/vnc/{port}/{path:path}")
@requires([Scope.ROMS_INSTALL])
async def install_vnc_ws(
    websocket: WebSocket,
    port: Annotated[int, PathVar(ge=1, le=65535)],
    path: str,
) -> None:
    """Proxy the actual VNC websocket (binary RFB-over-websocket frames via
    websockify on the install-worker) for a running install, gated on the
    owning user's session. See install_vnc_http for the static page/assets.
    """
    try:
        _assert_owns_vnc_port(websocket.user.id, port)
    except HTTPException:
        await websocket.close(code=4403)
        return

    requested_protocols = [
        p.strip()
        for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if p.strip()
    ]
    upstream_url = f"ws://{INSTALL_WORKER_HOST}:{port}/{path}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(
                upstream_url,
                params=list(websocket.query_params.multi_items()),
                protocols=requested_protocols or (),
            ) as upstream:
                await websocket.accept(subprotocol=upstream.protocol)

                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send_bytes(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send_str(message["text"])

                async def upstream_to_client():
                    async for message in upstream:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            await websocket.send_bytes(message.data)
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(message.data)
                        else:
                            return

                first_done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
        except aiohttp.ClientError:
            log.debug(f"Couldn't reach install-worker VNC bridge on port {port}")
        finally:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()


@protected_route(
    router.get,
    "/{id}/install/files",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def get_install_files(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> InstallFilesSchema:
    """List the files a finished install produced, with their sha1 hashes.

    A client downloads each one from GET .../install/files/{path} and
    compares the hash against its own copy to catch a corrupted transfer.
    404s until the session reaches a state that actually wrote a manifest
    (DONE; a run that FAILED partway never gets one).
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)

    entries = read_manifest(session_cache_dir(session.id))
    if entries is None:
        raise InstallSessionNotFoundException(id)

    return InstallFilesSchema(
        rom_id=rom.id,
        total_bytes=manifest_total_bytes(entries),
        files=[
            InstallFileSchema(path=e.path, size_bytes=e.size_bytes, sha1=e.sha1)
            for e in entries
        ],
    )


@protected_route(
    router.get,
    "/{id}/install/files/{file_path:path}",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def download_install_file(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
    file_path: str,
) -> Response:
    """Download one file from a finished install.

    `file_path` must be an exact entry in the session's manifest: anything
    else 404s before ever touching the filesystem, so this can't be used to
    read outside the session's own cache directory.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)

    entries = read_manifest(session_cache_dir(session.id))
    entry = find_manifest_entry(entries, file_path) if entries else None
    if not entry:
        raise InstallSessionNotFoundException(id)

    if DEV_MODE:
        return FileResponse(
            path=session_cache_dir(session.id) / entry.path,
            filename=Path(entry.path).name,
            media_type="application/octet-stream",
        )

    return FileRedirectResponse(
        download_path=Path(f"/cache/installs/{session.id}/{entry.path}"),
        filename=Path(entry.path).name,
    )


# Network-write granularity for the still-running-install streaming path
# below - decoupled from manifest.CHUNK_SIZE (the hashing/repair unit), just
# how much we read+throttle+yield at a time.
_STREAM_PIECE_SIZE = 256 * 1024


def _parse_range(
    range_header: str | None, sealed_bytes: int
) -> tuple[int, int] | tuple[None, None]:
    """Clamp a Range request to the hash-verified prefix of a still-growing
    file (``[0, sealed_bytes)``). No Range header defaults to serving from
    the start. ``(None, None)`` means nothing sealed is left to give for the
    requested range - the caller responds 416, and a client's own retry/
    backoff loop naturally waits for more of the file to seal."""
    start = 0
    end = sealed_bytes - 1
    if range_header and range_header.startswith("bytes="):
        spec = range_header[len("bytes=") :].split(",")[0].strip()
        range_start, _, range_end = spec.partition("-")
        if range_start:
            try:
                start = int(range_start)
            except ValueError:
                start = 0
        if range_end:
            try:
                end = min(end, int(range_end))
            except ValueError:
                pass
    if sealed_bytes <= 0 or start > end:
        return None, None
    return start, end


@protected_route(
    router.get,
    "/{id}/install/stream/manifest",
    [Scope.ROMS_INSTALL],
    responses={status.HTTP_404_NOT_FOUND: {}},
)
async def get_install_stream_manifest(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
) -> InstallStreamManifestSchema:
    """Live view of an install's output, whether it's still running or
    already finished - lets the Install page poll ONE endpoint regardless
    of state.

    While the install is running, entries come from the best-effort live
    manifest (see handler.install.manifest.scan_live_manifest); once it's
    DONE, the same shape is synthesized from the real, fully-verified
    manifest instead, so a completed session's files never look different
    to a client just because it stopped polling and came back.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)

    cache_dir = session_cache_dir(session.id)
    live_entries = read_live_manifest(cache_dir)
    if live_entries is None:
        final_entries = read_manifest(cache_dir)
        if final_entries is None:
            raise InstallSessionNotFoundException(id)
        live_entries = live_view_of_final_manifest(final_entries)

    return InstallStreamManifestSchema(
        rom_id=rom.id,
        files=[
            InstallStreamFileSchema(
                path=e.path,
                size_bytes=e.size_bytes,
                sealed_bytes=e.sealed_bytes,
                complete=e.complete,
            )
            for e in live_entries.values()
        ],
        viewer_count=await stream_presence.count_viewers(session.id),
        download_speed_limit_bytes_per_sec=await bandwidth.get_bytes_per_second(),
    )


@protected_route(
    router.get,
    "/{id}/install/stream/{file_path:path}",
    [Scope.ROMS_INSTALL],
    responses={
        status.HTTP_404_NOT_FOUND: {},
        status.HTTP_416_RANGE_NOT_SATISFIABLE: {},
    },
)
async def download_install_stream_file(
    request: Request,
    id: Annotated[int, PathVar(description="Rom internal id.", ge=1)],
    file_path: str,
    device_id: str = "web",
) -> Response:
    """Download one file from an install, live if it's still running.

    Once the file is complete this behaves exactly like
    GET .../install/files/{path} (same code path, unchanged - nginx/
    FileResponse Range support applies as always). While it's still being
    written, this instead serves a Range request clamped to the sealed
    (hash-verified) portion so far: a request past what's sealed yet gets
    416, and any browser's or download manager's own retry/backoff loop
    naturally waits for more of the file to seal before trying again -
    that's the whole resume story, no session/token state needed for it.

    `file_path` must be an exact live- or final-manifest entry, same
    traversal guard as the existing download endpoint.
    """
    rom = db_rom_handler.get_rom(id)
    if not rom:
        raise RomNotFoundInDatabaseException(id)
    assert_rom_visible(request, rom)

    session = db_install_session_handler.get_latest_session_for_rom(
        rom.id, request.user.id
    )
    if not session:
        raise InstallSessionNotFoundException(id)

    cache_dir = session_cache_dir(session.id)
    live_entries = read_live_manifest(cache_dir)
    entry = live_entries.get(file_path) if live_entries else None

    if entry is None:
        # No live manifest at all (install already finished) or this path
        # was never part of it - either way, defer to the exact existing
        # "complete file" path, unchanged.
        final_entries = read_manifest(cache_dir)
        final_entry = (
            find_manifest_entry(final_entries, file_path) if final_entries else None
        )
        if not final_entry:
            raise InstallSessionNotFoundException(id)

        if DEV_MODE:
            return FileResponse(
                path=cache_dir / final_entry.path,
                filename=Path(final_entry.path).name,
                media_type="application/octet-stream",
            )
        return FileRedirectResponse(
            download_path=Path(f"/cache/installs/{session.id}/{final_entry.path}"),
            filename=Path(final_entry.path).name,
        )

    start, end = _parse_range(request.headers.get("range"), entry.sealed_bytes)
    if start is None:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Retry-After": "1"},
        )

    await stream_presence.heartbeat(session.id, request.user.id, device_id)

    file_on_disk = cache_dir / entry.path
    content_length = end - start + 1

    async def body_stream():
        # Reads happen in a thread (asyncio.to_thread, matching the pattern
        # already used for other blocking file/hash work in this codebase,
        # e.g. handler/filesystem/roms_handler.py) - a plain sync read here
        # would block the event loop for every other request while this
        # multi-GB file is served.
        remaining = content_length
        f = await asyncio.to_thread(file_on_disk.open, "rb")
        try:
            await asyncio.to_thread(f.seek, start)
            while remaining > 0:
                piece = await asyncio.to_thread(f.read, min(remaining, _STREAM_PIECE_SIZE))
                if not piece:
                    break
                await bandwidth.acquire(len(piece))
                remaining -= len(piece)
                yield piece
        finally:
            await asyncio.to_thread(f.close)

    return StreamingResponse(
        body_stream(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type="application/octet-stream",
        headers={
            "Content-Range": f"bytes {start}-{end}/*",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
        },
    )
