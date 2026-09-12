<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">MiniSEED v2 固定长度记录／SAC 6、7 均匀采样二进制试点。原始存储值；不拼接记录、不填补缺口、不滤波、不重采样、不校正仪器响应，SAC SCALE 不应用。目录与质量标志不是全文件数据质量证明。</p>
    <form v-if="catalog" class="mb-3 rounded border p-3 text-sm" @submit.prevent="inspect(false)">
      <p class="mb-2">{{ catalog.format === 'sac' ? 'SAC 单段' : '固定长度记录槽（由首条记录长度计算）' }}：{{ catalog.recordSlots.toLocaleString() }}；当前目录 {{ catalog.catalogOffset }}–{{ catalog.catalogOffset + catalog.catalogCount - 1 }}。{{ catalog.catalogComplete ? '已检查全部记录头，未预读全部样本。' : '仅核查本页记录头；其他槽未扫描。' }}</p>
      <div v-if="catalog.format !== 'sac'" class="flex flex-wrap items-center gap-3"><label>目录起始记录 <input v-model.number="pageOffset" aria-label="目录起始记录" type="number" min="0" :max="catalog.recordSlots - 1" step="1" class="w-24 rounded border p-1" :disabled="busy" /></label>
        <label>每页记录 <input v-model.number="pageLimit" aria-label="每页记录" type="number" min="1" max="16" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
        <button type="submit" class="rounded border px-3 py-1" :disabled="busy">读取目录页</button>
        <button type="button" class="rounded border px-3 py-1" :disabled="busy || catalog.catalogOffset === 0" @click="page(-1)">上一目录页</button>
        <button type="button" class="rounded border px-3 py-1" :disabled="busy || catalog.catalogOffset + catalog.catalogCount >= catalog.recordSlots" @click="page(1)">下一目录页</button>
      </div>
    </form>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadWindow">
      <label>记录 <select v-model.number="recordId" aria-label="地震波形记录" class="ml-2 max-w-full rounded border p-1" :disabled="busy"><option v-for="r in catalog.records" :key="r.id" :value="r.id">#{{ r.id }} {{ r.label }} · {{ r.encoding }} · {{ r.samples.toLocaleString() }} 样本</option></select></label>
      <div v-if="selectedRecord" class="my-3 space-y-1 text-xs text-gray-600">
        <p>采样间隔 {{ selectedRecord.sample_interval }} 秒；{{ selectedRecord.start_time ? `起始 UTC（微秒显示）：${selectedRecord.start_time}` : '未声明可用绝对参考时间' }}。</p>
        <p>单位：{{ selectedRecord.unit === 'unknown' ? '未知（不猜测 counts 或物理单位）' : `${selectedRecord.unit}（文件声明）` }}；{{ selectedRecord.variant }} / {{ selectedRecord.byte_order }} endian。{{ selectedRecord.declared_scale !== null ? `SAC SCALE=${selectedRecord.declared_scale}，仅显示，未应用。` : '' }}</p>
        <p v-if="selectedRecord.variant.startsWith('SAC')">SAC B={{ selectedRecord.begin_seconds }} 秒（相对文件参考时间）；图中横轴从本段首样本计时。</p>
        <p v-else>实际添加的起始时间修正 {{ selectedRecord.time_adjustment_seconds }} 秒；timing quality={{ selectedRecord.timing_quality ?? '未提供' }}；quality flags={{ selectedRecord.quality_flags }}（不自动屏蔽标志样本）。</p>
        <p data-testid="seismic-continuity">本页同通道前驱比较：{{ relationLabel(selectedRecord.relation) }}{{ selectedRecord.gap_seconds !== null ? `，时间差 ${selectedRecord.gap_seconds} 秒` : '' }}。负差可能为重叠或乱序；本视图从不连接不同记录。</p>
      </div>
      <div class="flex flex-wrap items-center gap-3"><label>起始样本（从 0 开始） <input v-model.number="startSample" aria-label="起始样本" type="number" min="0" :max="(selectedRecord?.samples ?? 1) - 1" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label>
        <label>样本数 <input v-model.number="sampleCount" aria-label="样本数" type="number" min="1" :max="Math.min(16384, selectedRecord?.samples ?? 1)" step="1" class="w-24 rounded border p-1" :disabled="busy" /></label>
        <button type="submit" class="rounded border px-3 py-1" :disabled="busy || !selectedRecord">读取波形窗口</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">打开目录不自动读取样本。最多 16,384 个输出点；STEIM 必须先解码命中的一条记录（≤65,535 样本），再取窗口；未压缩记录与 SAC 直接读取选定样本字节。NaN/Inf 留空，不连接空值。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取授权的地震记录字节范围…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="current" class="my-3 text-xs text-gray-500" data-testid="seismic-stats">本次读取 {{ current.readBytes.toLocaleString() }} / 源 {{ current.sourceBytes.toLocaleString() }} 字节，{{ current.reads }} 次范围请求；解码 {{ current.decodedSamples.toLocaleString() }} 样本 / {{ current.decodedBytes.toLocaleString() }} 字节。</p>
    <NumericSeriesPlot v-if="data" :traces="traces" x-label="相对此记录首样本的时间（秒）" label="单记录原始地震波形" />
  </section>
