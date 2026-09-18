# Corrective Plan: Multi-Version Proton Management

## Status: Planning (no code changes)

## 1. Root Cause of the `/opt/proton` Error

The error `bwrap: Can't find source path /opt/proton: No such file or directory` comes from `runner.py:491-501` (`_wrap_for_sandbox`):

```python
ro_binds = tuple(
    {
        str(Path(path).resolve().parent)
        for path in (INSTALL_PROTON_PATH, INSTALL_PROTON_CACHYOS_PATH)
        if path
    }
)
```

This takes the **parent directory** of whatever `INSTALL_PROTON_PATH` / `INSTALL_PROTON_CACHYOS_PATH` point at and bind-mounts it read-only into the sandbox. The Docker image hardcodes:
- `INSTALL_PROTON_PATH=/opt/proton-ge/proton` -> parent binds `/opt/proton-ge`
- `INSTALL_PROTON_CACHYOS_PATH=/opt/proton-cachyos/proton` -> parent binds `/opt/proton-cachyos`

The error path `/opt/proton` not existing means the **runtime env vars don't match the baked-in paths** — either someone overrode `INSTALL_PROTON_PATH` to `/opt/proton/proton` (parent `/opt/proton`, which doesn't exist in the image), or the env wasn't propagated correctly to the worker container. The `resolve_proton_path` fallback silently returns `None` and falls back to `INSTALL_PROTON_PATH`, which itself was wrong, so bwrap gets a non-existent directory and the entire prefix init (wineboot --init) dies, producing exactly the log output seen.

### Why this is systemic, not a one-off misconfig

The current design couples three independently-fallible things:

1. **Build identity is a string key in a static dict** (`_BUILD_PATHS` in `proton_builds.py:41-44`) that is only correct *if* the env vars match what the image baked in. There is no runtime discovery — `resolve_proton_path` trusts the env var blindly and `_wrap_for_sandbox` trusts the env var blindly, with no cross-check that the path actually exists on disk.

2. **The bwrap bind list is constructed from env vars** (`runner.py:495-501`), not from the resolved build. So if the session picked `cachyos-latest` but `INSTALL_PROTON_PATH` (the GE env) is misconfigured, *both* directories get attempted as ro-binds, and the broken one kills the whole command even though the build actually in use exists.

3. **The frontend API contract is static** (`get_proton_builds` endpoint returns whatever `KNOWN_PROTON_BUILDS` hardcodes), so there's no way to surface "this build is installed on disk" vs "this build exists as an env var" vs "this build is downloadable". The `installed` flag is compile-time, not runtime.

## 2. Current State Summary

| Layer | File | How it works today |
|-------|------|--------------------|
| **Config** | `config/__init__.py:89-90` | Two env vars: `INSTALL_PROTON_PATH`, `INSTALL_PROTON_CACHYOS_PATH` |
| **Registry** | `handler/install/proton_builds.py` | Static `KNOWN_PROTON_BUILDS` tuple + `_BUILD_PATHS` dict; `resolve_proton_path(build_id)` looks up by id |
| **Runner** | `runner.py:101-110` | `_wine_or_proton()` calls `resolve_proton_path` or falls back to `INSTALL_PROTON_PATH` or `"wine"` |
| **Sandbox** | `runner.py:479-510` | `_wrap_for_sandbox()` builds bwrap ro-binds from both env-var paths, not from the resolved build |
| **Entry** | `runner.py:269` | `proton_or_wine = _wine_or_proton(session.proton_build)` |
| **Session model** | `models/install_session.py:93` | `proton_build: str | None` column on the session |
| **API** | `endpoints/roms/install.py:333-350` | `GET /install/proton-builds` returns static list; `POST /install` accepts `proton_build` in body |
| **Frontend** | `Install.vue:316-348` | RSelect combobox, disabled button for "download other versions" (hardcoded disabled) |
| **Docker image** | `Dockerfile.install-sandbox:122-139` | Both Protons downloaded at **build time** via ARG/curl, baked into `/opt/proton-ge/` and `/opt/proton-cachyos/` |
| **Entrypoint** | `install-sandbox-entrypoint.sh:20-23` | Warms up whichever `INSTALL_PROTON_PATH` points at |

## 3. Corrective Plan

### Phase 1: Runtime-discovered Proton registry (fixes the immediate crash + enables dynamic management)

**Goal**: Decouple build identity from env vars. Build a `ProtonBuildManager` that discovers what's *actually on disk* and what's *actually available to download*, rather than trusting static config.

#### 3.1 Proton build directory layout

