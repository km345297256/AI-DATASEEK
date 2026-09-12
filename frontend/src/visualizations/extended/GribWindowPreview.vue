<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { GRIB_WARNING, gribMessageOffset, parseGribWindow, validateGribSelection, type GribData } from './gribWindowData';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), plotScope = usePreviewLoad();
const catalog = shallowRef<GribData>(), displayed = shallowRef<GribData>();
const selectedId = ref(''), roi = ref<number[]>([]), busy = ref(false), error = ref(''), target = ref<HTMLDivElement>();
const history = ref<number[]>([0]);
const message = computed(() => catalog.value?.messages.find(v => v.id === selectedId.value));
const current = computed(() => displayed.value ?? catalog.value);
const timeUnits: Record<number, string> = { 0: '分钟', 1: '小时', 2: '天', 10: '3 小时', 11: '6 小时', 12: '12 小时', 13: '秒' };
let version: string | undefined;
function envelope(result: Awaited<ReturnType<typeof requestVisualization>>, kind: 'tree' | 'array') {
  if (result.kind !== kind || !/^[0-9a-f]{64}$/.test(result.version) || result.sampled !== false || JSON.stringify(result.warnings) !== JSON.stringify([GRIB_WARNING])) throw new Error('GRIB 结果或版本无效。');
}
function clearWindow() { scope.begin(); plotScope.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
async function inspect(offset = 0) {
  const pinned = offset ? version : undefined, request = scope.begin();
  plotScope.begin(); catalog.value = undefined; displayed.value = undefined; selectedId.value = ''; roi.value = []; busy.value = false; error.value = '';
  await nextTick(); if (!request.isCurrent() || !props.plugin.enabled) return;
  busy.value = true;
  try {
    if (offset && !pinned) throw new Error('请先读取第一页目录。');
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree', ...(pinned ? { version: pinned } : {}), offset }, request.signal);
    request.assertCurrent(); envelope(result, 'tree');
    if (pinned && result.version !== pinned) throw new Error('GRIB 文件版本变化，请重新读取第一页。');
    const parsed = parseGribWindow('tree', result.payload, result.metadata, { offset });
    if (parsed.sourceBytes !== props.file.size) throw new Error('GRIB 文件大小变化，请重新打开预览。');
    catalog.value = parsed; version = result.version;
    const previous = history.value.indexOf(offset);
    history.value = offset === 0 ? [0] : previous >= 0 ? history.value.slice(0, previous + 1) : [...history.value, offset];
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'GRIB 目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(data: GribData) {
  if (!data.array) return;
  const request = plotScope.begin(), array = data.array, item = data.messages[0]!;
  const plotly = await loadBrowserLibrary('plotly', request.signal);
  await nextTick(); request.assertCurrent(); if (!target.value) return;
  const element = document.createElement('div'); element.style.height = '420px'; element.dataset.testid = 'grib-plot'; target.value.replaceChildren(element);
  request.onDispose(() => { plotly.purge(element); element.remove(); });
  const width = array.shape[1]!;
  await plotly.newPlot(element, [{ type: 'heatmap', x: data.axes[1]!.values, y: data.axes[0]!.values,
    z: Array.from({ length: array.shape[0]! }, (_, row) => array.values.slice(row * width, (row + 1) * width)),
    zsmooth: false, connectgaps: false, colorscale: 'Viridis', colorbar: { title: { text: item.unit } },
    hovertemplate: '经度=%{x}°<br>纬度=%{y}°<br>值=%{z}<extra></extra>' }],
  { margin: { l: 75, r: 60, t: 25, b: 60 }, xaxis: { title: { text: '经度 [degrees_east, 0–360)' } }, yaxis: { title: { text: '纬度 [degrees_north]' } } },
  { responsive: true, displaylogo: false, displayModeBar: false });
  if (!request.isCurrent()) { plotly.purge(element); element.remove(); return; }
  if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (request.isCurrent()) void plotly.Plots.resize(element); }); observer.observe(element); request.onDispose(() => observer.disconnect()); }
}
async function loadWindow() {
  const request = scope.begin(); plotScope.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !message.value || !version) throw new Error('请先读取目录并选择消息。');
    const chosen = message.value, selection = validateGribSelection({ message: chosen.id, roi: roi.value }, chosen), pinned = version;
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'image', version: pinned, ...selection }, request.signal);
    request.assertCurrent(); envelope(result, 'array');
    if (result.version !== pinned) throw new Error('GRIB 文件版本变化，请重新读取目录。');
    const parsed = parseGribWindow('image', result.payload, result.metadata, selection);
    if (parsed.sourceBytes !== catalog.value?.sourceBytes || JSON.stringify(parsed.messages[0]) !== JSON.stringify(chosen)) throw new Error('GRIB 消息与已检查目录不一致。');
    await draw(parsed); request.assertCurrent(); displayed.value = parsed;
  } catch (reason) { if (request.isCurrent()) { plotScope.begin(); error.value = reason instanceof Error ? reason.message : 'GRIB 窗口读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch(selectedId, () => { if (!catalog.value) return; clearWindow(); roi.value = message.value ? [0, 0, Math.min(64, message.value.shape[1]!), Math.min(64, message.value.shape[0]!)] : []; });
watch(roi, () => { if (catalog.value) clearWindow(); }, { deep: true });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { history.value = [0]; version = undefined; void inspect(); }, { immediate: true });
</script>

<template>
  <section class="grib-preview">
    <p role="note">{{ GRIB_WARNING }}</p>
    <p class="notice">安全试点：仅 GRIB2 规则经纬网（3.0）、瞬时单层产品（4.0）和简单压缩（5.0）；每消息最多 16,384 网格点。仅文件内位图，不支持复杂压缩、局部表、集合/时间统计、交替行扫描或自动拼接消息。经度明确使用 [0,360)；跨 0/360 接缝的选区请缩小为单侧。</p>
    <div class="pages"><button :disabled="busy" @click="inspect(0)">重新读取 GRIB 目录</button><button :disabled="busy || history.length < 2" @click="inspect(history[history.length - 2]!)">上一页消息</button><button :disabled="busy || !catalog || catalog.nextOffset === null" @click="inspect(catalog!.nextOffset!)">下一页消息</button></div>
    <form v-if="catalog" @submit.prevent="loadWindow">
      <label>GRIB 消息<select v-model="selectedId" aria-label="GRIB 消息" :disabled="busy"><option value="">请选择消息</option><option v-for="item in catalog.messages" :key="item.id" :value="item.id">{{ item.short_name }} · {{ item.reference_time }} +{{ item.forecast_time }}×{{ timeUnits[item.forecast_unit] }} · 面 {{ item.surface_type }} · 字节 {{ gribMessageOffset(item.id) }} · {{ item.unit }}</option></select></label>
      <div v-if="message" class="details">{{ message.label }}；基准 {{ message.reference_time }}，预报 {{ message.forecast_time }} × {{ timeUnits[message.forecast_unit] }}；固定面代码 {{ message.surface_type }}，原始 scale/value {{ message.surface_scale ?? '未声明' }} / {{ message.surface_value ?? '未声明' }}。不跨消息合并层或时刻。</div>
      <div v-if="message" class="region"><label v-for="(name, index) in ['列起点', '行起点', '宽度', '高度']" :key="name">{{ name }}<input v-model.number="roi[index]" :aria-label="`GRIB ${name}`" type="number" :min="index < 2 ? 0 : 1" step="1" :disabled="busy" /></label></div>
      <button type="submit" :disabled="busy || !message">读取 GRIB 窗口</button>
    </form>
    <p v-if="catalog && !catalog.messages.length" class="notice" data-testid="grib-empty">本页没有符合安全方言的消息；可尝试下一页。文件不损坏也可能因网格、产品、编码或规模超出本试点范围而不可选。</p>
    <p v-else-if="catalog && !displayed && !busy" class="notice">目录仅读取元信息，未展开气象场。请选择消息与范围后点击读取。</p>
    <p v-if="busy" role="status">正在读取授权 GRIB 范围…</p><p v-if="error" role="alert">{{ error }}</p>
    <p v-if="current" class="notice" data-testid="grib-stats">读取 {{ current.readBytes }} / {{ current.sourceBytes }} 字节，{{ current.reads }} 次范围请求；本页跳过 {{ current.skipped }} 条不支持消息。{{ displayed ? `所选消息完整解码 ${displayed.decoded} 点，选区缺失 ${displayed.missing} 点。` : '' }}</p>
    <div ref="target" class="plot" aria-label="GRIB 经纬度气象场" />
  </section>
</template>

<style scoped>
.grib-preview{display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;flex:1;padding:16px;font-size:13px}p{margin:0;line-height:1.65}.notice,.details{font-size:12px;color:#667085}form{display:flex;flex-direction:column;gap:10px;padding:12px;border:1px solid #d0d5dd;border-radius:6px}.pages,.region{display:flex;flex-wrap:wrap;gap:10px}label{display:flex;gap:6px;align-items:center}select,input,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:85px}select{max-width:100%}button[type=submit]{align-self:flex-start}button:disabled{opacity:.5}[role=alert]{color:#b42318}.plot{width:100%;min-height:0;flex-shrink:0}
</style>
