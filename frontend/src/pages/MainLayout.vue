<template>
  <div class="app-viewport flex overflow-hidden bg-white">
    <button
      v-if="isLeftPanelShow"
      type="button"
      class="fixed inset-0 z-40 bg-black/25 backdrop-blur-[1px] sm:hidden"
      aria-label="关闭导航"
      @click="hideLeftPanel"
    />
    <LeftPanel />
    <div className="flex-1 min-w-0 h-full py-0 pr-0 relative">
      <div className="flex h-full bg-[var(--background-gray-main)]">
        <div class="flex flex-1 min-w-0 min-h-0">
          <router-view />
          <FilePanel />
        </div>
      </div>
    </div>
  </div>
  <TakeOverView />
  <CustomDialog />
  <SessionFileList />
  <SettingsDialog />
  <ContextMenu />
</template>

<script setup lang="ts">
import LeftPanel from '@/components/LeftPanel.vue';
import CustomDialog from '@/components/ui/CustomDialog.vue';
import ContextMenu from '@/components/ui/ContextMenu.vue';
import TakeOverView from '@/components/TakeOverView.vue';
import SessionFileList from '@/components/SessionFileList.vue';
import FilePanel from '@/components/FilePanel.vue';
import SettingsDialog from '@/components/settings/SettingsDialog.vue';
import { onMounted, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useFilePanel } from '@/composables/useFilePanel';
import { useLeftPanel } from '@/composables/useLeftPanel';

const route = useRoute();
const { hideFilePanel } = useFilePanel();
const { isLeftPanelShow, hideLeftPanel } = useLeftPanel();

const isMobileViewport = () => window.matchMedia('(max-width: 639px)').matches;

onMounted(() => {
  if (isMobileViewport()) hideLeftPanel();
});

watch(
  () => route.fullPath,
  () => {
    hideFilePanel();
    if (isMobileViewport()) hideLeftPanel();
  }
);
</script>
