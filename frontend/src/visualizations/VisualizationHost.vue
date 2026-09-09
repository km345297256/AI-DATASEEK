<template>
  <div class="flex min-h-0 flex-1 flex-col">
    <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-secondary)]">
      <label v-if="candidates.length" class="flex min-w-0 flex-1 items-center gap-2">
        可视化
        <select :value="selectedPlugin?.id || ''" class="min-w-0 flex-1 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 py-1.5 text-[var(--text-primary)]" aria-label="选择可视化插件" @change="selectedId = ($event.target as HTMLSelectElement).value">
          <option v-for="plugin in candidates" :key="plugin.id" :value="plugin.id">{{ plugin.name }} · {{ viewKindLabel(plugin.view_kind) }}</option>
        </select>
      </label>
      <RouterLink v-if="!shared" to="/plugins?tab=renderers" class="shrink-0 underline">管理可视化插件</RouterLink>
      <button type="button" class="shrink-0 underline disabled:opacity-50" :disabled="loading" @click="refresh">刷新</button>
    </div>
    <div v-if="error" role="alert" class="m-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">{{ error }}</div>
    <div v-else-if="!catalog && loading" role="status" class="p-8 text-center text-sm text-[var(--text-tertiary)]">正在加载 Cordis 可视化插件…</div>
    <div v-else-if="!selectedPlugin" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">此格式没有已启用的可视化插件。<br />可在插件管理中启用对应能力，或下载原始文件。</div>
    <div v-else-if="blockedReason" role="alert" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">{{ blockedReason }}</div>
    <component v-else-if="adapter && selectedPlugin.data_kind === 'scientific'" :is="adapter" :key="instanceKey" :file="file" :plugin="selectedPlugin" />
    <component v-else-if="adapter" :is="adapter" :key="instanceKey" :file="file" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, RouterLink } from 'vue-router';
import type { FileInfo } from '../api/file';
import { useVisualizationCatalog } from './catalog';
import { matchingVisualizations, selectVisualization, viewKindLabel } from './contract';
import { getVisualizationAdapter } from './adapters';

const props = defineProps<{ file: FileInfo }>();
const route = useRoute();
const shared = computed(() => route.path.startsWith('/share/'));
const { catalog, error, loading, refresh } = useVisualizationCatalog();
const selectedId = ref<string | null>(null);
const candidates = computed(() => matchingVisualizations(catalog.value?.plugins ?? [], props.file.filename));
const selectedPlugin = computed(() => selectVisualization(catalog.value?.plugins ?? [], props.file.filename, selectedId.value));
const adapter = computed(() => selectedPlugin.value ? getVisualizationAdapter(selectedPlugin.value.adapter) : null);
// A capability version/state revision, file, or user-selected view replacement owns a
// fresh Vue scope. Unmount aborts requests and disposes canvas/WebGL/blob resources.
const instanceKey = computed(() => `${props.file.file_id}:${catalog.value?.revision}:${selectedPlugin.value?.id}:${selectedPlugin.value?.version}:${selectedPlugin.value?.adapter}:${selectedPlugin.value?.reader}`);
const blockedReason = computed(() => {
  const plugin = selectedPlugin.value;
  if (!plugin) return '';
  if (shared.value && plugin.data_kind === 'scientific') return '共享页面暂不开放科学数据读取接口。请在原会话中使用此插件，或下载原始文件。';
  // Text/CSV read bounded pages; their descriptor budget is per page, not a
  // maximum source-file size. Preserve large-file pagination and downloads.
  if (plugin.data_kind === 'file' && !['text', 'csv'].includes(plugin.adapter) && props.file.size != null && (!Number.isFinite(props.file.size) || props.file.size < 0 || props.file.size > plugin.limits.max_input_bytes)) {
    return `文件超出该插件的安全预览上限（${Math.round(plugin.limits.max_input_bytes / 1024 / 1024)} MiB），请下载后分析。`;
  }
  return '';
});
watch(() => props.file.file_id, () => { selectedId.value = null; });
let poll: ReturnType<typeof setInterval> | undefined;
const refreshVisible = () => { if (document.visibilityState !== 'hidden') void refresh(); };
onMounted(() => {
  void refresh();
  poll = setInterval(refreshVisible, 15000);
  window.addEventListener('focus', refreshVisible);
});
onUnmounted(() => {
  if (poll) clearInterval(poll);
  window.removeEventListener('focus', refreshVisible);
});
</script>
