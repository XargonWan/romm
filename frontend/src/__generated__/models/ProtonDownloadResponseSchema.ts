/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Response to POST /install/proton/{build_id}/download — the RQ job id
 * to poll for completion via GET /install/proton/{build_id}/progress.
 */
export type ProtonDownloadResponseSchema = {
    job_id: string;
};

