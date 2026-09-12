<template>
  <PreviewFrame class="plotly-preview" :loading="loading" :error="error" note="Plotly · 有界表格数值探索；空值保持缺失，布尔值按 false=0/true=1 绘制，不执行表格公式。">
    <form v-if="arrayFile" class="plotly-controls plotly-array-settings" @submit.prevent="loadData">
      <label v-if="variables.length" class="plotly-field">变量 <select v-model="variable" @change="indices = []; sourceShape = []"><option v-for="name in variables" :key="name">{{ name }}</option></select></label>
      <label class="plotly-field">视图 <select v-model="arrayKind" @change="indices = []"><option value="series">数值曲线</option><option value="heatmap">数组热图</option></select></label>
      <label v-for="(size, index) in sliceShape" :key="index" class="plotly-field">dim_{{ index }} <input v-model.number="indices[index]" type="number" min="0" :max="size - 1" step="1" /></label>
      <button type="submit" class="plotly-draw" :disabled="loading">读取变量／切片</button>
    </form>
    <form v-if="table" class="plotly-controls" @submit.prevent="draw">
      <div class="plotly-table-settings">
        <label class="plotly-field plotly-x-field">横轴 <select v-model.number="xColumn"><option :value="-1">行号</option><option v-for="(name, index) in table.columns" :key="index" :value="index">{{ name }}</option></select></label>
        <label class="plotly-field">图式 <select v-model="mode"><option value="lines">曲线</option><option value="markers">散点</option><option value="histogram">直方图</option></select></label>
        <button type="submit" class="plotly-draw" :disabled="loading || !columns.length">绘制</button>
      </div>
      <fieldset class="plotly-series">
        <legend>纵轴 <span>已选 {{ yColumns.length }} / 8 项</span></legend>
        <div v-if="columns.length" class="plotly-series-list">
          <label v-for="index in columns" :key="index" class="plotly-series-option" :title="table.columns[index]">
            <input v-model="yColumns" type="checkbox" :value="index" :disabled="yColumns.length >= 8 && !yColumns.includes(index)" />
            <span class="plotly-series-name">{{ table.columns[index] }}</span>
          </label>
        </div>
        <p v-else class="plotly-hint">当前预览中没有可绘制的数值列。</p>
        <p v-if="columns.length && !yColumns.length" class="plotly-hint">勾选数值列后，点击「绘制」更新图表。</p>
      </fieldset>
    </form>
    <div ref="target" class="plotly-chart" aria-label="Plotly 数值图" />
    <p v-if="sampled" class="plotly-sampling-note">当前为有界抽样／窗口，不代表完整数据。</p>
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
    const columnName = current.columns[index]!;
    const label = { name: columnName.length > 18 ? `#${index + 1} ${columnName.slice(0, 18)}…` : columnName, meta: { columnName }, hovertemplate: '%{meta.columnName}<br>x: %{x}<br>y: %{y}<extra></extra>' };
    return mode.value === 'histogram' ? { type: 'histogram' as const, x: y, ...label } : { type: 'scatter' as const, mode: mode.value as 'lines' | 'markers', ...label, x: current.rows.map((row, rowIndex) => { const value = row[xColumn.value]; return xColumn.value < 0 ? rowIndex : typeof value === 'string' ? plainLabel(value) : typeof value === 'boolean' ? Number(value) : value; }), y, connectgaps: false };
  });
  // Plotly 4 supports a scrolling legend height cap; upstream TS types lag this option.
  const legend: Partial<import('plotly.js').Legend> & { maxheight: number } = { orientation: 'h', x: 0, y: -0.3, xanchor: 'left', yanchor: 'top', maxheight: 88, font: { size: 11 } };
  await plotly.react(target.value, traces, { margin: { l: 52, r: 16, b: 60, t: 24 }, autosize: true, showlegend: true, legend, xaxis: { automargin: true, title: { text: xColumn.value < 0 ? '行号' : current.columns[xColumn.value], standoff: 12 } } }, { responsive: true, displaylogo: false, displayModeBar: false, plotGlPixelRatio: 1 });
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
watch([() => props.file.file_id, () => props.plugin.id], () => { variable.value = ''; variables.value = []; sourceShape.value = []; indices.value = []; void loadData(); }, { immediate: true });
</script>

<style scoped>
.plotly-preview {
  container: plotly-preview / inline-size;
  min-width: 0;
  gap: 12px;
}

/* Controls keep their natural height; short panels scroll instead of squeezing labels. */
.plotly-preview > :deep(p) {
  flex-shrink: 0;
  margin: 0;
  line-height: 1.6;
}

.plotly-controls {
  flex: 0 0 auto;
  min-width: 0;
  margin: 0;
  padding: 14px;
  border: 1px solid var(--border-main);
  border-radius: 10px;
  background: var(--background-white-main, #fff);
  font-size: 13px;
  line-height: 1.5;
}

.plotly-table-settings {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(100px, 160px) auto;
  align-items: end;
  gap: 12px;
}

.plotly-array-settings {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  align-items: end;
  gap: 12px;
}

.plotly-field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
  font-weight: 500;
}

.plotly-field select,
.plotly-field input {
  box-sizing: border-box;
  display: block;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  height: 38px;
  margin: 0;
  padding: 0 10px;
  border: 1px solid var(--border-main);
  border-radius: 6px;
  background: var(--background-white-main, #fff);
  color: var(--text-primary);
  font: inherit;
  font-weight: 400;
  text-overflow: ellipsis;
}

.plotly-draw {
  min-height: 38px;
  padding: 8px 20px;
  border: 1px solid #b5d8cc;
  border-radius: 6px;
  background: #edf7f2;
  color: #28654f;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}

.plotly-draw:hover:not(:disabled) { background: #e0f0e8; }
.plotly-draw:disabled { opacity: 0.5; cursor: not-allowed; }
.plotly-field select:focus-visible,
.plotly-field input:focus-visible,
.plotly-draw:focus-visible,
.plotly-series-option input:focus-visible {
  outline: 2px solid #398469;
  outline-offset: 2px;
}

.plotly-series {
  min-width: 0;
  margin: 14px 0 0;
  padding: 0;
  border: 0;
}

.plotly-series legend {
  width: 100%;
  margin-bottom: 8px;
  padding: 0;
  font-weight: 500;
}

.plotly-series legend span {
  margin-left: 8px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 400;
}

.plotly-series-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr));
  gap: 6px 10px;
  max-height: 126px;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 3px;
}

.plotly-series-option {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border-main);
  border-radius: 6px;
  cursor: pointer;
}

.plotly-series-option:has(:checked) {
  border-color: #b5d8cc;
  background: #f0f8f4;
  color: #28654f;
}

.plotly-series-option:has(:disabled) { opacity: 0.5; cursor: not-allowed; }
.plotly-series-option input {
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: #398469;
}

.plotly-series-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plotly-hint { margin: 8px 0 0; color: var(--text-secondary); font-size: 12px; }
.plotly-chart { flex: 1 0 360px; width: 100%; min-width: 0; min-height: 360px; overflow: hidden; border-radius: 8px; background: #fff; }
.plotly-sampling-note { color: #a45b16; font-size: 12px; }

@container plotly-preview (max-width: 520px) {
  .plotly-table-settings { grid-template-columns: minmax(0, 1fr) auto; }
  .plotly-x-field { grid-column: 1 / -1; }
  .plotly-controls { padding: 12px; }
}
</style>