Introduce a standard Proton install directory, e.g. `/opt/proton/` (replacing the scattered `/opt/proton-ge/` and `/opt/proton-cachyos/`):

```
/opt/proton/
├── proton-ge/
│   ├── GE-Proton10-34/
│   │   └── proton         (executable, launch script)
│   └── GE-Proton9-27/
├── cachyos/
│   ├── latest/
│   │   └── proton
│   └── Proton-CachyOS-42-1/
│       └── proton
└── wine/                  (plain Wine fallback, no "proton" launcher)
    └── bin/wine
```

The manager enumerates subdirectories, validates each by checking for an executable `proton` (or `wine`) binary, and returns a live list. This eliminates the class of bug where an env var points at a path that doesn't exist.

#### 3.2 `ProtonBuildManager` — replaces the static module

Replace `handler/install/proton_builds.py` with a `ProtonBuildManager` class (instantiated once per worker process) that:

- **Discovers installed builds** by scanning `PROTON_INSTALL_ROOT` (new config var, default `/opt/proton`) for subdirectories containing a `proton` or `wine` binary.
- **Lists downloadable builds** by querying upstream APIs (GE-Proton GitHub releases, Proton-CachyOS GitHub releases, ProtonUp-Qt's manifest, possibly Steam's Proton manifests).
- **Resolves a build id to a binary path** by looking it up in the discovered set — returning `None` if not installed (falling back to Wine), rather than trusting a potentially-stale env var.
- **Downloads a new build** by fetching the tarball from the release URL and extracting it into the right subdirectory under `PROTON_INSTALL_ROOT`.
- **Validates integrity** before marking a download as `installed` (check that `proton` exists and is executable).

The `ProtonBuild` dataclass gains a `source` field: `"baked"`, `"runtime-downloaded"`, or `"host-mounted"` — so the API and UI can distinguish what's available.

The `installed` flag becomes **runtime-derived**, not compile-time: a build is `installed=True` only if its binary actually exists on disk right now.

#### 3.3 Fix `_wrap_for_sandbox` to bind only what's in use

Change `runner.py:491-501` so that instead of binding both env-var paths statically, it binds **only the resolved build's own directory** (computed from `resolve_proton_path(session.proton_build)`):

```python
proton_or_wine = _wine_or_proton(session.proton_build)  # resolves via manager
# ... in _wrap_for_sandbox, bind only this build's parent directory
ro_binds = (Path(proton_or_wine).resolve().parent,) if Path(proton_or_wine).exists() else ()
```

This ensures bwrap only ever tries to bind paths that actually exist, eliminating the `Can't find source path` crash entirely.

### Phase 2: Remote download API (protonup-qt parity)

**Goal**: Let users download and install new Proton builds at runtime, like ProtonUp-Qt.

#### 2.1 API design

New endpoints (all under `/roms/install/proton`):

```
GET    /install/proton/builds
       — list all known builds (installed + downloadable), each with:
         id, label, installed (bool), version, source, download_url, size

POST   /install/proton/{build_id}/download
       — triggers a background download+extract for build_id
       — returns 202 with a task_id; the download runs as an RQ job on the
         install worker (it needs network + disk access in the sandbox image)
       — the worker image already has curl/git; this re-uses the same
         install_queue that run_install already uses

GET    /install/proton/{build_id}/progress
       — polls the download task's progress (bytes fetched / total)

DELETE /install/proton/{build_id}
       — removes a downloaded build (only for runtime-downloaded ones,
         never for baked-in builds)
```

#### 2.2 Download sources

- **GE-Proton**: GitHub releases API (`https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases`)
- **Proton-CachyOS**: GitHub releases API (`https://api.github.com/repos/CachyOS/proton-cachyos/releases`)
- **Upstream Steam Proton**: Could use Steam's CDN / ProtonUp-Qt's GitHub manifest as a fallback

Each source has a provider adapter in a new `handler/install/proton_sources/` package, so adding Valve's or other Proton forks is a drop-in module.

#### 2.3 Background task

The download runs as an RQ job enqueued on the same `install` queue the worker already consumes. The worker has network access (unlike the bwrap sandbox which is network-less), so the download job runs unconfined and only the final extraction is sanity-checked. After download completes, the manager re-scans `PROTON_INSTALL_ROOT` and the build becomes `installed=True`.

### Phase 3: Frontend integration

#### 3.1 Update `Install.vue` proton picker