</template>
<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import NumericSeriesPlot from './NumericSeriesPlot.vue';
import { parseSeismicWindow, seismicRecordsEqual, validateSeismicPage, validateSeismicSelection, type SeismicData } from './seismicWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), catalog = shallowRef<SeismicData>(), data = shallowRef<SeismicData>();
const recordId = ref(0), startSample = ref(0), sampleCount = ref(1), pageOffset = ref(0), pageLimit = ref(16), busy = ref(false), error = ref('');
let version: string | undefined;
const selectedRecord = computed(() => catalog.value?.records.find(r => r.id === recordId.value));
const current = computed(() => data.value ?? catalog.value);
// Plotly treats NaN as a gap; retain every x position instead of filtering missing samples.
const traces = computed(() => data.value?.x && data.value.y ? [{ name: data.value.records[0]!.label, unit: data.value.records[0]!.unit === 'unknown' ? '未知原始单位' : data.value.records[0]!.unit, x: data.value.x, y: data.value.y.map(value => value ?? Number.NaN) }] : []);
const relationLabel = (value: string) => ({ uncompared: '未比较', continuous: '在时间精度内连续', gap: '存在间隙', overlap: '重叠／乱序', 'rate-change': '采样率改变' }[value] ?? '未比较');
async function inspect(initial: boolean) {
  const request = scope.begin(); data.value = undefined; error.value = ''; busy.value = false;
  if (initial) { catalog.value = undefined; version = undefined; recordId.value = 0; startSample.value = 0; sampleCount.value = 1; pageOffset.value = 0; pageLimit.value = 16; }
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const options = initial ? {} : validateSeismicPage({ record_offset: pageOffset.value, record_limit: pageLimit.value });
    if (!initial && (!version || !catalog.value || pageOffset.value >= catalog.value.recordSlots)) throw new Error('请先读取有效文件目录。');
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree', ...(version ? { version } : {}), ...options }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version) || version && result.version !== version) throw new Error('地震记录版本已变化，请重新打开预览。');
    const parsed = parseSeismicWindow('tree', result.payload, result.metadata, options);
    if (parsed.format !== props.file.filename.split('.').pop()?.toLowerCase() || typeof props.file.size === 'number' && props.file.size > 0 && parsed.sourceBytes !== props.file.size
      || catalog.value && (parsed.recordBytes !== catalog.value.recordBytes || parsed.recordSlots !== catalog.value.recordSlots)) throw new Error('返回目录与当前文件不一致。');
    catalog.value = parsed; version = result.version; recordId.value = parsed.records[0]!.id;
    startSample.value = 0; sampleCount.value = Math.min(1024, parsed.records[0]!.samples); pageOffset.value = parsed.catalogOffset;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '地震记录目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function loadWindow() {
  const request = scope.begin(); data.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !selectedRecord.value || !version || !catalog.value) throw new Error('请先检查并选择记录。');
    const selection = validateSeismicSelection({ record: recordId.value, start_sample: startSample.value, sample_count: sampleCount.value }, selectedRecord.value);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'series', version, ...selection }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'series' || result.version !== version) throw new Error('地震记录版本已变化，请重新打开预览。');
    const parsed = parseSeismicWindow('series', result.payload, result.metadata, selection);
    if (!seismicRecordsEqual(parsed.records[0]!, selectedRecord.value) || parsed.sourceBytes !== catalog.value.sourceBytes || parsed.format !== catalog.value.format || parsed.recordBytes !== catalog.value.recordBytes || parsed.recordSlots !== catalog.value.recordSlots) throw new Error('返回波形与已检查记录不一致。');
    data.value = parsed;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '地震波形窗口读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function page(direction: number) {
  if (!catalog.value || busy.value) return;
  const offset = direction > 0 ? catalog.value.catalogOffset + catalog.value.catalogCount : Math.max(0, catalog.value.catalogOffset - pageLimit.value);
  if (offset >= catalog.value.recordSlots) return;
  pageOffset.value = offset; await inspect(false);
}
watch(recordId, () => { if (selectedRecord.value) { startSample.value = 0; sampleCount.value = Math.min(1024, selectedRecord.value.samples); } }, { flush: 'sync' });
watch([recordId, startSample, sampleCount, pageOffset, pageLimit], () => { if (catalog.value) { scope.begin(); data.value = undefined; busy.value = false; error.value = ''; } }, { flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(true); }, { immediate: true, flush: 'sync' });
</script>
