<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p class="mb-3 text-xs text-gray-500">EDF / EDF+C / BDF / BDF+C 连续记录试点。仅按需读取选定通道和相对时间窗，各通道保留原采样率与物理单位；不重采样、不滤波、不用于诊断。</p>
    <form v-if="data" class="rounded border p-3 text-sm" @submit.prevent="load(false)">
      <fieldset><legend>信号通道（最多 8 个；单位及采样率分别显示）</legend>
        <div class="my-2 grid max-h-40 grid-cols-1 gap-2 overflow-auto sm:grid-cols-2">
          <label v-for="channel in data.channels" :key="channel.id" class="flex items-center gap-2"><input v-model="channels" type="checkbox" :value="channel.id" :disabled="busy || !channel.selectable || channels.length >= 8 && !channels.includes(channel.id)" />#{{ channel.id + 1 }} {{ channel.label || '未命名' }} · {{ channel.unit || '未声明单位' }} · {{ channel.sample_rate }} Hz <span v-if="!channel.selectable">（标注／状态隐藏）</span></label>
        </div>
      </fieldset>
      <div class="flex flex-wrap items-end gap-3">
        <label>起点（秒）<input v-model.number="start" class="ml-2 w-28 rounded border p-1" type="number" min="0" :max="data.totalDuration" step="any" :disabled="busy" /></label>
        <label>时长（秒）<input v-model.number="duration" class="ml-2 w-24 rounded border p-1" type="number" min="0.000001" max="60" step="any" :disabled="busy" /></label>
        <button type="submit" class="rounded border px-3 py-1" :disabled="busy || !channels.length">读取时间窗</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">每窗总样本 ≤16,384；若超过上限，请减少通道或缩短时间窗。自动首窗只选第一个可读通道。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取授权的信号字节范围…</p>
    <p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <template v-if="data && !busy && !error">
      <p class="my-3 text-xs text-gray-500">{{ data.format.toUpperCase() }} · 记录时长 {{ data.totalDuration }} 秒 · 当前 {{ data.start }}–{{ data.start + data.duration }} 秒 · 本窗读取 {{ data.readBytes.toLocaleString() }} 字节 / 源文件 {{ data.sourceBytes.toLocaleString() }} 字节（{{ data.reads }} 次范围请求）</p>
      <NumericSeriesPlot :traces="data.traces" :x-range="timeRange" x-label="相对首条记录的时间（秒）" label="EDF / BDF 分通道信号曲线" />
    </template>
    <p v-for="warning in warnings" :key="warning" class="mt-2 text-xs text-amber-700">{{ warning }}</p>
    <p class="mt-3 text-xs text-gray-500">患者身份、记录身份、绝对日期与标注文本不返回。连续性依赖文件头声明，未读取标注时间轴；不连续 EDF+D/BDF+D 与同名 ESRF EDF 图像格式不支持。</p>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { parseSignalWindow, type SignalWindow } from './instrumentData';
import NumericSeriesPlot from './NumericSeriesPlot.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), data = shallowRef<SignalWindow>(), channels = ref<number[]>([]), start = ref(0), duration = ref(1);
const busy = ref(false), error = ref(''), warnings = ref<string[]>([]);
const timeRange = computed<[number, number] | undefined>(() => data.value ? [data.value.start, data.value.start + data.value.duration] : undefined);
let version: string | undefined;
async function load(initial: boolean) {
  const request = scope.begin(); busy.value = true; error.value = ''; warnings.value = [];
  const selection = { channels: [...channels.value], start_seconds: start.value, duration_seconds: duration.value };
  try {
    if (!initial && (!version || !channels.value.length || channels.value.length > 8 || !Number.isFinite(start.value) || start.value < 0 || !Number.isFinite(duration.value) || duration.value <= 0 || duration.value > 60)) throw new Error('请选择有效通道与不超过 60 秒的时间窗。');
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series', ...(version ? { version } : {}),
      ...(!initial ? selection : {}) }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'series' || version && result.version !== version) throw new Error('信号记录已变化，请重新打开预览。');
    const parsed = parseSignalWindow(result.payload, result.metadata);
    if (!initial && (JSON.stringify(parsed.selected) !== JSON.stringify(selection.channels) || parsed.start !== selection.start_seconds || parsed.duration !== selection.duration_seconds)) throw new Error('返回的信号窗口与当前请求不一致。');
    data.value = parsed; channels.value = [...parsed.selected]; start.value = parsed.start; duration.value = parsed.duration;
    version = result.version; warnings.value = result.warnings.slice(0, 8);
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '无法读取信号窗口。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version], () => { data.value = undefined; version = undefined; channels.value = []; start.value = 0; duration.value = 1; void load(true); }, { immediate: true });
</script>
