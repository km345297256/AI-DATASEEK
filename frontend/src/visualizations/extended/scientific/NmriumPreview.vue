<template>
  <PreviewFrame :loading="loading" :error="error" note="NMRium 0.60 · 已处理 1D JCAMP-DX 只读谱图；不导入外部数据、不自动处理 FID，不修改原文件。">
    <div ref="target" class="flex-none bg-white" style="height: 480px" @drop.capture.prevent.stop @paste.capture.prevent.stop />
    <p v-if="sampled" class="text-xs text-amber-700">当前谱图为有界采样，峰高和细节请以完整原始数据分析为准。</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { numericPairs } from './data';
import { loadNmrBrowserLibrary } from './browserLibraries';
import PreviewFrame from './PreviewFrame.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
watch([() => props.file.file_id, () => props.plugin.id], async () => {
  const load = scope.begin(); loading.value = true; error.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, { kind: 'series' }, load.signal);
    load.assertCurrent();
    const { x, y: re } = numericPairs(result);
    if (x.length < 2) throw new Error('核磁谱需至少两个有效数值点。');
    const metadata = result.metadata as Record<string, unknown> | undefined;
    if (metadata?.is_fid === true) throw new Error('此插件不执行 FID 自动处理。');
    if (metadata?.x_unit !== 'PPM' && metadata?.x_unit !== 'ppm') throw new Error('NMRium 首期需要 ppm 横轴的已处理核磁谱；请先明确转换单位。');
    const nucleus = metadata?.nucleus; if (typeof nucleus !== 'string' || !/^\d{1,3}[A-Z][a-z]?$/.test(nucleus)) throw new Error('谱图缺少明确的观测核信息。');
    const library = await loadNmrBrowserLibrary(load.signal);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    // Explicit data only; no source, URL, originalData, filters or saved workspace
    // from a file can reach NMRium. Settings above override built-in imports/QC.
    const dispose = library.mountNmr(target.value!, { x, y: re, nucleus, ...(typeof metadata?.frequency === 'number' && metadata.frequency > 0 ? { frequency: metadata.frequency } : {}) }, () => { if (load.isCurrent()) error.value = '核磁谱图渲染失败，请核对数据。'; });
    load.onDispose(dispose);
    sampled.value = result.sampled === true;
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '核磁谱图预览失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}, { immediate: true });
</script>
