<script setup lang="ts">
// StreamInstallSection — the "Stream Install" tab in Library Management.
// One setting today: the global download-speed cap shared by every
// concurrent stream-install transfer (see handler.install.bandwidth on the
// backend) - not per-game, a single server-wide value, like a torrent
// client's global rate limit. Entered here in KB/s (friendlier than typing
// a raw bytes/sec number) and converted on save/load.
import { RBtn, RTextField } from "@v2/lib";
import { storeToRefs } from "pinia";
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import configApi from "@/services/api/config";
import storeAuth from "@/stores/auth";
import type { Config } from "@/stores/config";
import storeConfig from "@/stores/config";
import { useSnackbar } from "@/v2/composables/useSnackbar";
import SettingsSection from "./SettingsSection.vue";

const { t } = useI18n();
const snackbar = useSnackbar();
const authStore = storeAuth();
const configStore = storeConfig();
const { config } = storeToRefs(configStore);

const KB = 1024;

function configToKbPerSec(cfg: Config): number | null {
  const bytesPerSec = cfg.INSTALL_DOWNLOAD_SPEED_LIMIT_BYTES_PER_SEC;
  return bytesPerSec ? Math.round(bytesPerSec / KB) : null;
}

const kbPerSec = ref<number | null>(configToKbPerSec(config.value));
const savedSnapshot = ref(kbPerSec.value);

const dirty = computed(() => kbPerSec.value !== savedSnapshot.value);
const canEdit = computed(
  () =>
    authStore.scopes.includes("platforms.write") &&
    config.value.CONFIG_FILE_WRITABLE,
);

const loading = ref(true);
const saving = ref(false);

async function loadConfig() {
  loading.value = true;
  try {
    const cfg = await configStore.fetchConfig({ rethrow: true });
    kbPerSec.value = configToKbPerSec(cfg);
    savedSnapshot.value = kbPerSec.value;
  } catch {
    // Best-effort: the section still renders with whatever the store
    // already had (e.g. from a previous successful load).
  } finally {
    loading.value = false;
  }
}

onMounted(loadConfig);

function onReset() {
  kbPerSec.value = savedSnapshot.value;
}

async function onSave() {
  saving.value = true;
  try {
    const bytesPerSec =
      kbPerSec.value && kbPerSec.value > 0
        ? Math.round(kbPerSec.value * KB)
        : null;
    await configApi.updateInstallSettings({
      download_speed_limit_bytes_per_sec: bytesPerSec,
    });
    savedSnapshot.value = kbPerSec.value;
    await configStore.fetchConfig();
    snackbar.success(t("settings.stream-install-saved"));
  } catch (err) {
    const e = err as {
      response?: { data?: { detail?: string }; statusText?: string };
      message?: string;
    };
    const detail =
      e?.response?.data?.detail || e?.response?.statusText || e?.message;
    snackbar.error(t("settings.stream-install-save-error", { detail }));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="r-v2-stream-install">
    <SettingsSection
      :title="t('settings.stream-install-speed-limit')"
      icon="mdi-speedometer"
    >
      <p class="r-v2-stream-install__desc">
        {{ t("settings.stream-install-speed-limit-desc") }}
      </p>
      <RTextField
        v-model="kbPerSec"
        type="number"
        min="0"
        :disabled="!canEdit || loading"
        :label="t('settings.stream-install-speed-limit-field')"
        :placeholder="t('settings.stream-install-unlimited')"
        :hint="t('settings.stream-install-unlimited-hint')"
        class="r-v2-stream-install__field"
      >
        <template #append-inner>
          <span class="r-v2-stream-install__unit">KB/s</span>
        </template>
      </RTextField>
    </SettingsSection>

    <Transition name="r-v2-stream-install__bar">
      <div v-if="dirty && canEdit" class="r-v2-stream-install__bar">
        <span class="r-v2-stream-install__bar-label">
          {{ t("settings.scan-unsaved-changes") }}
        </span>
        <div class="r-v2-stream-install__bar-actions">
          <RBtn variant="text" :disabled="saving" @click="onReset">
            {{ t("common.discard") }}
          </RBtn>
          <RBtn
            variant="flat"
            color="primary"
            prepend-icon="mdi-content-save-outline"
            :loading="saving"
            @click="onSave"
          >
            {{ t("common.save") }}
          </RBtn>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.r-v2-stream-install {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.r-v2-stream-install__desc {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--r-color-fg-muted);
}
.r-v2-stream-install__field {
  max-width: 280px;
}
.r-v2-stream-install__unit {
  font-size: 12px;
  color: var(--r-color-fg-muted);
}

.r-v2-stream-install__bar {
  position: sticky;
  bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: var(--r-color-bg-elevated);
  border: 1px solid var(--r-color-border);
  border-radius: var(--r-radius-md);
  box-shadow: 0 8px 24px color-mix(in srgb, black 30%, transparent);
}
.r-v2-stream-install__bar-label {
  font-size: 13px;
  color: var(--r-color-fg-secondary);
}
.r-v2-stream-install__bar-actions {
  display: flex;
  gap: 8px;
}
.r-v2-stream-install__bar-enter-active,
.r-v2-stream-install__bar-leave-active {
  transition:
    opacity var(--r-motion-med) var(--r-motion-ease-out),
    transform var(--r-motion-med) var(--r-motion-ease-out);
}
.r-v2-stream-install__bar-enter-from,
.r-v2-stream-install__bar-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
