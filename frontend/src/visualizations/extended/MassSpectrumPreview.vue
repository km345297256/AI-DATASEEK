<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { MASS_WARNING, massIntensityLabel, massSpectrumTrace, parseMassSpectrum, type MassSpectrumData } from './massSpectrumData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), plotScope = usePreviewLoad();
const catalog = shallowRef<MassSpectrumData>(), displayed = shallowRef<MassSpectrumData>();
const selectedId = ref(''), busy = ref(false), error = ref(''), target = ref<HTMLDivElement>();
const chosen = computed(() => catalog.value?.spectra.find(v => v.id === selectedId.value));
const reasons: Record<string, string> = { 'unsupported-representation': '未声明唯一 profile/centroid', 'unsupported-encoding': '不支持的数组编码', 'ambiguous-metadata': '元信息多项或含引用', 'point-budget': '超过 16,384 点', 'empty-spectrum': '空谱' };
let version: string | undefined;
function envelope(result: Awaited<ReturnType<typeof requestVisualization>>, kind: 'tree' | 'array') {
  if (result.kind !== kind || !/^[0-9a-f]{64}$/.test(result.version) || result.sampled !== false || JSON.stringify(result.warnings) !== JSON.stringify([MASS_WARNING])) throw new Error('质谱结果或版本不一致。');
}
async function inspect(offset = 0) {
  const pinned = offset ? version : undefined, request = scope.begin(); plotScope.begin();
  catalog.value = undefined; displayed.value = undefined; selectedId.value = ''; error.value = ''; busy.value = false;
  await nextTick(); if (!request.isCurrent() || !props.plugin.enabled) return;
  busy.value = true;
  try {
    if (offset && !pinned) throw new Error('请先读取第一页谱目录。');
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree', ...(pinned ? { version: pinned } : {}), offset }, request.signal);
    request.assertCurrent(); envelope(result, 'tree'); if (pinned && result.version !== pinned) throw new Error('文件版本变化，请重新读取质谱目录。');
    const value = parseMassSpectrum('tree', result.payload, result.metadata, { offset });
    if (value.sourceBytes !== props.file.size) throw new Error('文件大小变化，请重新打开质谱预览。');
    catalog.value = value; version = result.version;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '质谱目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(data: MassSpectrumData) {
  const request = plotScope.begin(), plotly = await loadBrowserLibrary('plotly', request.signal);
  await nextTick(); request.assertCurrent(); if (!target.value) return;
  const element = document.createElement('div'); element.style.height = '420px'; element.dataset.testid = 'mass-spectrum-plot'; target.value.replaceChildren(element);
  request.onDispose(() => { plotly.purge(element); element.remove(); });
  const item = data.spectra[0]!;
  await plotly.newPlot(element, [massSpectrumTrace(data)], { margin: { l: 80, r: 30, t: 25, b: 60 },
    xaxis: { title: { text: item.mz_unit ? 'm/z [MS:1000040]' : 'm/z [格式语义；单位未声明]' } },
    yaxis: { title: { text: '原始强度 [' + massIntensityLabel(item) + ']' }, zeroline: true } },
    { responsive: true, displaylogo: false, displayModeBar: false });
  if (!request.isCurrent()) { plotly.purge(element); element.remove(); return; }
  if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (request.isCurrent()) void plotly.Plots.resize(element); }); observer.observe(element); request.onDispose(() => observer.disconnect()); }
}
async function loadSpectrum() {
  const request = scope.begin(); plotScope.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !chosen.value?.selectable || !version) throw new Error('请先选择一条受支持的质谱。');
    const item = chosen.value, pinned = version;
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series', version: pinned, spectrum: item.id }, request.signal);
    request.assertCurrent(); envelope(result, 'array'); if (result.version !== pinned) throw new Error('文件版本变化，请重新读取质谱目录。');
    const value = parseMassSpectrum('series', result.payload, result.metadata, { spectrum: item.id });
    if (value.sourceBytes !== catalog.value?.sourceBytes || value.total !== catalog.value.total || JSON.stringify(value.spectra[0]) !== JSON.stringify(item)) throw new Error('所选质谱与已检查目录不一致。');
    await draw(value); request.assertCurrent(); displayed.value = value;
  } catch (reason) { if (request.isCurrent()) { plotScope.begin(); error.value = reason instanceof Error ? reason.message : '质谱读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch(selectedId, () => { if (catalog.value) { scope.begin(); plotScope.begin(); displayed.value = undefined; error.value = ''; busy.value = false; } });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { version = undefined; void inspect(); }, { immediate: true });
</script>
<template>
  <section class="mass-preview">
    <p role="note">{{ MASS_WARNING }}</p>
    <p class="notice">首版：MGF 两列峰表；mzML 1.1 内联 32/64 位浮点（不压缩或 zlib）。整文件 ≤16 MiB、最多 1,024 谱，每条谱 ≤16,384 点。目录读取整个有界源，但不展开 mzML 二进制数组；不支持 Numpress、外部数组、多 scan/前体或参数组引用。</p>
    <div class="pages"><button :disabled="busy" @click="inspect(0)">重新读取质谱目录</button><button :disabled="busy || !catalog || catalog.offset === 0" @click="inspect(catalog!.offset - 64)">上一页质谱</button><button :disabled="busy || !catalog || catalog.nextOffset === null" @click="inspect(catalog!.nextOffset!)">下一页质谱</button></div>
    <form v-if="catalog" @submit.prevent="loadSpectrum">
      <label>质谱<select v-model="selectedId" aria-label="质谱选择" :disabled="busy"><option value="">请选择单条质谱</option><option v-for="item in catalog.spectra" :key="item.id" :value="item.id" :disabled="!item.selectable">Spectrum {{ item.index + 1 }} · {{ item.representation }} · {{ item.points }} 点{{ item.selectable ? '' : ' · ' + reasons[item.reason] }}</option></select></label>
      <button type="submit" :disabled="busy || !chosen?.selectable">读取所选质谱</button>
      <p v-if="!catalog.spectra.some(v => v.selectable)" role="status">本页没有首版支持的质谱，可继续下一页或使用原文查看器核对文件说明。</p>
      <p>共 {{ catalog.total }} 谱，当前 {{ catalog.offset + 1 }}–{{ catalog.offset + catalog.spectra.length }}；不自动合并谱或生成色谱。</p>
    </form>
    <p v-if="busy" role="status">正在读取质谱…</p><p v-if="error" role="alert">{{ error }}</p>
    <p v-if="displayed" data-testid="mass-spectrum-stats">Spectrum {{ displayed.spectra[0]!.index + 1 }} · {{ displayed.spectra[0]!.representation === 'centroid' ? 'centroid 棒谱（峰间不连接）' : 'profile 连续谱（原始点顺序）' }} · {{ displayed.spectra[0]!.points }} 点 · 强度 {{ massIntensityLabel(displayed.spectra[0]!) }} · 保留时间 {{ displayed.spectra[0]!.retention_time ?? '未声明' }} {{ displayed.spectra[0]!.time_unit ?? '' }} · MS {{ displayed.spectra[0]!.ms_level ?? '未声明' }} · 前体 m/z {{ displayed.spectra[0]!.precursor_mz ?? '未声明' }}</p>
    <div ref="target" aria-label="质谱图" />
  </section>
</template>
<style scoped>
.mass-preview{padding:16px;overflow:auto;flex:1;min-height:0}.mass-preview p{font-size:13px;margin:8px 0}.notice{color:#64748b}.pages,form{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0}form p{width:100%}select,button{border:1px solid #cbd5e1;border-radius:6px;padding:6px;background:white}button:disabled{opacity:.45}[role=alert]{color:#b45309}
</style>
