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
      <button type="button" class="shrink-0 underline disabled:opacity-50" :disabled="loading" @click="manualRefresh">刷新</button>
    </div>
    <p v-if="profileBusy" role="status" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">正在有界识别文件内容…</p>
    <p v-else-if="profile" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">{{ profileLabel(profile) }} · 可手动切换视图</p>
    <p v-else-if="profileError" class="px-4 py-2 text-xs text-[var(--text-tertiary)]">{{ profileError }}</p>
    <div v-if="error" role="alert" class="m-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">{{ error }}</div>
    <div v-else-if="!catalog && loading" role="status" class="p-8 text-center text-sm text-[var(--text-tertiary)]">正在加载 Cordis 可视化插件…</div>
    <div v-else-if="!selectedPlugin" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">此格式没有已启用的可视化插件。<br />可在插件管理中启用对应能力，或下载原始文件。</div>
    <div v-else-if="blockedReason" role="alert" class="p-8 text-center text-sm leading-6 text-[var(--text-tertiary)]">{{ blockedReason }}</div>
    <component v-else-if="adapter && !profileBusy" :is="adapter" :key="instanceKey" :file="file" :plugin="selectedPlugin" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, RouterLink } from 'vue-router';
import type { FileInfo } from '../api/file';
import { useVisualizationCatalog } from './catalog';
import { matchingVisualizations, viewKindLabel } from './contract';
import { getVisualizationAdapter } from './adapters';
import { requestVisualization } from './runtime';
import { filePreviewIdentity, pluginPreviewIdentity } from './previewIdentity';
import { canProfileFilename, parseContentProfile, profileCandidates, profileLabel, type ContentProfile } from './contentProfile';

const props = defineProps<{ file: FileInfo }>();
const route = useRoute();
const shared = computed(() => route.path.startsWith('/share/'));
const { catalog, error, loading, refresh } = useVisualizationCatalog();
const selectedId = ref<string | null>(null);
const refreshEpoch = ref(0);
const profile = ref<ContentProfile | null>(null), profileBusy = ref(false), profileError = ref('');
const filenameCandidates = computed(() => matchingVisualizations(catalog.value?.plugins ?? [], props.file.filename));
const candidates = computed(() => profileCandidates(filenameCandidates.value, profile.value));
const selectedPlugin = computed(() => candidates.value.find(plugin => plugin.id === selectedId.value) ?? candidates.value[0] ?? null);
const adapter = computed(() => selectedPlugin.value ? getVisualizationAdapter(selectedPlugin.value.adapter) : null);
const fileIdentity = computed(() => filePreviewIdentity(props.file));
// A capability version/state revision, file, or user-selected view replacement owns a
// fresh Vue scope. Unmount aborts requests and disposes canvas/WebGL/blob resources.
const instanceKey = computed(() => JSON.stringify([fileIdentity.value, catalog.value?.revision,
  selectedPlugin.value ? pluginPreviewIdentity(selectedPlugin.value) : null, refreshEpoch.value]));
const manualRefresh = async () => {
  const previousKey = instanceKey.value;
  const confirmed = await refresh();
  // A genuine capability/file change already replaced the scope. Do not load it twice,
  // or refresh a different file that the user opened while confirmation was pending.
  if (confirmed && catalog.value && !error.value && instanceKey.value === previousKey) refreshEpoch.value += 1;
};
const blockedReason = computed(() => {
  const plugin = selectedPlugin.value;
  if (!plugin) return '';
  if (shared.value && !plugin.capabilities.shared) return '此插件不开放共享读取。请在原会话中使用此插件，或下载原始文件。';
  // Text/CSV read bounded pages; their descriptor budget is per page, not a
  // maximum source-file size. Preserve large-file pagination and downloads.
  if (plugin.capabilities.input_mode === 'whole' && props.file.size != null && (!Number.isFinite(props.file.size) || props.file.size < 0 || props.file.size > plugin.limits.max_input_bytes)) {
    return `文件超出该插件的安全预览上限（${Math.round(plugin.limits.max_input_bytes / 1024 / 1024)} MiB），请下载后分析。`;
  }
  return '';
});
watch(() => props.file.file_id, () => { selectedId.value = null; });
// A profile only chooses among already authorized, enabled candidates. A fresh
// generation owns its request; disable/reload/close cannot resurrect old content.
watch(() => JSON.stringify([fileIdentity.value, props.file.filename, catalog.value?.revision, refreshEpoch.value,
  filenameCandidates.value.map(pluginPreviewIdentity), shared.value]), async (_, __, onCleanup) => {
  profile.value = null; profileError.value = ''; profileBusy.value = false;
  if (shared.value || !canProfileFilename(props.file.filename)) return;
  const plugin = filenameCandidates.value.find(p => p.capabilities.operations.includes('prepare') && ['tiff', 'plotly', 'h5web', 'viv', 'openlayers'].includes(p.adapter));
  if (!plugin || (props.file.size != null && (!Number.isFinite(props.file.size) || props.file.size < 0 || props.file.size > plugin.limits.max_input_bytes))) return;
  const controller = new AbortController();
  onCleanup(() => controller.abort());
  profileBusy.value = true;
  try {
    const result = await requestVisualization(props.file, plugin, 'prepare', {}, controller.signal);
    if (controller.signal.aborted) return;
    if (result.kind !== 'resources' || result.metadata.purpose !== 'content-profile' || result.revision !== catalog.value?.revision) throw new Error('内容画像已过期。');
    profile.value = parseContentProfile(result.payload.profile);
  } catch {
    if (!controller.signal.aborted) profileError.value = '内容识别未完成；仍可选择已有视图，由对应读取器验证格式。';
  } finally { if (!controller.signal.aborted) profileBusy.value = false; }
}, { immediate: true });
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
