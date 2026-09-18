"""Registry of Proton builds the install sandbox knows about.

Two builds are actually baked into the sandbox image (see
docker/Dockerfile.install-sandbox): GE-Proton10-34 (INSTALL_PROTON_PATH, the
server default when a session picks no build) and Proton-CachyOS's latest
release (INSTALL_PROTON_CACHYOS_PATH, selectable). Both, side by side, so an
installer that doesn't get along with one still has the other to try - see
docker/Dockerfile.install-sandbox's comment on why GE-Proton is pinned to the
10.x line specifically (glibc compatibility with the Debian bookworm base
image; its own actual latest release, GE-Proton11-7, needs a newer glibc
this image doesn't have). Everything else here is listed so the frontend has
something real to show in a "Proton version" picker and a (still disabled)
"download other versions" affordance, without promising a build that isn't
actually installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import INSTALL_PROTON_CACHYOS_PATH, INSTALL_PROTON_PATH


@dataclass(frozen=True, slots=True)
class ProtonBuild:
    """One Proton build this server knows about."""

    id: str
    label: str
    # Whether this build's binary actually exists in the sandbox image right
    # now. Only an installed build can be picked; others are listed purely
    # for the (disabled) "download" UI.
    installed: bool


# id -> the real binary path resolve_proton_path() returns for it. Kept
# separate from KNOWN_PROTON_BUILDS below since that's a static description
# of what this Dockerfile always bakes in, not a live "is the env var set
# right now" check (which would go stale in any process - tests included -
# that doesn't have the sandbox image's env).
_BUILD_PATHS: dict[str, str | None] = {
    "GE-Proton10-34": INSTALL_PROTON_PATH,
    "cachyos-latest": INSTALL_PROTON_CACHYOS_PATH,
}

KNOWN_PROTON_BUILDS: tuple[ProtonBuild, ...] = (
    ProtonBuild(id="GE-Proton10-34", label="GE-Proton 10-34", installed=True),
    ProtonBuild(id="cachyos-latest", label="Proton-CachyOS (latest)", installed=True),
    # The real latest upstream GE-Proton release - listed so the (disabled)
    # "download other versions" UI is honest about what exists, but not
    # installed: it needs glibc 2.38+, which this image's Debian bookworm
    # base (2.36) doesn't have.
    ProtonBuild(id="GE-Proton11-7", label="GE-Proton 11-7", installed=False),
)


def list_proton_builds() -> tuple[ProtonBuild, ...]:
    """Every build the server knows about, installed or not."""
    return KNOWN_PROTON_BUILDS


def resolve_proton_path(build_id: str | None) -> str | None:
    """The binary to run for a chosen build id.

    Returns None when the id is unset, unknown, or not actually installed -
    callers should fall back to the server default (INSTALL_PROTON_PATH)
    rather than fail the install over a stale/invalid choice.
    """
    if build_id is None:
        return None
    for build in KNOWN_PROTON_BUILDS:
        if build.id == build_id and build.installed:
            return _BUILD_PATHS.get(build_id)
    return None
