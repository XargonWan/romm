from __future__ import annotations

from pydantic import ConfigDict

from models.install_session import InstallSessionState

from .base import BaseModel, UTCDatetime


class InstallCandidateSchema(BaseModel):
    """A possible installer/entry file detected inside a ROM's directory."""

    # Path relative to the ROM's directory.
    path: str
    file_name: str
    file_size_bytes: int
    # Detection bucket, lower rank = higher priority (see installer_detection.py).
    rank: int
    # Human-readable reason this file was picked (e.g. "gog installer", "archive").
    kind: str


class InstallCandidatesSchema(BaseModel):
    rom_id: int
    candidates: list[InstallCandidateSchema]
    # True when nothing could be auto-detected and the client must prompt a picker.
    needs_manual_pick: bool
    # True when the ROM is not an installer and should be stream-copied directly.
    stream_copy: bool


class InstallFileSchema(BaseModel):
    """One file produced by a finished install, downloadable and verifiable."""

    # Path relative to the session's cache directory; also the path segment
    # used by GET /{id}/install/files/{path} to download it.
    path: str
    size_bytes: int
    sha1: str


class InstallFilesSchema(BaseModel):
    rom_id: int
    total_bytes: int
    files: list[InstallFileSchema]


class ProtonBuildSchema(BaseModel):
    """One Proton build the server knows about (see handler.install.proton_builds)."""

    id: str
    label: str
    # Whether this build's binary actually exists in the sandbox image - only
    # an installed build can be picked; others are listed for a (currently
    # disabled) "download" affordance.
    installed: bool


class ProtonBuildsSchema(BaseModel):
    builds: list[ProtonBuildSchema]


class InstallWorkerStatusSchema(BaseModel):
    """Whether an install-sandbox worker is currently connected.

    Drives whether the client offers the "Install" action at all - there's no
    separate on/off setting, availability is purely "is a worker listening
    right now" (see handler.install.queue_status.has_install_worker).
    """

    available: bool


class InstallSessionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rom_id: int
    user_id: int
    state: InstallSessionState
    installer_path: str | None = None
    proton_build: str | None = None
    expires_at: UTCDatetime | None = None
    vnc_url: str | None = None
    bytes_written: int
    bytes_total: int
    error: str | None = None
    created_at: UTCDatetime
    updated_at: UTCDatetime


class InstallDashboardEntrySchema(BaseModel):
    """One row for the Home "Active Installers" widget: a session plus just
    enough of its ROM to render and link to it."""

    session: InstallSessionSchema
    rom_id: int
    rom_name: str | None
    platform_slug: str
    path_cover_small: str | None


class InstallDashboardSchema(BaseModel):
    entries: list[InstallDashboardEntrySchema]


class InstallStreamFileSchema(BaseModel):
    """One file's live delivery state - how much exists, how much of that
    is hash-verified and safe to download, and whether it's actually done."""

    path: str
    size_bytes: int
    sealed_bytes: int
    complete: bool


class InstallStreamManifestSchema(BaseModel):
    """Polled by the Install page regardless of session state - entries come
    from the best-effort live manifest while installing, or are synthesized
    from the real one once DONE (see handler.install.manifest)."""

    rom_id: int
    files: list[InstallStreamFileSchema]
    # Other clients currently pulling this same session's files right now.
    viewer_count: int
    # The server-wide cap all of them share, or None when unlimited.
    download_speed_limit_bytes_per_sec: int | None = None
