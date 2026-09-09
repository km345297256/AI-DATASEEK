<template>
  <div class="scientific-preview flex min-h-0 flex-1 flex-col overflow-auto p-4 text-[var(--text-primary)]">
    <form class="mb-3 flex flex-wrap items-end gap-3 rounded-lg border border-[var(--border-main)] p-3 text-xs" @submit.prevent="load(false)">
      <label v-if="plugin.reader === 'netcdf' && variables.length" class="flex min-w-32 flex-col gap-1">
        数据变量
        <select v-model="variable" class="control" @change="resetDimensions">
          <option v-for="item in variables" :key="item.name" :value="item.name">{{ item.name }}{{ item.units ? ` [${item.units}]` : '' }}</option>
        </select>
      </label>
      <label v-if="plugin.view_kind === 'series' && selectedVariable?.dimensions.length" class="flex min-w-28 flex-col gap-1">
        横轴维度
        <select v-model="xDimension" class="control">
          <option v-for="dimension in selectedVariable.dimensions" :key="dimension.name" :value="dimension.name">{{ dimension.name }} ({{ dimension.size }})</option>
        </select>
      </label>
      <label v-for="dimension in sliceDimensions" :key="dimension.name" class="flex w-28 flex-col gap-1">
        {{ dimension.name }} 索引
        <input v-model.number="indices[dimension.name]" class="control" type="number" min="0" :max="dimension.size - 1" step="1" required />
      </label>
      <label v-if="hdus.length" class="flex min-w-32 flex-col gap-1">
        FITS 图像 HDU
        <select v-model.number="hdu" class="control" @change="changeHdu">
          <option v-for="item in hdus" :key="item.index" :value="item.index">{{ item.index }} · {{ item.shape.join(' × ') }}</option>
        </select>
      </label>
      <button type="submit" class="control cursor-pointer bg-[var(--fill-tsp-gray-main)] disabled:opacity-50" :disabled="loading">{{ loading ? '读取中…' : '应用 / 重新读取' }}</button>
      <label v-if="plugin.view_kind === 'map'" class="flex items-center gap-1.5 pb-2"><input v-model="showBasemap" type="checkbox" />叠加离线地理底图</label>
    </form>
    <p v-if="error" role="alert" class="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{{ error }} <button v-if="version" type="button" class="underline" @click="load(true)">重新载入文件版本</button></p>
    <p v-if="loading" role="status" class="py-12 text-center text-sm text-[var(--text-tertiary)]">正在读取有界数据切片…</p>
    <template v-else-if="data">
      <div class="mb-2 flex flex-wrap gap-2 text-xs text-[var(--text-secondary)]">
        <span>{{ data.selected_variable || plugin.name }}</span>
        <span v-if="data.sampled" class="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">抽样 / 降采样预览，非完整数据</span>
        <span v-if="data.kind === 'map'">经纬度坐标 · 不进行空间插值</span>
        <span v-if="data.kind === 'quality'">Phred+33 · 各位置平均质量分数</span>
      </div>
      <div class="overflow-x-auto rounded-lg border border-[var(--border-main)] bg-white p-2">
        <canvas ref="canvas" width="760" height="460" class="h-auto w-full min-w-[420px]" role="img" :aria-label="chartDescription" />
      </div>
      <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ chartDescription }}。灰白栅格或曲线断点表示缺失值，不按零值处理。</p>
      <p v-if="appliedIndices" class="mt-1 text-xs text-[var(--text-tertiary)]">当前切片：{{ appliedIndices }}</p>
      <p v-if="data.kind === 'map' && showBasemap" class="mt-1 text-[10px] text-[var(--text-tertiary)]">底图：Natural Earth 1:110m 公共领域轮廓，简化示意，不表示边界立场。</p>
      <ul v-if="data.warnings.length" class="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-700"><li v-for="(warning, index) in data.warnings" :key="index">{{ warning }}</li></ul>
      <details v-if="metadataItems.length" class="mt-3 text-xs"><summary class="cursor-pointer">读取与抽样信息</summary><dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1"><template v-for="[key, value] in metadataItems" :key="key"><dt>{{ key }}</dt><dd class="break-all">{{ value }}</dd></template></dl></details>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../api/file';
