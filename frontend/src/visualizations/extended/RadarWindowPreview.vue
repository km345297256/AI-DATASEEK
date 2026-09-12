<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { RADAR_WARNING, parseRadarWindow, radarDisplay, validateRadarOptions, type RadarData, type RadarSelection } from './radarWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), plots = usePreviewLoad();
const catalog = shallowRef<RadarData>(), displayed = shallowRef<RadarData>(), target = ref<HTMLDivElement>();
const sweep = ref(1), quantity = ref('DBZH'), rayStart = ref(0), rayCount = ref(1), gateStart = ref(0), gateCount = ref(1), calibrated = ref(false), busy = ref(false), error = ref('');
let version: string | undefined, initializing = false;
const selectedSweep = computed(() => catalog.value?.sweeps.find(s => s.id === sweep.value));
const current = computed(() => displayed.value ?? catalog.value);
const selectedQuantity = computed(() => selectedSweep.value?.quantities.find(q => q.id === quantity.value));
const selection = computed((): RadarSelection => ({ sweep: sweep.value, quantity: quantity.value, ray_start: rayStart.value, ray_count: rayCount.value, gate_start: gateStart.value, gate_count: gateCount.value, decode: 'raw' }));
function clearWindow() { loads.begin(); plots.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
function cancel() { clearWindow(); }
async function inspect() {
  const request = loads.begin(); plots.begin(); catalog.value = displayed.value = undefined; version = undefined; busy.value = false; error.value = '';
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal); request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version)) throw new Error('雷达目录版本无效。');
    const data = parseRadarWindow('tree', result.payload, result.metadata, {});
    if (data.sourceBytes !== props.file.size || data.format !== props.file.filename.toLowerCase().split('.').pop()) throw new Error('雷达文件大小或格式已变化。');
    const initial = data.sweeps[0]!; initializing = true;
    sweep.value = initial.id; quantity.value = initial.quantities[0]!.id; rayStart.value = gateStart.value = 0;
    rayCount.value = Math.min(32, initial.nrays); gateCount.value = Math.min(64, initial.nbins); calibrated.value = false;
    version = result.version; catalog.value = data; initializing = false;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '雷达目录读取失败。'; }
  finally { initializing = false; if (request.isCurrent()) busy.value = false; }
}
async function draw(data: RadarData) {
  const plot = plots.begin(), view = radarDisplay(data, calibrated.value);
  try {
  const library = await loadBrowserLibrary('plotly', plot.signal); await nextTick(); plot.assertCurrent();
  if (!target.value) return;
  const element = document.createElement('div'); element.dataset.testid = 'radar-plot'; element.style.height = '420px'; target.value.replaceChildren(element);
  plot.onDispose(() => { library.purge(element); element.remove(); });
  const traces: import('plotly.js').Data[] = [
    { type: 'heatmap', x: view.x, y: view.y, z: view.z, zsmooth: false, connectgaps: false, colorscale: 'Viridis', colorbar: { title: { text: view.label } }, hoverongaps: false },
    { type: 'heatmap', x: view.x, y: view.y, z: view.flags, zmin: 1, zmax: 2, zsmooth: false, connectgaps: false, colorscale: [[0, '#9ca3af'], [.499, '#9ca3af'], [.5, '#93c5fd'], [1, '#93c5fd']], showscale: false, hoverongaps: false,
      text: view.flags.map(row => row.map(value => value === 1 ? 'nodata · 无数据' : value === 2 ? 'undetect · 低于探测阈值' : '')) as unknown as string[], customdata: view.raw,
      hovertemplate: '斜距门中心 %{x} m<br>射线索引 %{y}<br>%{text}<br>原始存储码 %{customdata}<extra></extra>' },
  ];
  await library.newPlot(element, traces, { margin: { l: 70, r: 95, t: 20, b: 65 }, xaxis: { title: { text: '斜距门中心 [m]（非地面距离）' } }, yaxis: { title: { text: '射线存储索引（非方位角）' }, autorange: 'reversed', dtick: 1 } }, { responsive: true, displayModeBar: false, displaylogo: false });
  if (!plot.isCurrent()) { library.purge(element); element.remove(); return; }
  if (typeof ResizeObserver !== 'undefined') { const resize = new ResizeObserver(() => { if (plot.isCurrent()) library.Plots.resize(element); }); resize.observe(element); plot.onDispose(() => resize.disconnect()); }
  } catch (reason) { if (plot.isCurrent()) throw reason; }
}
async function loadWindow() {
  if (!catalog.value || !version || busy.value || !props.plugin.enabled) return;
  let options: RadarSelection;
  try { options = validateRadarOptions('image', selection.value, catalog.value.sweeps) as RadarSelection; }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '雷达窗口无效。'; return; }
  const request = loads.begin(), pinned = version, initial = catalog.value;
  plots.begin(); displayed.value = undefined; busy.value = true; error.value = '';
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'image', version: pinned, ...options }, request.signal); request.assertCurrent();
    if (result.kind !== 'array' || result.version !== pinned) throw new Error('雷达窗口版本或类型已变化。');
    const data = parseRadarWindow('image', result.payload, result.metadata, options);
    if (data.sourceBytes !== props.file.size || data.format !== initial.format || JSON.stringify(data.sweeps) !== JSON.stringify(initial.sweeps)) throw new Error('雷达目录与返回窗口不一致。');
    displayed.value = data; await draw(data); request.assertCurrent();
  } catch (reason) { if (request.isCurrent()) { plots.begin(); displayed.value = undefined; error.value = reason instanceof Error ? reason.message : '雷达窗口读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([sweep, quantity, rayStart, rayCount, gateStart, gateCount], () => { if (!initializing) clearWindow(); }, { flush: 'sync' });
watch(calibrated, () => { if (displayed.value) void draw(displayed.value).catch(() => { if (displayed.value) error.value = '本地雷达显示更新失败。'; }); }, { flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], inspect, { immediate: true, flush: 'sync' });
onScopeDispose(() => { catalog.value = displayed.value = undefined; });
</script>

<template>
  <section class="radar-preview">
    <p class="radar-warning">{{ RADAR_WARNING }}</p>
    <button :disabled="busy" @click="inspect">重新读取雷达目录</button>
    <div v-if="catalog" class="radar-controls">
      <label>扫描 <select v-model.number="sweep" aria-label="雷达扫描"><option v-for="s in catalog.sweeps" :key="s.id" :value="s.id">{{ s.id }} · 仰角 {{ s.elevation }}° · {{ s.nrays }} 射线 × {{ s.nbins }} 门</option></select></label>
      <label>物理量 <select v-model="quantity" aria-label="雷达物理量"><option v-for="q in selectedSweep?.quantities" :key="q.id" :value="q.id">{{ q.id }}</option></select></label>
      <div class="radar-row"><label>射线起点 <input v-model.number="rayStart" type="number" min="0" aria-label="雷达射线起点"></label><label>射线数 <input v-model.number="rayCount" type="number" min="1" max="128" aria-label="雷达射线数"></label><label>距离门起点 <input v-model.number="gateStart" type="number" min="0" aria-label="雷达距离门起点"></label><label>距离门数 <input v-model.number="gateCount" type="number" min="1" max="128" aria-label="雷达距离门数"></label></div>
      <button :disabled="busy || !selectedQuantity" @click="loadWindow">读取雷达窗口</button>
      <p v-if="selectedSweep">a1gate={{ selectedSweep.a1gate }} 为采集起始射线索引，不用它旋转行序；首门起点 {{ selectedSweep.rstart_m }} m，门间距 {{ selectedSweep.rscale_m }} m。</p>
    </div>
    <div v-if="displayed && selectedQuantity" class="radar-display">
      <label><input v-model="calibrated" :disabled="busy" type="checkbox" aria-label="应用雷达声明标定">手动应用文件声明的 gain/offset（仅本地，缓存窗口）</label>
      <p>{{ calibrated ? '标定值' : '存储码：无物理单位' }}；文件声明：value = {{ selectedQuantity.offset }} + {{ selectedQuantity.gain }} × raw，单位 {{ selectedQuantity.unit }}。保留码先分类，不代入公式。</p>
      <p><span class="nodata">■</span> nodata / 无数据：{{ displayed.nodata }}；<span class="undetect">■</span> undetect / 低于探测阈值：{{ displayed.undetect }}。不补零、不插值、不据此反演降雨。</p>
    </div>
    <p v-if="current">读取 {{ current.readBytes }} / {{ current.sourceBytes }} 字节，{{ current.reads }} 次范围请求。HDF5 元信息会预读缓存，小文件可能全部取回；不自动解码整扫描。</p>
    <p v-if="error" role="alert">{{ error }}</p><button v-if="busy" @click="cancel">取消读取</button>
    <div ref="target" class="radar-target" />
  </section>
</template>

<style scoped>
.radar-preview{padding:16px;overflow:auto;color:#334155;font-size:13px}.radar-warning{padding:12px;border:1px solid #fbbf24;background:#fffbeb;margin-bottom:12px}.radar-controls{padding:12px;border:1px solid #dbe2ea;border-radius:6px;margin:12px 0}.radar-controls>label{margin-right:16px}.radar-row{display:flex;flex-wrap:wrap;gap:14px;margin:12px 0}.radar-row input{width:76px}button,input,select{border:1px solid #cbd5e1;border-radius:4px;padding:5px;background:white}button:disabled{opacity:.45}p{line-height:1.6;margin:10px 0}.radar-target{width:100%;min-width:240px}.nodata{color:#9ca3af}.undetect{color:#93c5fd}[role=alert]{color:#b91c1c}
</style>
