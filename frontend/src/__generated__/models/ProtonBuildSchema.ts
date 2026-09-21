/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * One Proton build the server knows about (see handler.install.proton_builds).
 *
 * Installed builds are discovered at runtime by the ProtonBuildManager scanning
 * PROTON_INSTALL_ROOT (or legacy env vars) for an executable binary. Non-installed
 * builds come from upstream release APIs (GE-Proton, Proton-CachyOS) and can be
 * downloaded at runtime via POST /install/proton/{id}/download.
 */
export type ProtonBuildSchema = {
    id: string;
    label: string;
    installed: boolean;
    version?: (string | null);
    path?: (string | null);
    source?: string;
    size_bytes?: (number | null);
};

