# Install & Stream-Install API

Server-side installer sandbox for Windows ROMs (Proton/Wine + a VNC-driven install), and the HTTP API that lets a client start pulling a file as it's being written rather than waiting for the whole install to finish. This document is for third-party client implementers (companion apps, launchers) integrating against this system - it goes into more per-endpoint detail than the rest of `BACKEND_ARCHITECTURE.md` for that reason.

All endpoints below live under `/api/roms` and require the `ROMS_INSTALL` scope unless noted. `{id}` is always the ROM's internal id.

## 1. Overview

A Windows ROM can be installed on the server instead of (or before) being downloaded: the backend runs the ROM's installer inside a sandboxed Wine/Proton prefix with its own virtual display, exposed to the client as a VNC session so the user can click through the installer UI. The files the installer writes land in a per-session cache directory.

Two ways to get those files:

- **Wait for it to finish**, then download normal, complete files (`/{id}/install/files*`).
- **Stream them as they're written** (`/{id}/install/stream/*`): the backend maintains a live, best-effort manifest of what's on disk and how much of each file is safe to serve, and a client can start an HTTP Range GET against a file that's still growing. This is what the rest of this document mostly covers.

## 2. Session lifecycle

An install session moves through:

| State                | Meaning                                                      |
| -------------------- | ------------------------------------------------------------ |
| `detecting`          | Looking for installer candidates inside the ROM's files      |
| `awaiting_installer` | Waiting on a manual installer-file pick (ambiguous ROM)      |
| `installing`         | Sandbox is up, installer is running, VNC is available        |
| `streaming`          | Non-Windows ROM being copied straight through (no installer) |
| `done`               | Finished; the final manifest and files are available         |
| `failed`             | Errored or aborted; any partial cache has been deleted       |
| `expired`            | Session/cache TTL passed                                     |

`detecting`, `awaiting_installer`, `installing` and `streaming` are the _active_ states.

| Method | Path                       | Scope        | Description                                                            |
| ------ | -------------------------- | ------------ | ---------------------------------------------------------------------- |
| GET    | `/{id}/install/candidates` | ROMS_INSTALL | Detect installer entry-point candidates inside the ROM's files         |
| POST   | `/{id}/install`            | ROMS_INSTALL | Start (or return the existing) install session                         |
| GET    | `/{id}/install`            | ROMS_INSTALL | Get the latest install session for this ROM/user                       |
| DELETE | `/{id}/install`            | ROMS_INSTALL | Clear the install cache and delete the session                         |
| POST   | `/{id}/install/cancel`     | ROMS_INSTALL | Abort a running install: stop the sandbox and delete the partial cache |
| GET    | `/install/worker-status`   | ROMS_INSTALL | Whether an install-sandbox worker is currently connected               |
| GET    | `/install/proton-builds`   | ROMS_INSTALL | Proton builds the server knows about                                   |
| GET    | `/install/dashboard`       | ROMS_INSTALL | This user's active/recently-finished install sessions                  |

## 3. Stream delivery

### 3.1 `GET /{id}/install/stream/manifest`

Polled regardless of session state - while the install is running this reflects the best-effort live manifest; once `done`, the same shape is synthesized from the final, fully-verified manifest, so a client that stops polling and comes back later sees no difference.

```json
{
  "rom_id": 42,
  "files": [
    {
      "path": "MyGame/game.exe",
      "size_bytes": 1048576000,
      "sealed_bytes": 734003200,
      "complete": false
    }
  ],
  "viewer_count": 2,
  "download_speed_limit_bytes_per_sec": 5242880
}
```

- `size_bytes` is the file's current on-disk size (grows over time while `complete` is `false`).
- `sealed_bytes` is the byte offset the file is safe to read up to right now - see below.
- `viewer_count` and `download_speed_limit_bytes_per_sec` are described in §4.

### 3.2 `GET /{id}/install/stream/{file_path}`

`file_path` must be an exact path from the manifest above (same traversal guard as the non-streaming download endpoint - anything else 404s before touching the filesystem). Accepts an optional `?device_id=` query param (any client-chosen string, used only for the viewer count in §4).

**Once a file is `complete`**, this behaves exactly like the plain `/{id}/install/files/{path}` download - normal whole-file semantics, standard Range support.

**While a file is still being written**, requests are served against the growing file, but only up to `sealed_bytes` - the point at which the backend has seen the size hold steady for one scan interval, so it isn't about to be rewritten out from under a reader (no hashing is done at this stage - see §4 for where integrity is actually checked). This is the entire resumability story, and it's deliberately just standard HTTP:

