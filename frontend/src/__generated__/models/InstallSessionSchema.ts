/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { InstallSessionState } from './InstallSessionState';
export type InstallSessionSchema = {
    id: number;
    rom_id: number;
    user_id: number;
    state: InstallSessionState;
    installer_path?: (string | null);
    proton_build?: (string | null);
    expires_at?: (string | null);
    vnc_url?: (string | null);
    bytes_written: number;
    bytes_total: number;
    error?: (string | null);
    created_at: string;
    updated_at: string;
    manual_install_url?: (string | null);
};

