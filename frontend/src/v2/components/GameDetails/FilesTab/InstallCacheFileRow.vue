<script setup lang="ts">
// InstallCacheFileRow — one file inside the "Installer Cache" subtab.
// Deliberately not FileRow: these come from a finished install's manifest
// (path/size/sha1 only, see InstallFileSchema), not a RomFileSchema - no
// numeric id, no category, no selection/upload/delete story. Just a name,
// size, verifiable hash, and a download link.
import { RBtn } from "@v2/lib";
import { useI18n } from "vue-i18n";
import type { InstallFileSchema } from "@/__generated__";
import { formatBytes } from "@/utils";
import HashChip from "@/v2/components/shared/HashChip.vue";

defineOptions({ inheritAttrs: false });

const { t } = useI18n();

defineProps<{
  file: InstallFileSchema;
  downloadPath: string;
}>();
</script>

<template>
  <li class="r-v2-file-row">
    <div class="r-v2-file-row__main">
      <div class="r-v2-file-row__name">
        <span class="r-v2-file-row__path" :title="file.path">
          {{ file.path }}
        </span>
      </div>
      <div class="r-v2-file-row__meta">
        <span class="r-v2-file-row__size">{{
          formatBytes(file.size_bytes)
        }}</span>
      </div>
      <div class="r-v2-file-row__hashes">
        <HashChip label="SHA-1" :value="file.sha1" compact />
      </div>
    </div>
    <div class="r-v2-file-row__actions">
      <RBtn
        icon="mdi-download-outline"
        variant="text"
        size="small"
        :href="downloadPath"
        :tooltip="t('rom.download-file')"
        :aria-label="t('rom.download-named', { name: file.path })"
      />
    </div>
  </li>
</template>

<style scoped>
/* Mirrors FileRow's layout (same row shape across both subtab kinds),
   minus the leading checkbox this row never needs. */
.r-v2-file-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  background: var(--r-color-bg-elevated);
  border: 1px solid var(--r-color-border);
  border-radius: var(--r-radius-md);
  color: var(--r-color-fg);
  transition: border-color var(--r-motion-fast) var(--r-motion-ease-out);
}
.r-v2-file-row:hover {
  border-color: var(--r-color-border-strong);
}
.r-v2-file-row__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.r-v2-file-row__name {
  font-size: 13px;
  font-weight: var(--r-font-weight-medium);
  min-width: 0;
}
.r-v2-file-row__path {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--r-font-mono, monospace);
  font-size: 12.5px;
  display: block;
}
.r-v2-file-row__meta {
  font-size: 11px;
  color: var(--r-color-fg-faint);
}
.r-v2-file-row__size {
  font-variant-numeric: tabular-nums;
}
.r-v2-file-row__hashes {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.r-v2-file-row__actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  align-self: center;
}
</style>
