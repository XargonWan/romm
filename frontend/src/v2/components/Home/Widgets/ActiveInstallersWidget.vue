<script setup lang="ts">
// ActiveInstallersWidget — surfaces this user's in-progress and finished
// (still cached) server-side installs on Home. Renders nothing when
// `entries` is empty; WidgetBar folds that into whether the whole rail is
// worth showing (see useInstallDashboard). Clicking a row's title jumps
// straight to the install page for a still-active entry, or to the game's
// detail page once it's finished/failed (nothing left to watch there).
import { RIcon, RSpinner } from "@v2/lib";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import type { InstallDashboardEntrySchema } from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import GameCover from "@/v2/components/shared/GameCover.vue";
import WidgetCard from "./WidgetCard.vue";

defineOptions({ inheritAttrs: false });

defineProps<{
  entries: InstallDashboardEntrySchema[];
  loading?: boolean;
}>();

const { t } = useI18n();
const router = useRouter();

const ACTIVE_STATES = [
  "detecting",
  "awaiting_installer",
  "installing",
  "streaming",
];

function isActive(entry: InstallDashboardEntrySchema): boolean {
  return ACTIVE_STATES.includes(entry.session.state);
}

function statusLabel(entry: InstallDashboardEntrySchema): string {
  switch (entry.session.state) {
    case "streaming":
      return t("rom.install-copying");
    case "installing":
      return t("rom.install-starting");
    case "done":
      return t("home.widget-active-installers-installed");
    default:
      return t("rom.install-detecting");
  }
}

function openEntry(entry: InstallDashboardEntrySchema) {
  const name = isActive(entry) ? ROUTES.INSTALL : ROUTES.ROM;
  router.push({ name, params: { rom: entry.rom_id } });
}
</script>

<template>
  <WidgetCard
    :title="t('home.widget-active-installers')"
    :loading="loading"
    width="260px"
  >
    <ul class="r-v2-widget-installs__list">
      <li
        v-for="entry in entries"
        :key="entry.rom_id"
        class="r-v2-widget-installs__row"
      >
        <GameCover
          :rom="null"
          :cover-src="entry.path_cover_small"
          :title="entry.rom_name ?? ''"
          class="r-v2-widget-installs__cover"
        />
        <div class="r-v2-widget-installs__info">
          <button
            type="button"
            class="r-v2-widget-installs__name"
            @click="openEntry(entry)"
          >
            {{ entry.rom_name }}
          </button>
          <span
            class="r-v2-widget-installs__status"
            :class="{
              'r-v2-widget-installs__status--done':
                entry.session.state === 'done',
            }"
          >
            <RSpinner v-if="isActive(entry)" :size="10" />
            <RIcon v-else icon="mdi-check-circle-outline" size="11" />
            {{ statusLabel(entry) }}
          </span>
        </div>
      </li>
    </ul>
  </WidgetCard>
</template>

<style scoped>
.r-v2-widget-installs__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  scrollbar-width: thin;
}

.r-v2-widget-installs__row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.r-v2-widget-installs__cover {
  height: 32px;
  width: auto;
  flex-shrink: 0;
  --r-cover-radius: var(--r-radius-xs);
}

.r-v2-widget-installs__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.r-v2-widget-installs__name {
  appearance: none;
  background: transparent;
  border: 0;
  padding: 0;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: var(--r-font-weight-semibold);
  color: var(--r-color-fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.r-v2-widget-installs__name:hover {
  color: var(--r-color-brand-primary);
}

.r-v2-widget-installs__status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--r-color-fg-muted);
}
.r-v2-widget-installs__status--done {
  color: var(--r-color-success);
}
</style>
