// Regression coverage for refreshSession's error handling: a transient
// polling failure (network blip, backend restart) must never be treated the
// same as "no install session for this rom" - only a real 404 means that.
// Conflating the two used to wipe the session on any hiccup, which closed
// the VNC overlay and reset the button to "Install" mid-run even though the
// install (and its session) were still very much alive server-side.
import { type VueWrapper, mount } from "@vue/test-utils";
import mitt, { type Emitter } from "mitt";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent } from "vue";
import { createI18n } from "vue-i18n";
import installApi from "@/services/api/install";
import storeAuth from "@/stores/auth";
import type { SimpleRom } from "@/stores/roms";
import type { Events } from "@/types/emitter";
import type { User } from "@/stores/users";
import { useInstallSession } from "./index";

// Minimal i18n instance: useInstallSession calls useI18n() (for snackbar/
// confirm copy elsewhere in the composable), which throws without an
// installed plugin even though refreshSession itself never touches it.
const i18n = createI18n({ legacy: false, locale: "en_US", messages: {} });

vi.mock("@/services/api/install", () => ({
  default: {
    getInstallSession: vi.fn(),
    clearInstallCache: vi.fn(),
    cancelInstall: vi.fn(),
  },
}));

const rom = { id: 42 } as SimpleRom;

let wrapper: VueWrapper | null = null;

function withComposable() {
  let result!: ReturnType<typeof useInstallSession>;
  wrapper = mount(
    defineComponent({
      setup() {
        result = useInstallSession(() => rom);
        return () => null;
      },
    }),
    { global: { plugins: [i18n] } },
  );
  return result;
}

// useConfirm() resolves `false` outright when no emitter is provided (see
// its own code) - fine for tests that never need a real confirm dialog, but
// confirmClearIfInstalled needs an actual round trip, so this variant wires
// up a real `mitt()` emitter and answers each successive `showConfirm` with
// the next value from `answers` (confirmClearIfInstalled shows up to two,
// in order: "keep vs. clear", then, only if clear was picked, the typed
// "Clear install cache?" confirmation).
function withComposableAndConfirm(...answers: boolean[]) {
  const emitter: Emitter<Events> = mitt();
  let next = 0;
  emitter.on("showConfirm", (payload) => {
    const confirmed = answers[next] ?? false;
    next += 1;
    emitter.emit("confirmResolved", { id: payload.id, confirmed });
  });
  let result!: ReturnType<typeof useInstallSession>;
  wrapper = mount(
    defineComponent({
      setup() {
        result = useInstallSession(() => rom);
        return () => null;
      },
    }),
    { global: { plugins: [i18n], provide: { emitter } } },
  );
  return result;
}

describe("useInstallSession refreshSession", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    storeAuth().user = {
      oauth_scopes: ["roms.install"],
    } as unknown as User;
    vi.mocked(installApi.getInstallSession).mockReset();
  });

  afterEach(() => {
    // Unmount so the composable's onBeforeUnmount stops any pending poll
    // timer scheduled by a test's second (error) response.
    wrapper?.unmount();
    wrapper = null;
  });

  it("keeps the session on a transient (non-404) polling error", async () => {
    const session = {
      id: 1,
      rom_id: rom.id,
      user_id: 1,
      state: "installing",
      vnc_url: "http://example.com/vnc.html",
      bytes_written: 0,
      bytes_total: 0,
      created_at: "",
      updated_at: "",
    };
    vi.mocked(installApi.getInstallSession)
      .mockResolvedValueOnce({ data: session } as never)
      .mockRejectedValueOnce({ response: { status: 500 } });

    const install = withComposable();
    await install.checkExisting();
    expect(install.session.value).toEqual(session);

    // Second poll: the backend hiccups (500) - refreshSession isn't
    // exported directly, so drive it through another checkExisting call,
    // which calls the same internal function. The session must survive.
    await install.checkExisting();
    expect(install.session.value).toEqual(session);
  });

  it("clears the session on a real 404", async () => {
    const session = {
      id: 1,
      rom_id: rom.id,
      user_id: 1,
      state: "installing",
      vnc_url: null,
      bytes_written: 0,
      bytes_total: 0,
      created_at: "",
      updated_at: "",
    };
    vi.mocked(installApi.getInstallSession)
      .mockResolvedValueOnce({ data: session } as never)
      .mockRejectedValueOnce({ response: { status: 404 } });

    const install = withComposable();
    await install.checkExisting();
    expect(install.session.value).toEqual(session);

    await install.checkExisting();
    expect(install.session.value).toBeNull();
  });
});

