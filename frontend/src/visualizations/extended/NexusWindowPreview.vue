<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { NEXUS_WARNING, parseNexusWindow, validateNexusSelection, type NexusData, type NexusSlice } from './nexusWindowData';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), plotScope = usePreviewLoad();
const catalog = shallowRef<NexusData>(), displayed = shallowRef<NexusData>();
const selectedId = ref(''), slices = ref<NexusSlice[]>([]), busy = ref(false), error = ref(''), target = ref<HTMLDivElement>();
const signal = computed(() => catalog.value?.signals.find(v => v.id === selectedId.value));
const current = computed(() => displayed.value ?? catalog.value);
let version: string | undefined;
const axisTitle = (axis: { label: string; unit: string | null }) => axis.label + (axis.unit ? ` [${axis.unit}]` : '（单位未声明）');
function clearWindow() { scope.begin(); plotScope.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
async function inspect() {
  const request = scope.begin(); plotScope.begin(); catalog.value = undefined; displayed.value = undefined; version = undefined;
  selectedId.value = ''; slices.value = []; busy.value = false; error.value = '';
  // Flush selection watchers before creating the actual tree request.
  await nextTick();
  if (!request.isCurrent() || !props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version)) throw new Error('NXdata 目录或文件版本无效。');
    const parsed = parseNexusWindow('tree', result.payload, result.metadata);
    if (parsed.sourceBytes !== props.file.size) throw new Error('NXdata 源文件大小已变化，请重新打开预览。');
    catalog.value = parsed; version = result.version;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'NXdata 目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(data: NexusData) {
  const plot = plotScope.begin(), array = data.array;
  if (!array) return;
  const library = await loadBrowserLibrary('plotly', plot.signal);
  await nextTick(); plot.assertCurrent();
  if (!target.value) return;
  const element = document.createElement('div'); element.dataset.testid = 'nexus-plot'; element.style.height = '420px'; target.value.replaceChildren(element);
  plot.onDispose(() => { library.purge(element); element.remove(); });
  const chosen = data.signals.find(v => v.id === data.selected?.nxdata)!;
  let traces: import('plotly.js').Data[];
  if (array.shape.length === 1) {
    traces = [{ type: 'scatter', mode: array.shape[0] === 1 ? 'markers' : 'lines+markers', x: data.axes[0]!.values, y: array.values, connectgaps: false,
      name: chosen.signal, ...(data.errors ? { error_y: { type: 'data' as const, array: data.errors, visible: true } } : {}) }];
  } else {
    const width = array.shape[1]!;
    traces = [{ type: 'heatmap', x: data.axes[1]!.values, y: data.axes[0]!.values,
      z: Array.from({ length: array.shape[0]! }, (_, row) => array.values.slice(row * width, (row + 1) * width)),
      zsmooth: false, connectgaps: false, colorscale: 'Viridis', colorbar: { title: { text: chosen.unit ?? '原始值' } },
      ...(data.errors ? { customdata: Array.from({ length: array.shape[0]! }, (_, row) => data.errors!.slice(row * width, (row + 1) * width)), hovertemplate: 'x=%{x}<br>y=%{y}<br>原值=%{z}<br>标准差=%{customdata}<extra></extra>' } : {}) }];
  }
  await library.newPlot(element, traces, { margin: { l: 85, r: 55, t: 25, b: 60 },
    xaxis: { title: { text: axisTitle(data.axes[array.shape.length - 1]!) } },
    yaxis: { title: { text: array.shape.length === 1 ? axisTitle({ label: chosen.signal, unit: chosen.unit }) : axisTitle(data.axes[0]!) },
      ...(array.shape.length === 2 && data.axes[0]!.source === 'index' ? { autorange: 'reversed' as const } : {}) } },
    { responsive: true, displaylogo: false, displayModeBar: false });
  if (!plot.isCurrent()) { library.purge(element); element.remove(); return; }
  if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (plot.isCurrent()) void library.Plots.resize(element); }); observer.observe(element); plot.onDispose(() => observer.disconnect()); }
}
async function loadWindow() {
  const request = scope.begin(); plotScope.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !signal.value || !version) throw new Error('请先读取目录并选择 NXdata 信号。');
    const chosen = signal.value, kind = chosen.shape.length === 1 ? 'series' : 'image';
    const selection = validateNexusSelection(kind, { nxdata: selectedId.value, selection: slices.value }, chosen);
    const pinned = version;
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind, version: pinned, ...selection }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'array' || result.version !== pinned) throw new Error('NXdata 文件版本已变化，请重新读取目录。');
    const data = parseNexusWindow(kind, result.payload, result.metadata, selection);
    if (data.sourceBytes !== catalog.value?.sourceBytes || JSON.stringify(data.signals.find(v => v.id === chosen.id)) !== JSON.stringify(chosen)) throw new Error('NXdata 信号或坐标与已检查目录不一致。');
    await draw(data); request.assertCurrent(); displayed.value = data;
  } catch (reason) { if (request.isCurrent()) { plotScope.begin(); error.value = reason instanceof Error ? reason.message : 'NXdata 窗口读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch(selectedId, () => {
  if (!catalog.value) return;
  clearWindow();
  slices.value = (signal.value?.shape ?? []).map(size => ({ start: 0, stop: Math.min(size, signal.value?.shape.length === 1 ? 1024 : 64), step: 1 }));
});
watch(slices, () => { if (catalog.value) clearWindow(); }, { deep: true });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true });
</script>

