#!/bin/sh
# Entrypoint for the remote-install sandbox worker (see
# docker/Dockerfile.install-sandbox and docker-compose.yml's
# romm-install-sandbox service).
set -eu

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
if [ -n "${INSTALL_PROTON_PATH:-}" ]; then
    STEAM_COMPAT_DATA_PATH="$warmup_prefix" \
        STEAM_COMPAT_CLIENT_INSTALL_PATH="$warmup_prefix" \
        "$INSTALL_PROTON_PATH" run wineboot --init >/dev/null 2>&1 || true
    wineserver_bin="$(dirname "$INSTALL_PROTON_PATH")/files/bin/wineserver"
    wineprefix_for_kill="$warmup_prefix/pfx"
else
    WINEPREFIX="$warmup_prefix" wine wineboot --init >/dev/null 2>&1 || true
    wineserver_bin="wineserver"
    wineprefix_for_kill="$warmup_prefix"
fi
# wineboot --init returns while wineserver keeps running in the background,
# still writing to the prefix; without waiting for it to exit, `rm -rf` below
# can race it and fail with "Directory not empty".
WINEPREFIX="$wineprefix_for_kill" "$wineserver_bin" -k -w >/dev/null 2>&1 || true
rm -rf "$warmup_prefix" || true

redis_url="$(uv run --no-sync python3 -c 'from config import REDIS_URL; print(REDIS_URL)')"
exec uv run --no-sync rq worker install \
    --path /src/backend \
    --worker-class handler.rq_worker.RomMWorker \
    --url "$redis_url"