describe("useInstallSession hasCache", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    storeAuth().user = {
      oauth_scopes: ["roms.install"],
    } as unknown as User;
    vi.mocked(installApi.getInstallSession).mockReset();
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
  });

  // Regression guard: a session stuck in DETECTING (awaiting an installer
  // pick, never actually run) has nothing on disk to "reinstall" from - only
  // a session that actually ran and reached a terminal state does.
  it("is false while the session is still awaiting an installer pick", async () => {
    vi.mocked(installApi.getInstallSession).mockResolvedValueOnce({
      data: {
        id: 1,
        rom_id: rom.id,
        user_id: 1,
        state: "detecting",
        installer_path: null,
        vnc_url: null,
        bytes_written: 0,
        bytes_total: 0,
        created_at: "",
        updated_at: "",
      },
    } as never);

    const install = withComposable();
    await install.checkExisting();
    expect(install.hasCache.value).toBe(false);
  });

  it("is true once the session reaches a terminal state", async () => {
    vi.mocked(installApi.getInstallSession).mockResolvedValueOnce({
      data: {
        id: 1,
        rom_id: rom.id,
        user_id: 1,
        state: "done",
        vnc_url: null,
        bytes_written: 0,
        bytes_total: 0,
        created_at: "",
        updated_at: "",
      },
    } as never);

    const install = withComposable();
    await install.checkExisting();
    expect(install.hasCache.value).toBe(true);
  });
});

describe("useInstallSession confirmClearIfInstalled", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    storeAuth().user = {
      oauth_scopes: ["roms.install"],
    } as unknown as User;
    vi.mocked(installApi.getInstallSession).mockReset();
    vi.mocked(installApi.clearInstallCache).mockReset();
    vi.mocked(installApi.clearInstallCache).mockResolvedValue({} as never);
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
  });

  const doneSession = {
    id: 1,
    rom_id: rom.id,
    user_id: 1,
    state: "done",
    vnc_url: null,
    bytes_written: 0,
    bytes_total: 0,
    created_at: "",
    updated_at: "",
  };

  it("never asks and never clears when there's no cache to ask about", async () => {
    // No session at all - hasCache is false, nothing to confirm.
    const install = withComposableAndConfirm(true);
    await install.confirmClearIfInstalled();
    expect(installApi.clearInstallCache).not.toHaveBeenCalled();
  });

  it("clears the cache once the user picks Clear on both prompts", async () => {
    vi.mocked(installApi.getInstallSession).mockResolvedValueOnce({
      data: doneSession,
    } as never);
    // First prompt: "Clear install cache" is the cancel-slot button (see
    // confirmClearIfInstalled's own dangerSide usage), so answering false
    // picks it. Second prompt is the typed-DELETE one; true confirms it.
    const install = withComposableAndConfirm(false, true);
    await install.checkExisting();
    expect(install.hasCache.value).toBe(true);

    await install.confirmClearIfInstalled();

    expect(installApi.clearInstallCache).toHaveBeenCalledWith(rom.id);
    // The now-cleared session must not still read as "installed".
    expect(install.session.value).toBeNull();
  });

  it("leaves the cache alone when the user picks Keep on the first prompt", async () => {
    vi.mocked(installApi.getInstallSession).mockResolvedValueOnce({
      data: doneSession,
    } as never);
    // "Keep existing files" is the confirm-slot (default) button.
    const install = withComposableAndConfirm(true);
    await install.checkExisting();

    await install.confirmClearIfInstalled();

    expect(installApi.clearInstallCache).not.toHaveBeenCalled();
    // Declining doesn't wipe the still-valid existing session either.
    expect(install.session.value).toEqual(doneSession);
  });

  it("leaves the cache alone when the user backs out of the typed confirmation", async () => {
    vi.mocked(installApi.getInstallSession).mockResolvedValueOnce({
      data: doneSession,
    } as never);
    // Picks "Clear" on the first prompt, then declines the actual
    // typed-DELETE step - the destructive action never actually happens.
    const install = withComposableAndConfirm(false, false);
    await install.checkExisting();

    await install.confirmClearIfInstalled();

    expect(installApi.clearInstallCache).not.toHaveBeenCalled();
    // Declining doesn't wipe the still-valid existing session either.
    expect(install.session.value).toEqual(doneSession);
  });
});

describe("useInstallSession cancelInstall", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    storeAuth().user = {
      oauth_scopes: ["roms.install"],
    } as unknown as User;
    vi.mocked(installApi.cancelInstall).mockReset();
    vi.mocked(installApi.cancelInstall).mockResolvedValue({
      data: { id: 1, rom_id: rom.id, state: "failed" },
    } as never);
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
  });

  it("stops without clearing when the user picks Keep on the first prompt", async () => {
    // "Keep existing files" is the confirm-slot (default) button - same
    // dangerSide layout as confirmClearIfInstalled's own first prompt.
    const install = withComposableAndConfirm(true);

    await install.cancelInstall();

    expect(installApi.cancelInstall).toHaveBeenCalledWith(rom.id, {
      clearCache: false,
    });
  });

  it("clears the cache when the user picks Clear on both prompts", async () => {
    // First prompt: "Clear install cache" is the cancel-slot button, so
    // answering false picks it. Second prompt is the typed-DELETE one.
    const install = withComposableAndConfirm(false, true);

    await install.cancelInstall();

    expect(installApi.cancelInstall).toHaveBeenCalledWith(rom.id, {
      clearCache: true,
    });
  });

  it("does nothing when the user backs out of the typed confirmation", async () => {
    const install = withComposableAndConfirm(false, false);

    await install.cancelInstall();

    expect(installApi.cancelInstall).not.toHaveBeenCalled();
  });
});