<template>
  <section class="nexus-preview">
    <p role="note">{{ NEXUS_WARNING }}</p>
    <p class="notice">安全试点：仅 HDF5 中的现代 NXdata，1D/2D 默认信号及其标准差、固定长度字符串元信息、单调的 1D 中心点坐标和同文件硬链接。不显示辅助信号或坐标误差；不支持可变长字符串、边界坐标、多维轴或外部链接。单位仅为文件声明，不代表已完成标定；无兼容信号时请自行选择 HDF5 原始数组查看器。</p>
    <button class="reload" :disabled="busy" @click="inspect">重新读取 NXdata 目录</button>
    <form v-if="catalog" @submit.prevent="loadWindow">
      <label>NXdata 信号<select v-model="selectedId" aria-label="NXdata 信号" :disabled="busy"><option value="">请选择信号</option><option v-for="entry in catalog.signals" :key="entry.id" :value="entry.id">{{ entry.label }} · {{ entry.signal }} · {{ entry.shape.join(' × ') }} · {{ entry.dtype }}</option></select></label>
      <div v-for="(slice, dimension) in slices" :key="dimension" class="dimension">
        <span>{{ signal?.axes[dimension]?.label }} · {{ signal?.shape[dimension] }}</span>
        <label>起点<input v-model.number="slice.start" :aria-label="`NX 维度 ${dimension} 起点`" type="number" min="0" step="1" :disabled="busy" /></label>
        <label>终点（不含）<input v-model.number="slice.stop" :aria-label="`NX 维度 ${dimension} 终点`" type="number" min="1" step="1" :disabled="busy" /></label>
        <label>步长<input v-model.number="slice.step" :aria-label="`NX 维度 ${dimension} 步长`" type="number" min="1" step="1" :disabled="busy" /></label>
      </div>
      <button type="submit" :disabled="busy || !signal">读取 NXdata 窗口</button>
    </form>
    <p v-if="catalog && !catalog.signals.length" class="notice" data-testid="nexus-empty">没有符合当前安全方言的 NXdata 信号。文件可能使用可变长元信息、旧版 signal 声明或不支持的轴/存储结构；原始数组预览仍可由您手动选择。</p>
    <p v-else-if="catalog && !displayed && !busy" class="notice">目录已就绪，尚未读取信号数值。请选择信号与范围后点击读取。</p>
    <p v-if="busy" role="status">正在读取授权的 NXdata 范围…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="current" class="notice" data-testid="nexus-stats">读取 {{ current.readBytes }} / {{ current.sourceBytes }} 字节，{{ current.reads }} 次范围请求。{{ current.skipped ? `${current.skipped} 个组的元信息或信号不支持。` : '' }}{{ current.truncated ? '目录达到上限，仅显示部分组。' : '' }}</p>
    <p v-if="displayed" class="notice">横纵轴为文件声明的坐标（未声明时使用索引）；{{ displayed.errors ? '误差为文件提供的标准差，曲线显示误差条，图像悬停显示标准差。' : '文件未提供符合规范的标准差，不估算误差。' }}</p>
    <div ref="target" class="plot" aria-label="NXdata 科学数值图" />
  </section>
</template>

<style scoped>
.nexus-preview{display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;flex:1;padding:16px;font-size:13px}p{margin:0;line-height:1.65}.notice{font-size:12px;color:#667085}form{display:flex;flex-direction:column;gap:10px;padding:12px;border:1px solid #d0d5dd;border-radius:6px}label{display:flex;gap:6px;align-items:center}select,input,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:85px}select{max-width:100%}.dimension{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.reload,button[type=submit]{align-self:flex-start}button:disabled{opacity:.5}[role=alert]{color:#b42318}.plot{width:100%;min-height:0;flex-shrink:0}
</style>
