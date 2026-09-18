// Regression coverage for refreshSession's error handling: a transient
// polling failure (network blip, backend restart) must never be treated the
// same as "no install session for this rom" - only a real 404 means that.
// Conflating the two used to wipe the session on any hiccup, which closed
// the VNC overlay and reset the button to "Install" mid-run even though the
// install (and its session) were still very much alive server-side.
import { type VueWrapper, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent } from "vue";
import { createI18n } from "vue-i18n";
import installApi from "@/services/api/install";
import storeAuth from "@/stores/auth";
import type { SimpleRom } from "@/stores/roms";
import type { User } from "@/stores/users";
import { useInstallSession } from "./index";

// Minimal i18n instance: useInstallSession calls useI18n() (for snackbar/
// confirm copy elsewhere in the composable), which throws without an
// installed plugin even though refreshSession itself never touches it.
const i18n = createI18n({ legacy: false, locale: "en_US", messages: {} });

vi.mock("@/services/api/install", () => ({
  default: {
    getInstallSession: vi.fn(),
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
