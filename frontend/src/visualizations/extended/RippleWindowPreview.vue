<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { RIPPLE_WARNING, parseRippleWindow, validateRippleOptions, type RippleData, type RippleKind } from './rippleWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), plots = usePreviewLoad();
const catalog = shallowRef<RippleData>(), displayed = shallowRef<RippleData>(), target = ref<HTMLDivElement>();
const view = ref<'image' | 'series'>('image'), x = ref(0), y = ref(0), channel = ref(0), width = ref(16), height = ref(16), channelStart = ref(0), channelCount = ref(1), busy = ref(false), error = ref('');
let version: string | undefined;
const current = computed(() => displayed.value ?? catalog.value);
const selection = computed(() => view.value === 'image' ? { channel: channel.value, x: x.value, y: y.value, width: width.value, height: height.value } : { x: x.value, y: y.value, channel_start: channelStart.value, channel_count: channelCount.value });
function clearWindow() { scope.begin(); plots.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
async function inspect() {
  const request = scope.begin(); plots.begin(); catalog.value = undefined; displayed.value = undefined; version = undefined; busy.value = false; error.value = '';
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal); request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version)) throw new Error('Ripple 配对目录无效。');
    const data = parseRippleWindow('tree', result.payload, result.metadata);
    if (data.headerBytes !== props.file.size) throw new Error('头文件大小已变化，请重新打开。');
    x.value = y.value = channel.value = channelStart.value = 0; width.value = Math.min(16, data.cube.width); height.value = Math.min(16, data.cube.height); channelCount.value = Math.min(2048, data.cube.depth);
    version = result.version; catalog.value = data;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'Ripple 结构读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(data: RippleData, kind: RippleKind) {
  const plot = plots.begin(), a = data.array!;
  const library = await loadBrowserLibrary('plotly', plot.signal); await nextTick(); plot.assertCurrent();
  if (!target.value) return;
  const element = document.createElement('div'); element.dataset.testid = 'ripple-plot'; element.style.height = '420px'; target.value.replaceChildren(element);
  plot.onDispose(() => { library.purge(element); element.remove(); });
  const traces: import('plotly.js').Data[] = kind === 'image' ? [{ type: 'heatmap', x: data.axes[1]!.values, y: data.axes[0]!.values, z: Array.from({ length: a.shape[0]! }, (_, i) => a.values.slice(i*a.shape[1]!, (i+1)*a.shape[1]!)), zsmooth: false, connectgaps: false, colorscale: 'Viridis', colorbar: { title: { text: '原始值' } } }] : [{ type: 'scatter', mode: 'lines+markers', x: data.axes[0]!.values, y: a.values, connectgaps: false }];
  const axis = data.axes[kind === 'image' ? 1 : 0]!;
  await library.newPlot(element, traces, { margin: { l: 70, r: 45, t: 20, b: 65 }, xaxis: { title: { text: axis.label+(axis.unit ? ` [${axis.unit}]` : '') } }, yaxis: { title: { text: kind === 'image' ? data.axes[0]!.label+(data.axes[0]!.unit ? ` [${data.axes[0]!.unit}]` : '') : '原始值（未校正）' }, ...(kind === 'image' && data.cube.axes[0]!.calibration === 'index' ? { autorange: 'reversed' as const } : {}) } }, { responsive: true, displayModeBar: false, displaylogo: false });
  if (!plot.isCurrent()) { library.purge(element); element.remove(); return; }
  if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (plot.isCurrent()) void library.Plots.resize(element); }); observer.observe(element); plot.onDispose(() => observer.disconnect()); }
}
async function loadWindow() {
  const request = scope.begin(); plots.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    const initial = catalog.value, pinned = version, kind = view.value;
    if (!props.plugin.enabled || !initial || !pinned) throw new Error('请先读取配对结构。');
    const options = validateRippleOptions(kind, selection.value, initial.cube);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind, version: pinned, ...options }, request.signal); request.assertCurrent();
    if (result.kind !== 'array' || result.version !== pinned) throw new Error('配对文件版本已变化，请重新读取结构。');
    const data = parseRippleWindow(kind, result.payload, result.metadata, options);
    if (data.sourceBytes !== initial.sourceBytes || data.headerBytes !== initial.headerBytes || JSON.stringify(data.cube) !== JSON.stringify(initial.cube)) throw new Error('Ripple 结构与原目录不一致。');
    await draw(data, kind); request.assertCurrent(); displayed.value = data;
  } catch (reason) { if (request.isCurrent()) { plots.begin(); error.value = reason instanceof Error ? reason.message : '窗口读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([view, x, y, channel, width, height, channelStart, channelCount], () => { if (catalog.value) clearWindow(); }, { flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true, flush: 'sync' });
</script>
<template><section class="ripple-preview">
  <p role="note">{{ RIPPLE_WARNING }}</p>
  <p class="notice">从已登记数据集内的 .rpl 打开，需同目录、同名且唯一的 .raw。首版支持 record-by=vector 的无压缩谱像；不支持 Gatan DM3/DM4、Velox EMD 或完整 HyperSpy 工程。</p>
  <button :disabled="busy || !plugin.enabled" @click="inspect">重新读取配对结构</button>
  <form v-if="catalog" @submit.prevent="loadWindow">
    <p>{{ catalog.cube.width }} 列 × {{ catalog.cube.height }} 行 × {{ catalog.cube.depth }} 通道 · {{ catalog.cube.signal_type }}</p>
    <p class="notice">信号类型 {{ catalog.cube.signal_type }}；值未做背景扣除或定量。</p>
    <p v-for="axis in catalog.cube.axes" :key="axis.label" class="notice">{{ axis.label }}：{{ axis.calibration === 'index' ? '索引（无标定声明）' : `原点 ${axis.origin} · 步长 ${axis.scale} · ${axis.unit ?? '单位未声明'}` }}{{ axis.origin_defaulted && axis.calibration !== 'index' ? '；原点为格式默认 0' : '' }}</p>
    <label>视图<select v-model="view" aria-label="Ripple 视图"><option value="image">单通道区域</option><option value="series">像元光谱</option></select></label>
    <div class="fields"><label>列<input v-model.number="x" aria-label="Ripple 列" type="number" min="0" /></label><label>行<input v-model.number="y" aria-label="Ripple 行" type="number" min="0" /></label>
      <template v-if="view === 'image'"><label>通道<input v-model.number="channel" aria-label="Ripple 通道" type="number" min="0" /></label><label>宽<input v-model.number="width" aria-label="Ripple 宽" type="number" min="1" max="128" /></label><label>高<input v-model.number="height" aria-label="Ripple 高" type="number" min="1" max="128" /></label></template>
      <template v-else><label>起始通道<input v-model.number="channelStart" aria-label="Ripple 起始通道" type="number" min="0" /></label><label>通道数<input v-model.number="channelCount" aria-label="Ripple 通道数" type="number" min="1" max="16384" /></label></template>
    </div><button type="submit" :disabled="busy || !plugin.enabled">读取 Ripple 窗口</button>
  </form>
  <p class="notice">索引从 0 开始。首次仅读取头文件；单图 ≤128×128，单次谱线 ≤16384 通道。跨度过大时请缩小选区；不会自动读取整立方体。</p>
  <p v-if="busy" role="status">正在读取受控 Ripple 范围…</p><p v-if="error" role="alert">{{ error }}</p>
  <p v-if="current" class="notice" data-testid="ripple-stats">本次读取 {{ current.readBytes }} / 配对总计 {{ current.sourceBytes }} 字节，{{ current.reads }} 次范围请求；{{ current.nulls }} 个缺失或非有限值显示为空。</p>
  <p v-if="displayed && view === 'series'" class="notice">坐标按 origin + index × scale 展开；缺少 origin 时按格式默认 0 并在下方标记。无比例声明时仅显示索引，不将通道号冒充能量，不做单位换算。</p>
  <div ref="target" class="plot" aria-label="Ripple 通道或光谱图" />
</section></template>
<style scoped>
.ripple-preview{display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;flex:1;padding:16px;font-size:13px}p{margin:0;line-height:1.65}.notice{font-size:12px;color:#667085}form{display:flex;flex-direction:column;gap:10px;border:1px solid #d0d5dd;border-radius:6px;padding:12px}.fields{display:flex;gap:12px;flex-wrap:wrap}label{display:flex;gap:6px;align-items:center}input{width:72px}input,select,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}button{align-self:flex-start}button:disabled{opacity:.5}[role=alert]{color:#b42318}.plot{width:100%;flex-shrink:0}
</style>