import { getScientificVisualization, type ScientificVisualization, type ScientificVariable } from '../api/visualization';
import { usePreviewLoad } from '../composables/usePreviewLoad';
import type { VisualizationPlugin } from './contract';
import { coordinateEdges, finiteExtent, heatColor, seriesSegments, tickLabel } from './plot';
import worldOutline from './assets/world-outline.json';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad();
const data = ref<ScientificVisualization | null>(null);
const variables = ref<ScientificVariable[]>([]);
const variable = ref('');
const xDimension = ref('');
const indices = ref<Record<string, number>>({});
const hdu = ref<number | undefined>();
const hdus = ref<Array<{ index: number; shape: number[] }>>([]);
const version = ref<string>();
const loading = ref(false);
const error = ref('');
const canvas = ref<HTMLCanvasElement>();
const showBasemap = ref(true);
const selectedVariable = computed(() => variables.value.find((item) => item.name === variable.value));
const sliceDimensions = computed(() => {
  const dimensions = selectedVariable.value?.dimensions ?? [];
  if (props.plugin.view_kind === 'series') return dimensions.filter((item) => item.name !== xDimension.value);
  if (variable.value !== data.value?.selected_variable) return [];
  const plotDimensions = data.value?.metadata.spatial_dimensions;
  // The reader names actual coordinate dimensions, including nonstandard CF names.
  if (Array.isArray(plotDimensions)) return dimensions.filter((item) => !plotDimensions.includes(item.name));
  return dimensions.slice(0, -2);
});
const resetDimensions = () => {
  xDimension.value = selectedVariable.value?.dimensions[0]?.name ?? '';
  indices.value = Object.fromEntries((selectedVariable.value?.dimensions ?? []).map((dimension) => [dimension.name, 0]));
};
const changeHdu = () => {
  variables.value = []; variable.value = ''; xDimension.value = ''; indices.value = {};
};
const appliedIndices = computed(() => {
  const value = data.value?.metadata.indices;
  return value && typeof value === 'object' && !Array.isArray(value) ? Object.entries(value).map(([name, index]) => `${name}=${index}`).join('，') : '';
});
const metadataItems = computed(() => Object.entries(data.value?.metadata ?? {}).filter(([, value]) => value !== null && ['string', 'number', 'boolean'].includes(typeof value)).map(([key, value]) => [key, String(value).slice(0, 500)]));
const chartDescription = computed(() => !data.value ? '' : `${data.value.x_label || '横轴'} / ${data.value.y_label || '纵轴'}；${data.value.kind === 'series' || data.value.kind === 'quality' ? `${data.value.x.length} 个采样点` : `${data.value.width} × ${data.value.height} 栅格`}`);

const load = async (resetVersion = false) => {
  const request = scope.begin();
  loading.value = true;
  error.value = '';
  const activeDimensions = sliceDimensions.value;
  const selectedIndices = Object.fromEntries(activeDimensions.map((dimension) => [dimension.name, indices.value[dimension.name] ?? 0]));
  data.value = null;
  try {
    const result = await getScientificVisualization(props.file.file_id, {
      plugin_id: props.plugin.id,
      ...(props.plugin.reader === 'netcdf' && variable.value ? { variable: variable.value } : {}),
      ...(props.plugin.view_kind === 'series' && xDimension.value ? { x_dimension: xDimension.value } : {}),
      ...(Object.keys(selectedIndices).length ? { indices: selectedIndices } : {}),
      ...(props.plugin.reader === 'fits' && hdu.value !== undefined ? { hdu: hdu.value } : {}),
      ...(!resetVersion && version.value ? { version: version.value } : {}),
    }, request.signal);
    if (!request.isCurrent()) return;
    const expectedKind = props.plugin.adapter === 'scientific-quality' ? 'quality' : props.plugin.view_kind;
    if (result.kind !== expectedKind) throw new Error('读取结果与所选插件视图不匹配。');
    data.value = result;
    version.value = result.version;
    variables.value = result.variables;
    if (!variable.value) {
      variable.value = result.selected_variable ?? '';
      resetDimensions();
      if (typeof result.metadata.x_dimension === 'string') xDimension.value = result.metadata.x_dimension;
    }
    if (Array.isArray(result.metadata.hdus)) {
      hdus.value = result.metadata.hdus.filter((item): item is { index: number; shape: number[] } => !!item && typeof item === 'object' && Number.isInteger(item.index) && Array.isArray(item.shape) && item.shape.length > 0);
      if (typeof result.metadata.hdu === 'number') hdu.value = result.metadata.hdu;
    }
    loading.value = false;
    await nextTick();
    if (request.isCurrent()) draw();
  } catch (reason) {
    if (!request.isCurrent()) return;
    error.value = reason instanceof Error ? reason.message : '数据读取失败，请重试。';
  } finally {
    if (request.isCurrent()) loading.value = false;
  }
};

