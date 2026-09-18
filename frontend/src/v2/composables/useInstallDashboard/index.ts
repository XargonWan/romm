// useInstallDashboard — this user's install sessions worth surfacing on
// Home (still active, or finished with a cache still on disk). Backs the
// "Active Installers" widget, which only renders while `entries` is
// non-empty. A single GET, no polling: Home isn't the place to watch an
// install progress live (GameDetails' InstallButton/VNC overlay does that);
// this just needs to be roughly right when the page loads.
import { ref } from "vue";
import type { InstallDashboardEntrySchema } from "@/__generated__";
import installApi from "@/services/api/install";
import storeAuth from "@/stores/auth";

export function useInstallDashboard() {
  const auth = storeAuth();
  const entries = ref<InstallDashboardEntrySchema[]>([]);
  const loading = ref(false);

  async function refresh() {
    if (!auth.scopes.includes("roms.install")) return;
    loading.value = true;
    try {
      const { data } = await installApi.getInstallDashboard();
      entries.value = data.entries;
    } catch {
      entries.value = [];
    } finally {
      loading.value = false;
    }
  }

  return { entries, loading, refresh };
}
