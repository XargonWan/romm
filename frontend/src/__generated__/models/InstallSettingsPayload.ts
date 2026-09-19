/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Global stream-install settings.
 *
 * ``download_speed_limit_bytes_per_sec`` is a single server-wide cap shared
 * by every concurrent install download (see handler.install.bandwidth), not
 * a per-game setting - ``None``/``0`` means unlimited.
 *
 * ``default_proton_build`` is the default Proton build id (e.g.
 * ``"cachyos-latest"``) used for new Windows install sessions when the user
 * hasn't explicitly chosen one. ``None`` falls back to the first build
 * discovered on disk.
 */
export type InstallSettingsPayload = {
    download_speed_limit_bytes_per_sec?: (number | null);
    default_proton_build?: (string | null);
};
