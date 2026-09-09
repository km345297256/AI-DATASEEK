<template>
  <PreviewFrame :loading="loading" :error="error" note="Plotly · 有界表格数值探索；空值保持缺失，布尔值按 false=0/true=1 绘制，不执行表格公式。">
    <form v-if="arrayFile" class="mb-2 flex flex-wrap gap-3 text-xs" @submit.prevent="loadData">
      <label v-if="variables.length">变量 <select v-model="variable" @change="indices = []; sourceShape = []"><option v-for="name in variables" :key="name">{{ name }}</option></select></label>
      <label>视图 <select v-model="arrayKind" @change="indices = []"><option value="series">数值曲线</option><option value="heatmap">数组热图</option></select></label>
      <label v-for="(size, index) in sliceShape" :key="index">dim_{{ index }} <input v-model.number="indices[index]" type="number" min="0" :max="size - 1" step="1" class="w-16 border" /></label>
      <button type="submit" class="rounded border px-2" :disabled="loading">读取变量／切片</button>
    </form>
    <form v-if="table" class="mb-2 flex flex-wrap gap-3 text-xs" @submit.prevent="draw">
      <label>横轴 <select v-model.number="xColumn"><option :value="-1">行号</option><option v-for="(name, index) in table.columns" :key="index" :value="index">{{ name }}</option></select></label>
      <label>纵轴 <select v-model="yColumns" multiple size="3"><option v-for="index in columns" :key="index" :value="index">{{ table.columns[index] }}</option></select></label>
      <label>图式 <select v-model="mode"><option value="lines">曲线</option><option value="markers">散点</option><option value="histogram">直方图</option></select></label>
      <button type="submit" class="rounded border px-2">绘制</button>
    </form>
    <div ref="target" class="min-h-[430px] flex-1 bg-white" aria-label="Plotly 数值图" />
    <p v-if="sampled" class="text-xs text-amber-700">当前为有界抽样／窗口，不代表完整数据。</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { numericCell, numericColumns, parseTable, parseArray, plainLabel, type PreviewTable, type PreviewArray } from './data';
import PreviewFrame from './PreviewFrame.vue';
import { loadBrowserLibrary } from './browserLibraries';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
const table = shallowRef<PreviewTable>(), xColumn = ref(-1), yColumns = ref<number[]>([]), mode = ref('lines');
const array = shallowRef<PreviewArray>(), variables = ref<string[]>([]), variable = ref(''), arrayKind = ref('series'), sourceShape = ref<number[]>([]), indices = ref<number[]>([]), strides = ref<number[]>([]);
const arrayFile = computed(() => /\.(npy|npz|mat)$/i.test(props.file.filename));
const sliceShape = computed(() => sourceShape.value.slice(0, Math.max(0, sourceShape.value.length - (arrayKind.value === 'series' ? 1 : 2))));
const columns = computed(() => table.value ? numericColumns(table.value) : []);
let plotly: typeof import('plotly.js-cartesian-dist-min').default | undefined;
async function draw() {
  if (!plotly || !target.value) return;
  if (array.value) {
    const values = array.value, rank = values.shape.length;
    const x = Array.from({ length: values.shape[rank - 1]! }, (_, index) => index * (strides.value[rank - 1] ?? 1));
    const traces = rank === 1 ? [{ type: 'scatter' as const, mode: 'lines' as const, x, y: values.values, connectgaps: false }] : [{ type: 'heatmap' as const, x, y: Array.from({ length: values.shape[0]! }, (_, index) => index * (strides.value[0] ?? 1)), z: Array.from({ length: values.shape[0]! }, (_, row) => values.values.slice(row * values.shape[1]!, (row + 1) * values.shape[1]!)), colorscale: 'Viridis' }];
    await plotly.react(target.value, traces, { margin: { l: 60, r: 30, b: 50, t: 30 }, xaxis: { title: { text: '原数组索引' } }, yaxis: { title: { text: rank === 1 ? '数值' : '原数组索引' } } }, { responsive: true, displaylogo: false, displayModeBar: false });
    return;
  }
  if (!table.value) return;
  const current = table.value;
  const traces = yColumns.value.slice(0, 8).map((index) => {
    const y = current.rows.map((row) => numericCell(row[index]));
    return mode.value === 'histogram' ? { type: 'histogram' as const, x: y, name: current.columns[index] } : { type: 'scatter' as const, mode: mode.value as 'lines' | 'markers', name: current.columns[index], x: current.rows.map((row, rowIndex) => { const value = row[xColumn.value]; return xColumn.value < 0 ? rowIndex : typeof value === 'string' ? plainLabel(value) : typeof value === 'boolean' ? Number(value) : value; }), y, connectgaps: false };
  });
  await plotly.react(target.value, traces, { margin: { l: 60, r: 20, b: 60, t: 30 }, autosize: true, showlegend: true, xaxis: { title: { text: xColumn.value < 0 ? '行号' : current.columns[xColumn.value] } } }, { responsive: true, displaylogo: false, displayModeBar: false, plotGlPixelRatio: 1 });
}
async function loadData() {
  const load = scope.begin(); loading.value = true; error.value = ''; table.value = undefined; array.value = undefined;
  try {
    const options = arrayFile.value ? { kind: arrayKind.value, ...(variable.value ? { variable: variable.value } : {}), indices: sliceShape.value.map((_, index) => indices.value[index] ?? 0) } : { kind: 'table', row_offset: 0, column_offset: 0 };
    const [result, library] = await Promise.all([requestPreview(props.file, props.plugin, options, load.signal), loadBrowserLibrary('plotly', load.signal)]);
    load.assertCurrent(); plotly = library;
    sampled.value = result.sampled === true;
    if (arrayFile.value) {
      array.value = parseArray(result.array);
      const choices = result.choices as Record<string, unknown> | undefined, selected = result.selected as Record<string, unknown> | undefined, metadata = result.metadata as Record<string, unknown> | undefined;
      variables.value = Array.isArray(choices?.variables) ? choices.variables.filter((value): value is string => typeof value === 'string').slice(0, 128) : [];
      if (typeof selected?.variable === 'string') variable.value = selected.variable;
      sourceShape.value = Array.isArray(metadata?.source_shape) ? metadata.source_shape as number[] : array.value.shape;
      strides.value = Array.isArray(metadata?.strides) ? metadata.strides as number[] : [];
    } else { table.value = parseTable(result.table); yColumns.value = numericColumns(table.value).slice(0, 1); xColumn.value = -1; }
    await nextTick(); load.assertCurrent();
    const element = target.value!; load.onDispose(() => { plotly?.purge(element); });
    await draw();
    if (typeof ResizeObserver !== 'undefined') { const resize = new ResizeObserver(() => { if (load.isCurrent()) void plotly?.Plots.resize(element); }); resize.observe(element); load.onDispose(() => resize.disconnect()); }
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '图表加载失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}
watch(() => [props.file.file_id, props.plugin.id], () => { variable.value = ''; variables.value = []; sourceShape.value = []; indices.value = []; void loadData(); }, { immediate: true });
</script>
