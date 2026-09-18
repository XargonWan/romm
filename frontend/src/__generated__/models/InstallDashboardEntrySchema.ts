/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { InstallSessionSchema } from './InstallSessionSchema';
/**
 * One row for the Home "Active Installers" widget: a session plus just
 * enough of its ROM to render and link to it.
 */
export type InstallDashboardEntrySchema = {
    session: InstallSessionSchema;
    rom_id: number;
    rom_name: (string | null);
    platform_slug: string;
    path_cover_small: (string | null);
};

