/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { InstallStreamFileSchema } from './InstallStreamFileSchema';
/**
 * Polled by the Install page regardless of session state - entries come
 * from the best-effort live manifest while installing, or are synthesized
 * from the real one once DONE (see handler.install.manifest).
 */
export type InstallStreamManifestSchema = {
    rom_id: number;
    files: Array<InstallStreamFileSchema>;
    viewer_count: number;
    download_speed_limit_bytes_per_sec?: (number | null);
};