- Send a normal `Range: bytes=<start>-` (or `<start>-<end>`) request.
- If any of the requested range falls within `[0, sealed_bytes)`, you get `206 Partial Content` with `Content-Range: bytes <start>-<end>/*` (the `*` because the final total size isn't known yet) and exactly that many bytes.
- If the requested range starts at or past `sealed_bytes` (nothing new to give yet), you get `416 Range Not Satisfiable` with a `Retry-After: 1` header. There is no session or token to track - retry the identical `Range` request after the given delay (or your own backoff) and it will succeed once the file has grown further.
- No `Range` header defaults to serving from byte 0.

**Client disconnect/reconnect** (e.g. the device sleeps mid-download) needs no special handling: reconnect and re-issue a `Range` request starting from however many bytes you already have. There is no server-side per-client session state for a stream download - the growing file plus `sealed_bytes` is the entire state.

## 4. Integrity, viewer presence, and bandwidth

**Integrity**: once a session reaches `done`, `GET /{id}/install/files` lists every file with its `sha1`. A client should hash what it received and compare; a mismatch means re-fetching that file (any byte range works, per §3.2's Range support once `complete`). There is currently no server-exposed per-chunk hash for a more surgical partial re-fetch - a corrupted transfer is a whole-file re-download.

**Viewer presence**: `viewer_count` in the manifest response counts other clients actively pulling this same session's files right now (a TTL-based heartbeat refreshed by every in-flight stream request - not a persistent subscription). It's informational only; nothing in the API changes behavior based on it.

**Bandwidth**: `download_speed_limit_bytes_per_sec` is a single administrator-configured cap, shared across _every_ concurrent stream-install transfer on the server (not per-file or per-session - like a torrent client's global rate limit). It's surfaced read-only here; there's no per-client or per-request way to adjust it.

## 5. Complete-file endpoints (no streaming)

| Method | Path                              | Scope        | Description                                            |
| ------ | --------------------------------- | ------------ | ------------------------------------------------------ |
| GET    | `/{id}/install/files`             | ROMS_INSTALL | List a finished install's files with sha1 hashes       |
| GET    | `/{id}/install/files/{file_path}` | ROMS_INSTALL | Download one finished file (exact manifest path match) |

These 404 until the session reaches `done` - a session that `failed` never produces a manifest here.

## 6. Example CLI client

`cli/romm-install-cli.py` is a minimal, pure-stdlib (no third-party dependencies) command-line client that drives the whole flow described above. It's intended as a quick way to exercise the stream-install feature by hand, and as a reference implementation for the API. Run it with no flags first to see all options:

```bash
python cli/romm-install-cli.py --help
```

A typical end-to-end run from a terminal pointed at a local RomM instance:

```bash
python cli/romm-install-cli.py \
    --base http://localhost:5100 \
    --user admin --pass admin \
    --rom-id 123 \
    --out /tmp/romm-install
```

What it does (and how it maps to this document):

1. Logs in over HTTP Basic and captures the session cookie.
2. If a session for this ROM is already `done`, skips straight to streaming - the server re-serves the cached install immediately.
3. Otherwise it prints the detected installer candidates (`GET /{id}/install/candidates`, which works whether or not a worker is connected - it's a plain filesystem scan) and starts the session (`POST /{id}/install`). Omit `--installer-path` to let the server auto-pick; pass it only to override. A transient "worker not connected yet" (503) is retried for ~50s instead of failing outright, same as the web UI's own "Install" button - manual mode itself never needs the worker at all.
4. If the server can't confidently auto-pick an installer, the session comes back `awaiting_installer` (the "manual mode" branch): the CLI prints `manual_install_url` and exits. Open that URL to finish the install in the browser-based VNC session, then re-run the same command to stream the result.
5. While polling the session state (`GET /{id}/install`), it concurrently streams files (`GET /{id}/install/stream/manifest` and Range `GET /{id}/install/stream/{path}`) into `--out/<game name>/` - so it can start pulling bytes before the install finishes, which is the point of stream install.
6. Supports a few extra one-off operations: `--cancel` to abort a session, `--clear` to wipe its cache, `--list-proton`/`--download-proton` to inspect and pull Proton builds. See `--help` for the full set.

Pass `--no-download` to start and poll a session without streaming files. The CLI assumes the RomM instance already exposes the install endpoints; it does not stand up the sandbox container or worker itself.
