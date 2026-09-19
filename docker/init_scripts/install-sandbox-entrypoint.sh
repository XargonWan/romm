#!/bin/sh
# Entrypoint for the remote-install sandbox worker (see
# docker/Dockerfile.install-sandbox and docker-compose.yml's
# romm-install-sandbox service).
set -eu

# Ensure the unprivileged romm-install user can create download subdirectories
# under PROTON_INSTALL_ROOT. In production this is handled by the Dockerfile's
# chown; in dev overlays or unprivileged-userns hosts, chown on image layers
# can be blocked at runtime, so retry here as root (best-effort).
chown -R 10001:10001 "${PROTON_INSTALL_ROOT:-/opt/proton}" 2>/dev/null || true

# A fresh Wine/Proton prefix's first-ever bootstrap in a just-started
# container has been observed to fail outright ("could not load
# kernel32.dll") even when retried immediately; the same call reliably
# succeeds once the container's had a little real wall-clock time to settle
# (page cache warmed, wineserver past whatever it does on its first run).
# Rather than make every user's first real install pay for and risk that,
# absorb it once here, unsandboxed and display-less (this only primes Wine/
# Proton itself, it never touches a real installer) against a throwaway
# prefix, before the worker starts accepting jobs. Warms whichever runtime
# handler.install.runner will actually use (see its _wine_or_proton).
# Best-effort: if it still fails, the runner's own retries around the real
# prefix init cover it.
warmup_prefix="$(mktemp -d)"

# Find a Proton binary on disk to warm up (matches the manager's discovery
# logic in handler/install/proton_builds.py). Falls back to plain Wine if
# none found (e.g. fresh image with no baked-in Proton yet).
proton_bin="$(find "${PROTON_INSTALL_ROOT:-/opt/proton}" -maxdepth 2 -name proton -type f 2>/dev/null | head -1)"

if [ -n "$proton_bin" ] && [ -x "$proton_bin" ]; then
    STEAM_COMPAT_DATA_PATH="$warmup_prefix" \
        STEAM_COMPAT_CLIENT_INSTALL_PATH="$warmup_prefix" \
        timeout 90 "$proton_bin" run wineboot --init >/dev/null 2>&1 || true
    wineserver_bin="$(dirname "$proton_bin")/files/bin/wineserver"
    wineprefix_for_kill="$warmup_prefix/pfx"
else
    WINEPREFIX="$warmup_prefix" wine wineboot --init >/dev/null 2>&1 || true
    wineserver_bin="wineserver"
    wineprefix_for_kill="$warmup_prefix"
fi
# wineboot --init returns while wineserver keeps running in the background,
# still writing to the prefix; without waiting for it to exit, `rm -rf` below
# can race it and fail with "Directory not empty".
WINEPREFIX="$wineprefix_for_kill" timeout 30 "$wineserver_bin" -k -w >/dev/null 2>&1 || true
rm -rf "$warmup_prefix" || true

# Register every installed Proton build with Redis so the romm-dev API
# container (which doesn't share /opt/proton/ as a volume) can surface them
# via proton:worker:installed. The ProtonBuildManager reads this set in
# addition to its local disk scan.
redis_url="$(uv run --no-sync python3 -c 'from config import REDIS_URL; print(REDIS_URL)')"
uv run --no-sync python3 -c "
from handler.install.proton_builds import _discover_installed, _REDIS_INSTALLED_KEY
from handler.redis_handler import redis_client
builds = _discover_installed()
for build in builds:
    redis_client.sadd(_REDIS_INSTALLED_KEY, build.id)
print(f'Registered {len(builds)} Proton builds with Redis')
" 2>/dev/null || echo "Could not register Proton builds with Redis"

exec uv run --no-sync rq worker install \
    --path /src/backend \
    --worker-class handler.rq_worker.RomMWorker \
    --url "$redis_url"
