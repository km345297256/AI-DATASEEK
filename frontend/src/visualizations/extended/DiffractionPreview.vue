<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">一维衍射／散射扫描。保留文件坐标、单位与存储强度，不按计数时间归一化、不换算单位、不扣背景，不进行结构求解或 Rietveld 精修。误差仅按显式声明显示，不推断标准差。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadScan">
      <p class="mb-2" data-testid="diffraction-catalog">{{ catalog.dialect }} · {{ catalog.scans.length }} 个扫描 · 合计 {{ catalog.totalPoints.toLocaleString() }} 点。有界读取整个 XML，不是范围读取。</p>
      <div class="flex flex-wrap items-center gap-3"><label>选择扫描 <select v-model.number="scanId" aria-label="衍射扫描" class="ml-2 rounded border p-1" :disabled="busy"><option v-for="s in catalog.scans" :key="s.id" :value="s.id">{{ s.label }} · {{ s.points }} 点 · {{ s.x_quantity }} ({{ s.x_unit }})</option></select></label>
        <button type="submit" class="rounded border px-3 py-1" :disabled="busy">绘制所选扫描</button>
      </div>
      <p v-if="selected" class="mt-2 text-xs text-gray-600" data-testid="diffraction-semantics">{{ selected.axis_mode === 'linear-declared' ? '坐标按文件明确起止位置与点数展开等步长。' : '逐点显式坐标，保留非等步长和原顺序。' }} 强度字段 {{ selected.y_quantity }} ({{ selected.y_unit }})。{{ selected.y_quantity === 'intensities' ? 'XRDML intensities 可能已由仪器进行衰减修正，本插件不再次修正。' : '仅使用文件存储值，不应用额外校正。' }}</p>
      <p v-if="selected?.x_error || selected?.y_error" class="mt-2 text-xs text-gray-600" data-testid="diffraction-errors">{{ selected.x_error ? '横向' : '' }}{{ selected.y_error ? '纵向' : '' }}误差线：文件声明的不确定度；分布和标准差倍数未指定。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在检查授权文件与扫描…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <div ref="target" data-testid="diffraction-plot" class="min-w-0 w-full" aria-label="衍射散射曲线" />
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { displayError } from './domains/lifecycle';
import { diffractionScansEqual, parseDiffractionData, validateDiffractionSelection, type DiffractionData } from './diffractionData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), catalog = shallowRef<DiffractionData>(), data = shallowRef<DiffractionData>();
const target = ref<HTMLDivElement>(), scanId = ref(0), busy = ref(false), error = ref('');
const selected = computed(() => catalog.value?.scans[scanId.value]);
let version: string | undefined;
async function inspect() {
  const load = scope.begin(); catalog.value = undefined; data.value = undefined; version = undefined; error.value = ''; busy.value = false; scanId.value = 0;
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, load.signal); load.assertCurrent();
    catalog.value = parseDiffractionData(result, 'tree', {}, props.file.size, props.file.filename); version = result.version;
  } catch (reason) { if (load.isCurrent()) error.value = displayError(reason); }
  finally { if (load.isCurrent()) busy.value = false; }
}
async function loadScan() {
  if (busy.value) return;
  const load = scope.begin(); data.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !catalog.value || !version) throw new Error('请先读取有效扫描目录。');
    const options = validateDiffractionSelection(scanId.value, catalog.value.scans.length), pinned = version, expected = catalog.value;
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series', version: pinned, ...options }, load.signal); load.assertCurrent();
    if (result.version !== pinned) throw new Error('扫描文件版本已变化，请重新打开预览。');
    const parsed = parseDiffractionData(result, 'series', options, props.file.size, props.file.filename);
    if (!diffractionScansEqual(parsed.scans, expected.scans) || parsed.sourceBytes !== expected.sourceBytes || parsed.dialect !== expected.dialect) throw new Error('曲线与已检查的扫描目录不一致。');
    const trace = parsed.trace!, scan = parsed.scans[options.scan]!;
    const plotly = await loadBrowserLibrary('plotly', load.signal); await nextTick(); load.assertCurrent();
    if (!target.value) throw new Error('曲线绘图区已关闭。');
    const element = document.createElement('div'); element.style.height = '420px'; target.value.replaceChildren(element);
    load.onDispose(() => { plotly.purge(element); element.remove(); });
    const series: Partial<import('plotly.js').PlotData> = { type: 'scatter', mode: 'lines+markers', x: trace.x, y: trace.y, name: scan.label, connectgaps: false,
      marker: { size: 4 }, ...(trace.x_error ? { error_x: { type: 'data', array: trace.x_error, visible: true } } : {}),
      ...(trace.y_error ? { error_y: { type: 'data', array: trace.y_error, visible: true } } : {}) };
    await plotly.newPlot(element, [series], { margin: { l: 85, r: 35, t: 30, b: 65 }, showlegend: false,
      xaxis: { title: { text: `${scan.x_quantity} (${scan.x_unit})` }, type: 'linear' },
      yaxis: { title: { text: `${scan.y_quantity} (${scan.y_unit}) · 文件存储值` }, type: 'linear' } },
    { responsive: true, displaylogo: false, displayModeBar: false });
    if (!load.isCurrent()) { plotly.purge(element); element.remove(); return; }
    data.value = parsed;
    if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (load.isCurrent()) void plotly.Plots.resize(element); }); observer.observe(element); load.onDispose(() => observer.disconnect()); }
  } catch (reason) { if (load.isCurrent()) error.value = displayError(reason); }
  finally { if (load.isCurrent()) busy.value = false; }
}
watch(scanId, () => { if (catalog.value) { scope.begin(); data.value = undefined; error.value = ''; busy.value = false; } }, { flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true, flush: 'sync' });
onScopeDispose(() => { catalog.value = undefined; data.value = undefined; version = undefined; });
</script>
