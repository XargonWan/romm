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
 */
export type InstallSettingsPayload = {
    download_speed_limit_bytes_per_sec?: (number | null);
};

