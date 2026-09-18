/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Whether an install-sandbox worker is currently connected.
 *
 * Drives whether the client offers the "Install" action at all - there's no
 * separate on/off setting, availability is purely "is a worker listening
 * right now" (see handler.install.queue_status.has_install_worker).
 */
export type InstallWorkerStatusSchema = {
    available: boolean;
};

