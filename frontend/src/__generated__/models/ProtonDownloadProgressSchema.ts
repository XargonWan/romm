/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Progress of an in-flight Proton download/extract (0.0–1.0).
 *
 * ``progress`` is the download fraction while the tarball streams. Once the
 * download completes and extraction begins, ``extracting`` flips to true and
 * ``progress`` resets to 0.0 so the client can show an indeterminate
 * "Installing Proton…" spinner instead of a stale 100 %. None when no
 * download/extract is running for this build_id (either never started, or
 * finished and cleaned up).
 */
export type ProtonDownloadProgressSchema = {
    progress?: (number | null);
    extracting?: boolean;
};

