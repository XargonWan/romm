// useInstallSession — state machine backing the "Install" / "Installing…"
// button on Windows ROMs (GameDetails' GameActions row) and the full-page
// /rom/:id/install view.
//
// Lifecycle: idle -> starting -> polling (DETECTING/AWAITING_INSTALLER/
// INSTALLING/STREAMING) -> settled (DONE/FAILED/EXPIRED, or no session at
// all). The VNC viewport on the Install page only ever renders while
// polling sees state === "installing" (vnc_url set).
//
// Deliberately scoped to a single ROM view, not `useGameActions`: install
// state is asynchronous and polled, which would be wasteful re-instantiated
// per card in a virtualised gallery grid (see useGameActions' own comment on
// why it stays cheap-per-card). Mounted once by GameActions' InstallButton
// (for the ribbon control) and once by the Install page (for the full
// controls) - each gets its own independent poll loop.
import { computed, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { Router } from "vue-router";
import type {
  InstallCandidateSchema,
  InstallSessionSchema,
  InstallSessionState,
  ProtonBuildSchema,
} from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import installApi from "@/services/api/install";
import storeAuth from "@/stores/auth";
import type { SimpleRom } from "@/stores/roms";
import { useConfirm } from "@/v2/composables/useConfirm";
import { useSnackbar } from "@/v2/composables/useSnackbar";

// Extended ProtonBuild shape with runtime-discovered fields that haven't made
// it into the generated @/__generated__ types yet (version, source, size, etc.).
// Casts from the raw API response — the backend returns these fields, the
// generated types just haven't been regenerated.
export type ProtonBuildExtended = ProtonBuildSchema & {
  version?: string | null;
  path?: string | null;
  source?: string;
  size_bytes?: number | null;
};

// A candidate at this rank was matched by a well-known installer file name
// (gog-*.exe, setup.exe, ...) — confident enough to start without asking.
// Kept in sync with RANK_KNOWN_INSTALLER in
// backend/handler/filesystem/installer_detection.py.
const RANK_KNOWN_INSTALLER = 0;

const RUNNING_STATES: InstallSessionState[] = ["installing", "streaming"];
const ACTIVE_STATES: InstallSessionState[] = [
  "detecting",
  "awaiting_installer",
  ...RUNNING_STATES,
];

const POLL_INTERVAL_MS = 3000;

// The backend's error detail (e.g. "No install worker is currently
// connected...") is far more useful than a generic "failed" toast — surface
// it when axios gives us one, same fallback chain used elsewhere in the app.
function errorDetail(err: unknown): string {
  const e = err as {
    response?: { data?: { detail?: string }; statusText?: string };
    message?: string;
  };
  return (
    e?.response?.data?.detail ||
    e?.response?.statusText ||
    e?.message ||
    "unknown error"
  );
}

// A freshly (re)started install-sandbox worker takes real, bounded time to
// come up - Wine/Proton warmup then Redis registration (see
// docker/init_scripts/install-sandbox-entrypoint.sh) - before it's visible to
// has_install_worker(). Visiting /install (or clicking "Install") right as
// the stack comes up used to hard-fail on that transient window (503 "No
// install worker is currently connected…") instead of riding it out. ~50s
// total, loosely matching the worker's own up-to-90s prefix-warmup budget.
const WORKER_STARTUP_RETRY_DELAYS_MS = [
  2000, 3000, 5000, 5000, 5000, 10000, 10000, 10000,
];

function isWorkerUnavailable(err: unknown): boolean {
  return (err as { response?: { status?: number } })?.response?.status === 503;
}

/** Retry `fn` while it fails specifically with "worker not connected yet",
 *  waiting between attempts; any other error (or exhausting the retry
 *  budget) rethrows immediately. `onWaiting` toggles around each wait so
 *  the caller can show a distinct "starting up…" state instead of a bare
 *  spinner. `shouldStop` is checked before every retry so an unmounted
 *  caller doesn't keep scheduling timers (same idiom as the poll loops
 *  below). */
async function withWorkerStartupRetry<T>(
  fn: () => Promise<T>,
  {
    onWaiting,
    shouldStop,
  }: { onWaiting?: (w: boolean) => void; shouldStop?: () => boolean } = {},
): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try {
      const result = await fn();
      onWaiting?.(false);
      return result;
    } catch (err) {
      const delay = WORKER_STARTUP_RETRY_DELAYS_MS[attempt];
      if (!isWorkerUnavailable(err) || delay === undefined || shouldStop?.()) {
        onWaiting?.(false);
        throw err;
      }
      onWaiting?.(true);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
}

export function useInstallSession(getRom: () => SimpleRom | null | undefined) {
  const { t } = useI18n();
  const snackbar = useSnackbar();
  const confirm = useConfirm();
  const auth = storeAuth();

  const session = ref<InstallSessionSchema | null>(null);
  const candidates = ref<InstallCandidateSchema[]>([]);
  const protonBuilds = ref<ProtonBuildExtended[]>([]);
  const streamCopy = ref(false);
  const checking = ref(false);
  const starting = ref(false);
  const cancelling = ref(false);
  // Track in-progress Proton builds by id for live progress display.
  const downloadingBuilds = ref<Record<string, number>>({});
  // Whether an install-sandbox worker is connected right now - there's no
  // static setting, this is the sole signal for offering "Install" at all.
  // `null` = not checked yet (assume no, matches the fail-closed default).
  const workerAvailable = ref<boolean | null>(null);
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let downloadPollTimer: ReturnType<typeof setTimeout> | null = null;
  // schedulePoll() runs inside an async `finally`, so a plain "clear
  // whatever timer id we're tracking" on unmount can miss a call that's
  // already in flight - it resolves anyway and reschedules regardless.
  // Checked right before every reschedule so an in-flight call becomes a
  // no-op once the consumer has unmounted.
  let stopped = false;

  const canInstall = computed(() => auth.scopes.includes("roms.install"));

  const state = computed<InstallSessionState | null>(
    () => session.value?.state ?? null,
  );
  const isRunning = computed(
    () => !!state.value && RUNNING_STATES.includes(state.value),
  );
  const isActive = computed(
    () => !!state.value && ACTIVE_STATES.includes(state.value),
  );
  // "View install" only makes sense while the sandbox's VNC bridge is up.
  const vncUrl = computed(() =>
    state.value === "installing" ? session.value?.vnc_url : null,
  );
  const hasCache = computed(() => !!session.value && !isRunning.value);

  // While the worker is bootstrapping (downloading/installing the Proton
  // build before the VNC bridge comes up), poll its download progress so the
  // Install page can show "Downloading Proton X… xx%" instead of a bare
  // spinner with no context.
  const protonDownloadProgress = ref<number | null>(null);
  const protonDownloadLabel = ref<string | null>(null);
  // Whether the build is in the extraction phase (download complete, tarball
  // being unpacked). The frontend shows "Installing Proton…" in this state.
  const protonExtracting = ref(false);
  // True while startWithPath is retrying through a transient "worker not
  // connected yet" 503 (see withWorkerStartupRetry) - lets the Install page
  // show "waiting for the install worker to start…" instead of a bare
  // spinner during that window.
  const waitingForWorker = ref(false);

  function stopPolling() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedulePoll() {
    stopPolling();
    if (stopped) return;
    pollTimer = setTimeout(refreshSession, POLL_INTERVAL_MS);
  }

  async function refreshSession() {
    const rom = getRom();
    if (!rom) return;
    try {
      const { data } = await installApi.getInstallSession(rom.id);
      session.value = data;

      // While the worker is bootstrapping (state=installing, no VNC URL yet),
      // the Proton build it picked may still be downloading. Poll the
      // download-progress endpoint so the UI can show a percentage instead of
      // a bare spinner. Once the VNC bridge is up, stop polling.
      if (
        data.state === "installing" &&
        data.vnc_url === null &&
        data.proton_build
      ) {
        pollProtonDownload(data.proton_build);
      } else {
        protonDownloadProgress.value = null;
        protonDownloadLabel.value = null;
      }
    } catch (err) {
      // Only a real 404 means "no session for this rom/user" - anything
      // else (a dropped connection, a 5xx, the backend mid-restart) is
      // transient and must NOT be treated as "the install vanished": that
      // was closing the VNC overlay and flipping the button back to
      // "Install" on a single missed poll, while the session (and the
      // install itself) was still very much there server-side.
      const status = (err as { response?: { status?: number } })?.response
        ?.status;
      if (status === 404) {
        session.value = null;
      } else {
        // Keep the stale-but-correct session and just try again next tick
        // instead of stopping the poll loop outright.
        schedulePoll();
        return;
      }
    }
    if (session.value && ACTIVE_STATES.includes(session.value.state)) {
      schedulePoll();
    }
  }

  /** Call once when the button mounts, to resume polling an install already
   *  in flight (e.g. the user navigated away and came back). */
  async function checkExisting() {
    const rom = getRom();
    if (!rom || !canInstall.value) return;
    checking.value = true;
    try {
      await refreshSession();
    } finally {
      checking.value = false;
    }
  }

  /** Call once when the button mounts (Windows ROMs only - see GameActions),
   *  to decide whether "Install" is even offered. One cheap Redis lookup per
   *  page view, not polled continuously. */
  async function checkWorkerAvailable() {
    if (!canInstall.value) return;
    try {
      const { data } = await installApi.getInstallWorkerStatus();
      workerAvailable.value = data.available;
    } catch {
      workerAvailable.value = false;
    }
  }

  async function startWithPath(installerPath?: string, protonBuild?: string) {
    const rom = getRom();
    if (!rom) return;
    starting.value = true;
    try {
      const { data } = await withWorkerStartupRetry(
        () =>
          installApi.startInstall({
            romId: rom.id,
            installerPath,
            protonBuild,
          }),
        {
          onWaiting: (w) => (waitingForWorker.value = w),
          shouldStop: () => stopped,
        },
      );
      session.value = data;
      if (ACTIVE_STATES.includes(data.state)) schedulePoll();
    } catch (err) {
      snackbar.error(
        t("rom.install-snackbar-start-failed", { detail: errorDetail(err) }),
        { icon: "mdi-alert-circle-outline" },
      );
    } finally {
      starting.value = false;
    }
  }

  /** Fetch installer candidates without deciding anything - populates
   *  `candidates`/`streamCopy` for the Install page's file-select combo. */
  async function checkCandidates() {
    const rom = getRom();
    if (!rom || !canInstall.value) return;
    try {
      const { data } = await installApi.getInstallCandidates(rom.id);
      candidates.value = data.candidates;
      streamCopy.value = data.stream_copy;
    } catch {
      // Best-effort: the sidebar combo just stays empty.
    }
  }

  /** Proton builds this server knows about, for the version-picker combo.
   *  One cheap lookup per page view, not polled continuously. */
  async function fetchProtonBuilds() {
    if (!canInstall.value) return;
    try {
      const { data } = await installApi.getProtonBuilds();
      protonBuilds.value = data.builds as ProtonBuildExtended[];
    } catch {
      protonBuilds.value = [];
    }
  }

  /** Enqueue a Proton build download on the install worker, then start polling
   *  for progress. Re-fetches the build list when done so newly-installed
   *  builds appear in the picker. */
  async function downloadProtonBuild(buildId: string) {
    downloadingBuilds.value[buildId] = 0;
    try {
      await installApi.downloadProtonBuild(buildId);
      scheduleDownloadPoll(buildId);
    } catch (err) {
      const detail = errorDetail(err);
      snackbar.error(
        t("rom.install-snackbar-proton-download-failed", {
          name: buildId,
          detail,
        }),
        { icon: "mdi-alert-circle-outline" },
      );
      delete downloadingBuilds.value[buildId];
    }
  }

  const DOWNLOAD_POLL_INTERVAL_MS = 2000;

  function scheduleDownloadPoll(buildId: string) {
    if (downloadPollTimer !== null) clearTimeout(downloadPollTimer);
    if (stopped) return;
    downloadPollTimer = setTimeout(
      () => pollDownloadProgress(buildId),
      DOWNLOAD_POLL_INTERVAL_MS,
    );
  }

  async function pollDownloadProgress(buildId: string) {
    try {
      const { data } = await installApi.getProtonDownloadProgress(buildId);
      if (data.progress === null) {
        // Download finished or not running — refresh the build list so the
        // newly-installed build appears in the picker.
        downloadPollTimer = null;
        await fetchProtonBuilds();
        delete downloadingBuilds.value[buildId];
      } else {
        downloadingBuilds.value[buildId] = data.progress;
        scheduleDownloadPoll(buildId);
      }
    } catch {
      downloadPollTimer = null;
      delete downloadingBuilds.value[buildId];
    }
  }

  async function pollProtonDownload(buildId: string) {
    if (!canInstall.value) return;
    try {
      const { data } = await installApi.getProtonDownloadProgress(buildId);
      // Resolve a display label for the build.
      const build = protonBuilds.value.find((b) => b.id === buildId);
      protonDownloadLabel.value = build?.label ?? buildId;

      if (data.extracting) {
        // Tarball downloaded, extraction in progress — show "Installing…".
        protonExtracting.value = true;
        protonDownloadProgress.value = null;
      } else if (data.progress === null) {
        // Download complete and not extracting — build is ready.
        protonExtracting.value = false;
        protonDownloadProgress.value = null;
        fetchProtonBuilds();
      } else {
        protonExtracting.value = false;
        protonDownloadProgress.value = data.progress;
      }
    } catch {
      // Worker might not have started the download yet — leave the last
      // known state and try again on the next session poll.
    }
  }

  /** Abort a running install (any active state, not just once the VNC bridge
   *  is up) and clear whatever partial cache it left. Destructive and
   *  touches the filesystem, so it goes through a typed-confirm gate. */
  async function cancelInstall() {
    const rom = getRom();
    if (!rom) return;
    const ok = await confirm({
      title: t("rom.install-confirm-abort-title"),
      body: t("rom.install-confirm-abort-body"),
      confirmText: t("rom.install-abort"),
      tone: "danger",
      requireTyped: "DELETE",
    });
    if (!ok) return;

    cancelling.value = true;
    try {
      const { data } = await installApi.cancelInstall(rom.id);
      session.value = data;
      stopPolling();
      snackbar.success(t("rom.install-snackbar-aborted"), {
        icon: "mdi-check-bold",
      });
    } catch (err) {
      snackbar.error(
        t("rom.install-snackbar-abort-failed", { detail: errorDetail(err) }),
        { icon: "mdi-alert-circle-outline" },
      );
    } finally {
      cancelling.value = false;
    }
  }

  onBeforeUnmount(() => {
    stopped = true;
    stopPolling();
    if (downloadPollTimer !== null) clearTimeout(downloadPollTimer);
  });

  return {
    canInstall,
    workerAvailable,
    session,
    candidates,
    protonBuilds,
    downloadingBuilds,
    streamCopy,
    checking,
    starting,
    cancelling,
    state,
    isRunning,
    isActive,
    vncUrl,
    hasCache,
    protonDownloadProgress,
    protonDownloadLabel,
    protonExtracting,
    waitingForWorker,
    checkExisting,
    checkWorkerAvailable,
    checkCandidates,
    fetchProtonBuilds,
    downloadProtonBuild,
    startWithPath,
    cancelInstall,
  };
}

export type InstallSession = ReturnType<typeof useInstallSession>;

/** Navigate to the Install page and, in parallel (not sequentially), kick
 *  off the actual install request - so the sandbox/VNC startup (the slow
 *  part) begins the instant the click happens, not after the page has
 *  finished mounting. Deliberately a plain function, not part of
 *  useInstallSession's returned object: the caller (e.g. GameActions) is
 *  about to unmount as the route changes, and this must keep running after
 *  that. The Install page's own useInstallSession instance picks up the
 *  resulting session via its normal checkExisting() polling.
 *
 *  Ambiguous ROMs (no confident single candidate) are left alone here - the
 *  Install page's own file-select combo (backed by checkCandidates) lets
 *  the user pick and press "Install" there instead. */
export async function startInstallAndNavigate(
  rom: SimpleRom,
  router: Router,
): Promise<void> {
  void router.push({ name: ROUTES.INSTALL, params: { rom: rom.id } });
  try {
    // Retried as one unit (not just the final startInstall call): a worker
    // that's still booting 503s on the candidates fetch just as readily -
    // see withWorkerStartupRetry's own comment.
    await withWorkerStartupRetry(async () => {
      const { data } = await installApi.getInstallCandidates(rom.id);
      if (data.stream_copy) {
        await installApi.startInstall({ romId: rom.id });
        return;
      }
      const top = data.candidates[0];
      if (top && top.rank === RANK_KNOWN_INSTALLER) {
        await installApi.startInstall({
          romId: rom.id,
          installerPath: top.path,
        });
      }
    });
  } catch {
    // Best-effort: the Install page's own checkExisting()/checkCandidates()
    // still give the user a way to see what happened and retry.
  }
}
