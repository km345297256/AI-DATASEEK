<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p class="mb-3 text-xs text-gray-500">MCA 试点：仅支持带 PMCA / DPPMCA / AMPTEK 标识的 ASCII 单能谱，最多 8,192 通道、4 MiB。显示原始整数计数，不拟合、不扣背景。</p>
    <p v-if="busy" role="status">正在读取单能谱…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <template v-if="data && !error">
      <p class="my-2 text-sm">{{ data.channels }} 通道 · {{ data.calibrated ? '使用文件明确给出的双点线性能量标定' : '无受支持的能量标定，横轴保持通道索引' }}</p>
      <p v-if="data.coefficients" class="text-xs text-gray-500">能量 = {{ data.coefficients[0] }} + {{ data.coefficients[1] }} × 通道索引</p>
      <p class="my-2 text-xs text-gray-500">有效采集时间：{{ data.liveTime ?? '未声明' }} 秒；实际采集时间：{{ data.realTime ?? '未声明' }} 秒。</p>
      <NumericSeriesPlot :traces="[data.trace]" :x-label="data.xLabel" label="MCA 原始计数能谱" />
    </template>
    <p v-for="warning in warnings" :key="warning" class="mt-2 text-xs text-amber-700">{{ warning }}</p>
  </section>
</template>
<script setup lang="ts">
import { ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { parseMcaSpectrum, type McaSpectrum } from './instrumentData';
import NumericSeriesPlot from './NumericSeriesPlot.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), data = shallowRef<McaSpectrum>(), busy = ref(false), error = ref(''), warnings = ref<string[]>([]);
async function load() {
  const request = scope.begin(); busy.value = true; error.value = ''; data.value = undefined; warnings.value = [];
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series' }, request.signal);
    request.assertCurrent(); if (result.kind !== 'array') throw new Error('能谱响应不符合插件数值协议。');
    data.value = parseMcaSpectrum(result.payload, result.metadata); warnings.value = result.warnings.slice(0, 8);
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '无法读取当前能谱。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version], () => { void load(); }, { immediate: true });
</script>
