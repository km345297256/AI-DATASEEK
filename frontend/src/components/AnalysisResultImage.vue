<template>
  <div ref="container" class="relative flex min-h-32 w-full items-center justify-center bg-white">
    <img v-if="source && !failed" :key="source" :src="source" :alt="file.filename" loading="lazy"
      class="max-h-[520px] w-full object-contain" @error="imageFailed" />
    <span v-else class="px-4 py-8 text-center text-xs text-[var(--text-tertiary)]" role="status">{{ status }}</span>
    <span v-if="isSvg && source && !failed" class="absolute bottom-1 right-1 rounded bg-white/90 px-1.5 py-0.5 text-[10px] text-gray-500">SVG · 静态预览</span>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue';
import { API_CONFIG } from '../api/client';
import type { FileInfo } from '../api/file';
import { usePreviewLoad } from '../composables/usePreviewLoad';
import { useVisualizationCatalog } from '../visualizations/catalog';
import { matchingVisualizations } from '../visualizations/contract';
import { filePreviewIdentity, pluginPreviewIdentity } from '../visualizations/previewIdentity';
import { loadSvgPreview } from '../visualizations/svgPreview';

const props = defineProps<{ file: FileInfo }>();
const container = ref<HTMLElement | null>(null);
const visible = ref(typeof IntersectionObserver === 'undefined');
const source = ref('');
const failed = ref(false);
const status = ref('正在准备预览…');
const isSvg = computed(() => /\.svg$/i.test(props.file.filename) || props.file.content_type?.split(';')[0]?.trim().toLowerCase() === 'image/svg+xml');
const registry = useVisualizationCatalog();
const plugin = computed(() => isSvg.value ? matchingVisualizations(registry.catalog.value?.plugins ?? [], props.file.filename).find(item => item.adapter === 'svg') : undefined);
const loads = usePreviewLoad();
let observer: IntersectionObserver | undefined;
onMounted(() => {
  if (!visible.value && container.value) {
    observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { visible.value = true; observer?.disconnect(); }
    }, { rootMargin: '200px' });
    observer.observe(container.value);
  }
});
onScopeDispose(() => observer?.disconnect());
watch(() => visible.value && isSvg.value, needed => { if (needed) void registry.refresh(); }, { immediate: true });
watch(() => JSON.stringify([visible.value, filePreviewIdentity(props.file), isSvg.value,
  plugin.value ? pluginPreviewIdentity(plugin.value) : null,
  isSvg.value ? registry.error.value : props.file.file_url]), async () => {
  const load = loads.begin();
  source.value = ''; failed.value = false; status.value = '正在准备预览…';
  if (!visible.value) return;
  if (!isSvg.value) {
    const url = props.file.file_url;
    source.value = url ? (/^https?:\/\//i.test(url) ? url : `${API_CONFIG.host}${url}`) : '';
    if (!source.value) status.value = '暂无缩略图，点击查看或下载原文件';
    return;
  }
  if (!plugin.value) {
    status.value = registry.error.value || (registry.catalog.value ? 'SVG 预览插件未启用，点击查看或下载原文件' : '正在准备 SVG 预览…');
    return;
  }
  try {
    const preview = await loadSvgPreview(props.file, plugin.value, load.signal);
    load.assertCurrent();
    const url = URL.createObjectURL(preview.blob);
    load.onDispose(() => URL.revokeObjectURL(url));
    source.value = url;
  } catch (error) {
    if (!load.isCurrent()) return;
    failed.value = true;
    status.value = error instanceof Error ? error.message : 'SVG 预览暂不可用，点击查看或下载原文件';
  }
}, { immediate: true });
function imageFailed(event: Event) {
  if ((event.target as HTMLImageElement | null)?.getAttribute('src') !== source.value) return;
  failed.value = true;
  status.value = '预览暂不可用，点击查看或下载原文件';
}
</script>
