<script setup lang="ts">
// DownloadOrInstallDialog — the Download button's fork for Windows ROMs
// (see GameActions.vue): asks whether the click means "just give me the
// file" or "install it on this machine", instead of guessing.
import { RBtn, RDialog, RIcon } from "@v2/lib";
import { useI18n } from "vue-i18n";

defineProps<{
  modelValue: boolean;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "download"): void;
  (e: "install"): void;
}>();

const { t } = useI18n();

function close() {
  emit("update:modelValue", false);
}
</script>

<template>
  <RDialog
    :model-value="modelValue"
    icon="mdi-download-box-outline"
    width="380"
    @update:model-value="(v) => !v && close()"
  >
    <template #header>
      <span>{{ t("rom.download-or-install-title") }}</span>
    </template>
    <template #content>
      <div class="download-or-install__choices">
        <button
          type="button"
          class="download-or-install__choice"
          @click="emit('download')"
        >
          <RIcon icon="mdi-download-outline" size="22" />
          <span class="download-or-install__choice-text">
            <strong>{{ t("rom.download") }}</strong>
            <small>{{ t("rom.download-or-install-download-hint") }}</small>
          </span>
        </button>
        <button
          type="button"
          class="download-or-install__choice"
          @click="emit('install')"
        >
          <RIcon icon="mdi-download-box-outline" size="22" />
          <span class="download-or-install__choice-text">
            <strong>{{ t("rom.install") }}</strong>
            <small>{{ t("rom.download-or-install-install-hint") }}</small>
          </span>
        </button>
      </div>
    </template>
    <template #footer>
      <RBtn variant="text" @click="close">
        {{ t("common.cancel") }}
      </RBtn>
    </template>
  </RDialog>
</template>

<style scoped>
.download-or-install__choices {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.download-or-install__choice {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--r-color-border);
  background: transparent;
  color: inherit;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--r-motion-fast) var(--r-motion-ease-out);
}
.download-or-install__choice:hover {
  background: var(--r-color-surface);
}
.download-or-install__choice-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.download-or-install__choice-text strong {
  font-size: 14px;
  font-weight: var(--r-font-weight-medium);
}
.download-or-install__choice-text small {
  color: var(--r-color-fg-faint);
  font-size: 12px;
}
</style>
