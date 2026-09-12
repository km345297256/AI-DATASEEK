<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">FCS 3.0／3.1 单数据集 list mode 试点。只显示存储原值，不作位掩码、PnE 反对数、PnG 增益、标定、时间换算、补偿、门控或抽样。原值坐标轴不是标定后的物理单位；直方图仅统计明确选择的事件窗。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadWindow">
      <p class="mb-3">{{ catalog.totalEvents.toLocaleString() }} 个事件 · {{ catalog.channels.length }} 通道 · FCS {{ catalog.fcsVersion }} / {{ catalog.datatype }} / {{ catalog.byteOrder }} endian</p>
      <div class="flex flex-wrap items-center gap-3"><label>视图 <select v-model="view" aria-label="FCS 视图" class="rounded border p-1" :disabled="busy"><option value="scatter" :disabled="catalog.channels.length < 2">双通道原值散点</option><option value="histogram">单通道原值直方图</option></select></label>
        <label>X 通道 <select v-model.number="xChannel" aria-label="FCS X 通道" class="max-w-64 rounded border p-1" :disabled="busy"><option v-for="c in catalog.channels" :key="c.id" :value="c.id">{{ c.id }} · {{ c.name }}</option></select></label>
        <label v-if="view === 'scatter'">Y 通道 <select v-model.number="yChannel" aria-label="FCS Y 通道" class="max-w-64 rounded border p-1" :disabled="busy"><option v-for="c in catalog.channels" :key="c.id" :value="c.id">{{ c.id }} · {{ c.name }}</option></select></label>
        <label v-else>直方图箱数 <input v-model.number="bins" aria-label="直方图箱数" type="number" min="1" max="128" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-3"><label>起始事件（从 0 开始） <input v-model.number="eventOffset" aria-label="起始事件" type="number" min="0" :max="catalog.totalEvents - 1" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label>
        <label>事件数 <input v-model.number="eventCount" aria-label="事件数" type="number" min="1" max="8192" step="1" class="w-24 rounded border p-1" :disabled="busy" /></label><button type="submit" class="rounded border px-3 py-1" :disabled="busy">读取事件窗口</button></div>
      <p class="mt-3 text-xs text-gray-500">打开目录不自动读取 events。每窗 ≤8,192 事件／16,384 原值。DATA 按事件交错：读取选定事件行的完整字节，再仅解码所选通道；不是只读取所选列。浮点 NaN/Inf 保留事件位置并留空，散点不连接线。</p>
      <div class="mt-3 space-y-1 text-xs text-gray-600" data-testid="fcs-channel-metadata"><p v-for="c in selectedChannels" :key="c.id">{{ c.name }}{{ c.stain ? ` / ${c.stain}` : '' }}：{{ c.bits }} bit，PnR={{ c.range }}，PnE={{ c.exponent.join(',') }}，PnG={{ c.gain ?? '未声明' }}。{{ c.calibration ? `标定声明：${c.calibration.factor} ${c.calibration.unit}/scale unit（未应用）` : '未声明标定单位' }}。{{ c.display ? `建议显示：${c.display.scale} ${c.display.values.join(',')}（未应用）` : '' }}</p><p v-if="catalog.timestep !== null">TIMESTEP={{ catalog.timestep }} 秒／时间通道单位（未换算）。</p></div>
    </form>
    <details v-if="catalog" class="my-3 rounded border p-3 text-xs"><summary>补偿声明（未应用）：{{ catalog.compensation.declarations.length ? catalog.compensation.declarations.join('、') : '未发现已识别声明；不代表此前未被处理' }}</summary><p class="mt-2">不推断文件是否已在其他软件处理。只解释标准 $SPILLOVER；其他识别到的声明仅报告存在，不自动读取外部补偿资源。</p>
      <div v-if="catalog.compensation.spillover" class="mt-2 max-h-40 overflow-auto"><table aria-label="FCS 已声明 spillover 矩阵" class="border-collapse"><thead><tr><th class="border p-1">源 → 目标</th><th v-for="id in catalog.compensation.spillover.channels" :key="id" class="border p-1">{{ catalog.channels[id]!.name }}</th></tr></thead><tbody><tr v-for="(row, i) in catalog.compensation.spillover.matrix" :key="i"><th class="border p-1">{{ catalog.channels[catalog.compensation.spillover.channels[i]!]!.name }}</th><td v-for="(n, j) in row" :key="j" class="border p-1">{{ n }}</td></tr></tbody></table></div>
    </details>
    <p v-if="busy" role="status" class="py-3">正在读取授权的 FCS 事件字节窗口…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="current" data-testid="fcs-stats" class="my-3 text-xs text-gray-500">本次读取 {{ current.readBytes.toLocaleString() }} / 源 {{ current.sourceBytes.toLocaleString() }} 字节，{{ current.reads }} 次范围请求；事件行 {{ current.scannedBytes.toLocaleString() }} 字节，所选原值解码 {{ current.decodedBytes.toLocaleString() }} 字节。{{ data ? `可绘制 ${current.plottable} 个事件，非有限原值 ${current.nonfinite} 个。` : '仅检查结构，尚未读取事件数据。' }}</p>
    <p v-if="histogram" data-testid="fcs-histogram-summary" class="mb-2 text-xs text-gray-500">当前窗直方图计数 {{ histogram.counts.reduce((a, b) => a + b, 0) }}；忽略空值 {{ histogram.missing }}。{{ histogram.constant ? '有限原值全部相同：显示该原值的单一计数，不虚构数值区间。' : '等宽箱左闭右开，最后一箱含右端点。' }}</p>
    <div v-if="data" ref="target" aria-label="FCS 原值图" class="min-h-[400px] shrink-0 w-full" />
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { parseFcsWindow, validateFcsSelection, fcsCatalogMatches, fcsHistogram, type FcsData } from './fcsWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), catalog = shallowRef<FcsData>(), data = shallowRef<FcsData>(), histogram = shallowRef<ReturnType<typeof fcsHistogram>>();
const view = ref<'scatter' | 'histogram'>('scatter'), xChannel = ref(0), yChannel = ref(1), eventOffset = ref(0), eventCount = ref(1), bins = ref(32), busy = ref(false), error = ref(''), target = ref<HTMLDivElement>();
const current = computed(() => data.value ?? catalog.value);
const selectedChannels = computed(() => catalog.value?.channels.filter(c => c.id === xChannel.value || view.value === 'scatter' && c.id === yChannel.value) ?? []);
let version: string | undefined;
async function inspect() {
  const request = scope.begin(); catalog.value = undefined; data.value = undefined; histogram.value = undefined; version = undefined; busy.value = false; error.value = '';
  view.value = 'scatter'; xChannel.value = 0; yChannel.value = 1; eventOffset.value = 0; eventCount.value = 1; bins.value = 32;
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal); request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version) || props.file.filename.split('.').pop()?.toLowerCase() !== 'fcs') throw new Error('FCS 文件结构或版本无效。');
    const parsed = parseFcsWindow('tree', result.payload, result.metadata);
    if (typeof props.file.size === 'number' && props.file.size > 0 && parsed.sourceBytes !== props.file.size) throw new Error('返回目录与当前 FCS 文件大小不一致。');
    catalog.value = parsed; version = result.version; eventCount.value = Math.min(1024, parsed.totalEvents);
    if (parsed.channels.length === 1) view.value = 'histogram';
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'FCS 目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function loadWindow() {
  const request = scope.begin(); data.value = undefined; histogram.value = undefined; busy.value = true; error.value = '';
  try {
    if (!props.plugin.enabled || !catalog.value || !version) throw new Error('请先读取 FCS 目录。');
    const selected = validateFcsSelection({ view: view.value, channels: view.value === 'scatter' ? [xChannel.value, yChannel.value] : [xChannel.value], event_offset: eventOffset.value, event_count: eventCount.value, ...(view.value === 'histogram' ? { bins: bins.value } : {}) }, catalog.value);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series', version, ...selected }, request.signal); request.assertCurrent();
    if (result.kind !== 'series' || result.version !== version) throw new Error('FCS 文件版本已变化，请重新打开。');
    const parsed = parseFcsWindow('series', result.payload, result.metadata, selected);
    if (!fcsCatalogMatches(parsed, catalog.value)) throw new Error('返回事件与已检查的 FCS 目录不一致。');
    if (selected.view === 'histogram') histogram.value = fcsHistogram(parsed.traces[0]!.y, selected.bins!);
    data.value = parsed;
    // Preserve all raw values in the validated payload, but do not force Plotly to
    // overflow its axis arithmetic or silently rescale extreme coordinates.
    if (parsed.traces.some(t => t.y.some(v => v !== null && Math.abs(v) > 1e100))) throw new Error('所选原值过大，当前线性图不能可靠显示；未截断或变换原值，请选择其他事件窗。');
    const library = await loadBrowserLibrary('plotly', request.signal); await nextTick(); request.assertCurrent(); if (!target.value) return;
    const element = document.createElement('div'); element.style.height = '400px'; target.value.replaceChildren(element);
    request.onDispose(() => { library.purge(element); element.remove(); });
    const first = parsed.traces[0]!, h = histogram.value;
    const trace: Partial<import('plotly.js').PlotData> = selected.view === 'scatter'
      ? { type: 'scatter', mode: 'markers', x: first.y, y: parsed.traces[1]!.y, customdata: first.x, connectgaps: false, marker: { size: 5, opacity: .65 }, hovertemplate: '事件 %{customdata}<br>x=%{x}<br>y=%{y}<extra></extra>' }
      : { type: 'bar', x: h!.centers, y: h!.counts, hovertemplate: '原值箱中心 %{x}<br>窗口事件数 %{y}<extra></extra>' };
    await library.newPlot(element, [trace], { margin: { l: 95, r: 30, t: 20, b: 80 }, showlegend: false,
      xaxis: { title: { text: first.label + '（存储原值，非标定单位）' }, type: 'linear', automargin: true },
      yaxis: { title: { text: selected.view === 'scatter' ? parsed.traces[1]!.label + '（存储原值，非标定单位）' : '当前事件窗内计数' }, type: 'linear', automargin: true } }, { responsive: true, displaylogo: false, displayModeBar: false });
    if (!request.isCurrent()) { library.purge(element); element.remove(); return; }
    if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (request.isCurrent()) void library.Plots.resize(element); }); observer.observe(element); request.onDispose(() => observer.disconnect()); }
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'FCS 事件窗口读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([view, xChannel, yChannel, eventOffset, eventCount, bins], () => { if (catalog.value) { scope.begin(); data.value = undefined; histogram.value = undefined; busy.value = false; error.value = ''; } }, { flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true, flush: 'sync' });
</script>