function draw() {
  const result = data.value, context = canvas.value?.getContext('2d');
  if (!result || !context) return;
  context.clearRect(0, 0, 760, 460);
  context.fillStyle = '#fff'; context.fillRect(0, 0, 760, 460);
  const left = 78, top = 26, width = 600, height = 340;
  let xExtent: [number, number] = [-.5, Math.max(.5, result.width - .5)];
  // FITS raw array convention: row zero at the top; do not label it as the
  // bottom row or pretend this is a WCS-aware astronomical projection.
  let yExtent: [number, number] = [Math.max(.5, result.height - .5), -.5];
  const lines = result.kind === 'series' || result.kind === 'quality';
  const xEdges = result.kind === 'map' ? coordinateEdges(result.x, -180, 180) : [];
  const yEdges = result.kind === 'map' ? coordinateEdges(result.y as number[], -90, 90) : [];
  if (lines) { xExtent = finiteExtent(result.x) ?? [0, 1]; yExtent = finiteExtent(result.y) ?? [0, 1]; }
  else if (result.kind === 'map' && xEdges.length === result.width + 1 && yEdges.length === result.height + 1) {
    xExtent = [Math.min(...xEdges), Math.max(...xEdges)];
    yExtent = [Math.min(...yEdges), Math.max(...yEdges)];
  }
  const px = (x: number) => left + (x - xExtent[0]) / (xExtent[1] - xExtent[0] || 1) * width;
  const py = (y: number) => top + height - (y - yExtent[0]) / (yExtent[1] - yExtent[0] || 1) * height;
  context.save(); context.beginPath(); context.rect(left, top, width, height); context.clip();
  if (lines) {
    context.strokeStyle = '#277f69'; context.fillStyle = '#277f69'; context.lineWidth = 2;
    for (const segment of seriesSegments(result.x, result.y)) {
      if (segment.length === 1) { context.beginPath(); context.arc(px(segment[0]![0]), py(segment[0]![1]), 2.5, 0, 2 * Math.PI); context.fill(); continue; }
      context.beginPath(); segment.forEach(([x, y], index) => index ? context.lineTo(px(x), py(y)) : context.moveTo(px(x), py(y))); context.stroke();
    }
  } else {
    const colors = finiteExtent(result.values);
    for (let row = 0; row < result.height; row++) for (let column = 0; column < result.width; column++) {
      context.fillStyle = heatColor(result.values[row * result.width + column] ?? null, colors);
      if (result.kind === 'map' && xEdges.length && yEdges.length) {
        const x1 = px(xEdges[column]!), x2 = px(xEdges[column + 1]!), y1 = py(yEdges[row]!), y2 = py(yEdges[row + 1]!);
        context.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1) + .2, Math.abs(y2 - y1) + .2);
      } else context.fillRect(left + column / result.width * width, top + row / result.height * height, width / result.width + .2, height / result.height + .2);
    }
    if (result.kind === 'map' && showBasemap.value) {
      context.strokeStyle = '#202a34'; context.lineWidth = .7;
      for (const line of worldOutline.geometry.coordinates) {
        context.beginPath(); line.forEach(([x, y], index) => index ? context.lineTo(px(x!), py(y!)) : context.moveTo(px(x!), py(y!))); context.stroke();
      }
    }
    context.restore(); context.save();
    for (let index = 0; index < 100; index++) { context.fillStyle = heatColor(index / 99, [0, 1]); context.fillRect(left + index * 3, 424, 3.1, 9); }
    context.fillStyle = '#52606d'; context.font = '11px sans-serif';
    const units = result.variables.find((item) => item.name === result.selected_variable)?.units;
    context.fillText(`${colors ? tickLabel(colors[0]) : '无有效数值'} ← ${units || '数值色阶'} → ${colors ? tickLabel(colors[1]) : ''}`, left + 310, 433);
  }
  context.restore(); context.strokeStyle = '#6b7280'; context.lineWidth = 1; context.strokeRect(left, top, width, height);
  context.fillStyle = '#374151'; context.font = '11px sans-serif';
  for (let index = 0; index <= 4; index++) {
    const ratio = index / 4;
    context.textAlign = 'center'; context.fillText(tickLabel(xExtent[0] + ratio * (xExtent[1] - xExtent[0])), left + ratio * width, top + height + 19);
    context.textAlign = 'right'; context.fillText(tickLabel(yExtent[0] + ratio * (yExtent[1] - yExtent[0])), left - 8, top + height - ratio * height + 4);
  }
  context.textAlign = 'center'; context.font = '12px sans-serif'; context.fillText(result.x_label || '横轴', left + width / 2, 409);
  context.save(); context.translate(18, top + height / 2); context.rotate(-Math.PI / 2); context.fillText(result.y_label || '纵轴', 0, 0); context.restore();
}
watch(showBasemap, draw);
watch(() => [props.file.file_id, props.plugin.id], () => {
  variable.value = ''; variables.value = []; indices.value = {}; xDimension.value = ''; hdu.value = undefined; hdus.value = []; version.value = undefined;
  void load(true);
}, { immediate: true });
</script>

<style scoped>
.control { min-height: 32px; border: 1px solid var(--border-main); border-radius: 6px; padding: 4px 8px; color: var(--text-primary); background-color: var(--background-menu-white); max-width: 240px; }
</style>
