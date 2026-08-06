<template>
  <div class="flex h-full w-full flex-col overflow-hidden bg-[var(--background-gray-main)]">
    <header class="mobile-safe-top flex items-start gap-3 border-b border-[var(--border-main)] px-3 pb-4 sm:px-6 sm:py-5">
      <button
        v-if="!isLeftPanelShow"
        type="button"
        class="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-gray-main)] sm:hidden"
        aria-label="打开导航"
        @click="toggleLeftPanel"
      >
        <PanelLeft class="size-5 text-[var(--icon-secondary)]" />
      </button>
      <div class="min-w-0 flex-1">
        <h1 class="text-2xl font-semibold">系统管理</h1>
        <p class="mt-1 text-sm text-[var(--text-tertiary)]">管理 AI-DataSeek 的资源、分析任务与插件。</p>
      </div>
    </header>

    <nav class="border-b border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 sm:px-6">
      <select
        :value="activeTab"
        class="my-3 h-11 w-full rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-sm outline-none sm:hidden"
        aria-label="管理模块"
        @change="setActiveTab(($event.target as HTMLSelectElement).value as AdminTab)"
      >
        <option v-for="tab in tabs" :key="tab.key" :value="tab.key">{{ tab.label }}</option>
      </select>
      <div class="mx-auto hidden max-w-[1200px] gap-2 overflow-x-auto py-3 sm:flex">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          class="shrink-0 rounded-xl px-4 py-2 text-sm transition-colors"
          :class="activeTab === tab.key ? 'bg-[var(--fill-tsp-white-main)] text-[var(--text-primary)] shadow-sm' : 'text-[var(--text-tertiary)] hover:bg-[var(--fill-tsp-white-light)]'"
          @click="setActiveTab(tab.key)"
        >
          {{ tab.label }}
        </button>
      </div>
    </nav>

    <main class="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
      <div class="mx-auto max-w-[1200px]">
        <ResourceUsageManagement v-if="activeTab === 'resources'" />
        <ResourceConfigurationManagement v-else-if="activeTab === 'resource-config'" />
        <TaskManagement v-else-if="activeTab === 'tasks'" />
        <MCPManagement v-else-if="activeTab === 'mcp'" />
        <SkillManagement v-else />
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { PanelLeft } from 'lucide-vue-next';
import ResourceUsageManagement from '@/components/admin/ResourceUsageManagement.vue';
import ResourceConfigurationManagement from '@/components/admin/ResourceConfigurationManagement.vue';
import TaskManagement from '@/components/admin/TaskManagement.vue';
import MCPManagement from '@/components/admin/MCPManagement.vue';
import SkillManagement from '@/components/admin/SkillManagement.vue';
import { useLeftPanel } from '@/composables/useLeftPanel';

type AdminTab = 'resources' | 'resource-config' | 'tasks' | 'mcp' | 'skills';
const tabs: Array<{ key: AdminTab; label: string }> = [
  { key: 'resources', label: '资源用量' },
  { key: 'resource-config', label: '资源配置' },
  { key: 'tasks', label: '任务管理' },
  { key: 'mcp', label: 'MCP 管理' },
  { key: 'skills', label: '技能管理' },
];
const tabKeys = new Set<AdminTab>(tabs.map((item) => item.key));
const route = useRoute();
const router = useRouter();
const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel();
const initialTab = typeof route.query.tab === 'string' && tabKeys.has(route.query.tab as AdminTab)
  ? route.query.tab as AdminTab
  : 'resources';
const activeTab = ref<AdminTab>(initialTab);

function setActiveTab(tab: AdminTab) {
  activeTab.value = tab;
  void router.replace({ query: { ...route.query, tab } });
}

watch(() => route.query.tab, (tab) => {
  activeTab.value = typeof tab === 'string' && tabKeys.has(tab as AdminTab) ? tab as AdminTab : 'resources';
});
</script>