- Replace the hardcoded-disabled "download other versions" button with a real affordance.
- When `installed=false` builds are listed, show a "Download" action per build (or a context menu).
- Show download progress inline (reuse the existing polling pattern from `pollStreamManifest`).
- Only allow selecting `installed=true` builds as the active Proton for an install session.

#### 3.2 Add a "Proton Management" settings section

A new admin section (mirroring the "Install Cache" management the user already asked for in TODO.md:48) where an admin can:
- See all installed Proton builds with disk usage
- Trigger downloads of new versions
- Remove downloaded builds (with confirmation)
- Set a default Proton build for new sessions

#### 3.3 API schema changes

Update `ProtonBuildSchema` (responses/install.py:48-56) to include:
- `version: str` — the actual Proton version string from the release
- `source: str` — `"baked" | "runtime" | "host"`
- `download_progress: float | None` — for in-progress downloads

Update `ProtonBuildsSchema` to optionally include `download_in_progress: bool`.

### Phase 4: Docker image changes

#### 4.1 Consolidate Proton locations

The `Dockerfile.install-sandbox` bakes GE-Proton and CachyOS into `/opt/proton-ge/` and `/opt/proton-cachyos/`. Change the build to extract both into `/opt/proton/ge/GE-Proton10-34/` and `/opt/proton/cachyos/latest/`, matching the new directory layout so the manager discovers them automatically.

This also fixes the glibc pinning comment issue: the manager's `source` field and `installed` runtime check mean we can safely list *all* known versions (including GE-Proton11-7) without baking them in — they show as `installed=false` and `source="upstream"` until downloaded.

#### 4.2 Optional Proton download volume

Since runtime downloads need persistent storage across worker restarts, add an optional named volume mounted at `/opt/proton` in the `docker-compose.install-worker.example.yml`, separate from the main install cache. This lets downloaded builds survive container rebuilds while baked-in builds live in the image layer.

## 4. Risk Mitigation

- **Backward compatibility**: The `install_sessions.proton_build` column stays a string id; existing sessions with `GE-Proton10-34` or `cachyos-latest` still resolve. The manager's id space must include the old ids as aliases.
- **Graceful degradation**: If the Proton directory is empty or missing, `resolve_proton_path` returns `None` and the runner falls back to plain Wine (`"wine"`), same as today — just without the bwrap crash.
- **Tests**: Existing `test_proton_builds.py` and `test_runner.py` patterns (monkeypatch the module, assert resolution) carry over to the new `ProtonBuildManager` — the manager's discovery and download functions are each unit-testable in isolation, matching the existing "keep handler.install pure and testable" convention (CLAUDE.md:61, BACKEND_ARCHITECTURE.md:13-17).
- **Bwrap hardens**: The core crash fix (only bind dirs that exist) is a one-line guard in `_wrap_for_sandbox` and can ship independently of the full manager.

## 5. Suggested Implementation Order

1. **Fix the crash** (P0): guard `_wrap_for_sandbox` to only bind directories that `Path(...).exists()` — this alone prevents the `Can't find source path` error and unblocks runs.
2. **Introduce `ProtonBuildManager`** with runtime disk discovery replacing the static `proton_builds.py` — keeps the same API shape for Phase 1.
3. **Add remote download endpoints + background task** — unblocks the "download other versions" UI.
4. **Update frontend** to expose the download UI and the management settings.
5. **Update Docker image + compose** to the new `/opt/proton/` layout and optional volume.

## 6. Files That Would Change (for reference)

| File | Change |
|------|--------|
| `handler/install/proton_builds.py` | Replaced by `ProtonBuildManager` class |
| `handler/install/runner.py` | `_wine_or_proton` + `_wrap_for_sandbox` use manager; `_init_wine_prefix` stays |
| `config/__init__.py` | Replace two `INSTALL_PROTON_*` env vars with `PROTON_INSTALL_ROOT` |
| `endpoints/roms/install.py` | New download/delete/progress endpoints; update `get_proton_builds` |
| `endpoints/responses/install.py` | Extend `ProtonBuildSchema` |
| `models/install_session.py` | No schema change (id stays a string) |
| `docker/Dockerfile.install-sandbox` | New directory layout, keep baked-in builds |
| `docker/init_scripts/install-sandbox-entrypoint.sh` | Warm up default build via manager |
| `frontend/src/services/api/install.ts` | New `downloadProtonBuild`, `deleteProtonBuild` |
| `frontend/src/v2/views/Player/Install.vue` | Real download affordance in the picker |
| `frontend/src/__generated__/` | Regenerated OpenAPI types |
| `docker-compose.install-worker.example.yml` | Optional Proton volume mount |