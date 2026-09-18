/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * One file's live delivery state - how much exists, how much of that
 * is hash-verified and safe to download, and whether it's actually done.
 */
export type InstallStreamFileSchema = {
    path: string;
    size_bytes: number;
    sealed_bytes: number;
    complete: boolean;
};

