<template>
  <section class="flex min-h-0 flex-1 flex-col text-[var(--text-primary)]" aria-label="SVG 图表预览">
    <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs">
      <span class="mr-auto text-[var(--text-secondary)]">SVG 图表<span v-if="width && height"> · 预览 {{ width }} × {{ height }}</span></span>
      <button type="button" class="svg-preview-control" :disabled="!imageUrl || (!fitWidth && zoom <= 0.1)" aria-label="缩小 SVG 预览" @click="changeZoom(-1)">−</button>
      <span class="min-w-14 text-center tabular-nums" aria-live="polite">{{ fitWidth ? '适应宽度' : `${Math.round(zoom * 100)}%` }}</span>
      <button type="button" class="svg-preview-control" :disabled="!imageUrl || (!fitWidth && zoom >= 4)" aria-label="放大 SVG 预览" @click="changeZoom(1)">＋</button>
      <button type="button" class="svg-preview-control" :disabled="!imageUrl" @click="fitToWidth">适应宽度</button>
      <button type="button" class="svg-preview-control" :disabled="!imageUrl" @click="resetView">复位</button>
      <label class="ml-1 flex items-center gap-1">背景
        <select v-model="background" aria-label="SVG 预览背景" class="rounded border border-[var(--border-main)] bg-[var(--background-white-main)] px-2 py-1">
          <option value="white">白色</option><option value="checkerboard">透明棋盘</option>
        </select>
      </label>
    </div>
    <p class="shrink-0 px-4 py-2 text-xs leading-relaxed text-[var(--text-tertiary)]">安全静态预览（非交互 SVG），缩放仅作用于预览图；顶部下载按钮保留原始 SVG 矢量文件。</p>
    <p v-if="loading" role="status" class="px-4 py-3 text-sm text-[var(--text-secondary)]">正在生成 SVG 图表预览…</p>
    <div v-if="error" role="alert" class="mx-4 mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
      <p>{{ error }}</p>
      <button type="button" class="mt-2 underline underline-offset-2" :disabled="loading || !plugin.enabled" @click="loadPreview">重新加载预览</button>
    </div>
    <div ref="viewport" class="min-h-0 flex-1 overflow-auto p-4" :class="background === 'checkerboard' ? 'svg-preview-checkerboard' : 'bg-white'">
      <div v-if="imageUrl" class="mx-auto" :style="{ width: fitWidth ? '100%' : `${Math.round(width * zoom)}px` }">
        <img :key="imageUrl" :src="imageUrl" :alt="`${file.filename} 的安全静态预览`" class="block h-auto w-full max-w-none" draggable="false" @error="onImageError" />
      </div>
    </div>
    <p v-for="warning in warnings" :key="warning" class="shrink-0 px-4 py-1 text-xs text-[var(--text-secondary)]">{{ warning }}</p>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../../visualizations/contract';
import { filePreviewIdentity, pluginPreviewIdentity } from '../../visualizations/previewIdentity';
import { loadSvgPreview } from '../../visualizations/svgPreview';
import { usePreviewLoad } from '../../composables/usePreviewLoad';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad();
const viewport = ref<HTMLDivElement>();
const imageUrl = ref(''), loading = ref(false), error = ref('');
const width = ref(0), height = ref(0), warnings = ref<string[]>([]);
const zoom = ref(1), fitWidth = ref(true), background = ref<'white' | 'checkerboard'>('white');

function resetScroll() { if (viewport.value) { viewport.value.scrollLeft = 0; viewport.value.scrollTop = 0; } }
function fitToWidth() { fitWidth.value = true; resetScroll(); }
function resetView() { fitWidth.value = false; zoom.value = 1; resetScroll(); }
function changeZoom(direction: number) {
  if (!imageUrl.value || !width.value) return;
  const shownZoom = fitWidth.value && viewport.value
    ? Math.max(1, viewport.value.clientWidth - 32) / width.value : zoom.value;
  zoom.value = Math.max(0.1, Math.min(4, Math.round(shownZoom * (direction > 0 ? 1.25 : 0.8) * 100) / 100));
  fitWidth.value = false;
}
function onImageError(event: Event) {
  const target = event.target as HTMLImageElement | null;
  if (!target || !imageUrl.value || target.getAttribute('src') !== imageUrl.value) return;
  imageUrl.value = '';
  error.value = 'SVG 静态预览图未能显示。可重新加载，或使用顶部按钮下载原始文件。';
}
async function loadPreview() {
  const load = loads.begin();
  imageUrl.value = ''; width.value = 0; height.value = 0; warnings.value = [];
  error.value = ''; loading.value = true; zoom.value = 1; fitWidth.value = true; resetScroll();
  try {
    const result = await loadSvgPreview(props.file, props.plugin, load.signal);
    load.assertCurrent();
    const url = URL.createObjectURL(result.blob);
    load.onDispose(() => { URL.revokeObjectURL(url); if (imageUrl.value === url) imageUrl.value = ''; });
    width.value = result.width; height.value = result.height; warnings.value = result.warnings;
    imageUrl.value = url;
  } catch (reason) {
    if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'SVG 图表暂时无法预览，请重试或下载原始文件。';
  } finally { if (load.isCurrent()) loading.value = false; }
}

// A fresh poll descriptor or rotated download URL is not new file content.
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], loadPreview, { immediate: true });
</script>

<style scoped>
.svg-preview-control { border: 1px solid var(--border-main); border-radius: 0.375rem; padding: 0.25rem 0.5rem; }
.svg-preview-control:hover:not(:disabled) { background: var(--fill-tsp-gray-main); }
.svg-preview-control:disabled { opacity: 0.4; cursor: not-allowed; }
.svg-preview-checkerboard {
  background-color: white;
  background-image: linear-gradient(45deg, #e5e7eb 25%, transparent 25%), linear-gradient(-45deg, #e5e7eb 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #e5e7eb 75%), linear-gradient(-45deg, transparent 75%, #e5e7eb 75%);
  background-size: 20px 20px;
  background-position: 0 0, 0 10px, 10px -10px, -10px 0;
}
</style>
