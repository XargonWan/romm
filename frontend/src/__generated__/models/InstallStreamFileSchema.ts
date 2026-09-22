/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * One file's live delivery state - how much exists, how much of that
 * is safe to download right now, and whether it's actually done (see
 * handler.install.manifest.scan_live_manifest for what "safe" means here -
 * a stable-for-one-scan prefix, not a hash-verified one; the install's
 * own single whole-file sha1 is what actually gets verified, once).
 */
export type InstallStreamFileSchema = {
    path: string;
    size_bytes: number;
    sealed_bytes: number;
    complete: boolean;
};

