import type {
  InstallCandidatesSchema,
  InstallDashboardSchema,
  InstallFilesSchema,
  InstallSessionSchema,
  InstallStreamManifestSchema,
  InstallWorkerStatusSchema,
  ProtonBuildsSchema,
} from "@/__generated__";
import api from "@/services/api";

export const installApi = api;

async function getInstallCandidates(romId: number) {
  return api.get<InstallCandidatesSchema>(`/roms/${romId}/install/candidates`);
}

async function startInstall({
  romId,
  installerPath,
  protonBuild,
  ttlSeconds,
}: {
  romId: number;
  installerPath?: string;
  protonBuild?: string;
  ttlSeconds?: number;
}) {
  return api.post<InstallSessionSchema>(`/roms/${romId}/install`, {
    installer_path: installerPath ?? null,
    proton_build: protonBuild ?? null,
    ttl_seconds: ttlSeconds ?? null,
  });
}

async function getInstallSession(romId: number) {
  return api.get<InstallSessionSchema>(`/roms/${romId}/install`);
}

async function clearInstallCache(romId: number) {
  return api.delete(`/roms/${romId}/install`);
}

async function cancelInstall(romId: number) {
  return api.post<InstallSessionSchema>(`/roms/${romId}/install/cancel`);
}

/** Whether an install-sandbox worker is connected right now - drives whether
 *  the client offers "Install" at all. No on/off setting to check instead. */
async function getInstallWorkerStatus() {
  return api.get<InstallWorkerStatusSchema>("/roms/install/worker-status");
}

async function getInstallFiles(romId: number) {
  return api.get<InstallFilesSchema>(`/roms/${romId}/install/files`);
}

/** Proton builds the server knows about - only one is ever actually
 *  installed today; the rest are listed for a (currently disabled)
 *  "download other versions" affordance. */
async function getProtonBuilds() {
  return api.get<ProtonBuildsSchema>("/roms/install/proton-builds");
}

/** This user's install sessions worth surfacing on Home (still active, or
 *  finished with a cache still on disk). Backs the "Active Installers"
 *  widget, which only renders when this list is non-empty. */
async function getInstallDashboard() {
  return api.get<InstallDashboardSchema>("/roms/install/dashboard");
}

/** Relative download path for one installed file, ready for a plain `<a href>`. */
function getInstallFileDownloadPath(romId: number, filePath: string): string {
  return `/api/roms/${romId}/install/files/${encodeURI(filePath)}`;
}

/** Live view of an install's output (sealed/complete per file, viewer count,
 *  the shared bandwidth cap) - one endpoint regardless of whether the
 *  session is still running or already finished. */
async function getInstallStreamManifest(romId: number) {
  return api.get<InstallStreamManifestSchema>(
    `/roms/${romId}/install/stream/manifest`,
  );
}

/** Relative download path for one file that may still be installing - a
 *  plain `<a href>`/browser download against this Range-resumes as more of
 *  the file seals, the same way it would for an already-finished one. */
function getInstallStreamFileDownloadPath(
  romId: number,
  filePath: string,
): string {
  return `/api/roms/${romId}/install/stream/${encodeURI(filePath)}`;
}

export default {
  getInstallCandidates,
  startInstall,
  getInstallSession,
  clearInstallCache,
  cancelInstall,
  getInstallWorkerStatus,
  getInstallDashboard,
  getInstallFiles,
  getInstallFileDownloadPath,
  getInstallStreamManifest,
  getInstallStreamFileDownloadPath,
  getProtonBuilds,
};
