<template>
  <section
    v-if="presentation"
    class="mt-2 w-full overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]"
    :aria-label="presentation.title || kindLabel"
  >
    <header class="flex items-start gap-2 border-b border-[var(--border-main)] px-3 py-2.5">
      <component :is="kindIcon" class="mt-0.5 size-4 shrink-0 text-[#2b7659]" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <h4 class="truncate text-sm font-medium text-[var(--text-primary)]">
          {{ presentation.title || kindLabel }}
        </h4>
        <p v-if="presentation.description" class="mt-0.5 whitespace-pre-wrap break-words text-xs leading-5 text-[var(--text-tertiary)]">
          {{ presentation.description }}
        </p>
      </div>
      <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-[var(--text-tertiary)]">
        {{ presentation.kind }}
      </span>
    </header>

    <div v-if="presentation.kind === 'table' && tableRows.length" class="max-h-[360px] overflow-auto">
      <table class="w-full border-collapse text-left text-xs">
        <thead class="sticky top-0 z-[1] bg-[var(--background-gray-main)] text-[var(--text-secondary)]">
          <tr>
            <th
              v-for="column in tableColumns"
              :key="column.key"
              class="border-b border-[var(--border-main)] px-3 py-2 font-medium"
              :class="columnAlignClass(column.align)"
            >
              {{ column.label || column.key }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in tableRows" :key="rowIndex" class="border-b border-[var(--border-main)] last:border-b-0">
            <td
              v-for="column in tableColumns"
              :key="column.key"
              class="max-w-[320px] whitespace-pre-wrap break-words px-3 py-2 align-top text-[var(--text-secondary)]"
              :class="columnAlignClass(column.align)"
            >
              {{ cellText(row[column.key]) }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="tableRows.length >= 100" class="border-t border-[var(--border-main)] px-3 py-2 text-[10px] text-[var(--text-tertiary)]">
        为保持页面流畅，仅展示前 100 行。
      </p>
    </div>

    <div v-else-if="presentation.kind === 'chart' && chartSeries.length && chartPoints.length" class="p-3">
      <div class="mb-2 flex flex-wrap gap-x-3 gap-y-1">
        <span v-for="series in chartSeries" :key="series.key" class="inline-flex items-center gap-1.5 text-[11px] text-[var(--text-secondary)]">
          <span class="size-2 rounded-full" :style="{ backgroundColor: series.color }" />
          {{ series.label }}
        </span>
      </div>
      <svg class="h-[190px] w-full" viewBox="0 0 480 190" role="img" :aria-label="presentation.title || '数据图表'">
        <line x1="34" y1="8" x2="34" y2="164" stroke="currentColor" class="text-[var(--border-main)]" />
        <line x1="34" y1="164" x2="472" y2="164" stroke="currentColor" class="text-[var(--border-main)]" />
        <template v-if="presentation.chart_type === 'bar'">
          <rect
            v-for="bar in chartBars"
            :key="bar.key"
            :x="bar.x"
            :y="bar.y"
            :width="bar.width"
            :height="bar.height"
            :fill="bar.color"
            opacity="0.82"
            rx="1"
          />
        </template>
        <template v-else>
          <polyline
            v-for="series in chartSeries"
            :key="series.key"
            :points="chartPolyline(series.key)"
            fill="none"
            :stroke="series.color"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <template v-if="presentation.chart_type === 'scatter'">
            <circle
              v-for="point in chartDots"
              :key="point.key"
              :cx="point.x"
              :cy="point.y"
              r="2.5"
              :fill="point.color"
            />
          </template>
        </template>
        <text x="4" y="14" class="fill-[var(--text-tertiary)] text-[10px]">{{ compactNumber(chartRange.max) }}</text>
        <text x="4" y="164" class="fill-[var(--text-tertiary)] text-[10px]">{{ compactNumber(chartRange.min) }}</text>
        <text x="34" y="182" class="fill-[var(--text-tertiary)] text-[10px]">{{ chartPoints[0]?.label }}</text>
        <text x="472" y="182" text-anchor="end" class="fill-[var(--text-tertiary)] text-[10px]">{{ chartPoints[chartPoints.length - 1]?.label }}</text>
      </svg>
    </div>

    <div v-else-if="presentation.kind === 'map' && geoSummary.points.length" class="p-3">
      <svg class="h-[190px] w-full rounded-lg bg-[var(--background-gray-main)]" viewBox="0 0 480 190" role="img" :aria-label="presentation.title || '空间数据预览'">
        <circle
          v-for="point in mapPoints"
          :key="point.key"
          :cx="point.x"
          :cy="point.y"
          r="2.2"
          fill="#2b7659"
          opacity="0.72"
        />
      </svg>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-[var(--text-tertiary)]">
        <span>要素 {{ geoSummary.featureCount }}</span>
        <span v-if="geoSummary.geometryTypes.length">几何 {{ geoSummary.geometryTypes.join('、') }}</span>
        <span v-if="geoSummary.bounds">范围 {{ geoSummary.bounds.map(compactNumber).join(', ') }}</span>
      </div>
    </div>

    <div v-else-if="presentation.kind === 'image' && resourceUrl" class="p-3">
      <img
        :src="resourceUrl"
        :alt="presentation.title || presentation.filename || '工具生成图像'"
        class="max-h-[520px] w-full rounded-lg bg-[var(--background-gray-main)] object-contain"
        loading="lazy"
        referrerpolicy="no-referrer"
      />
    </div>

    <div v-else-if="presentation.kind === 'artifact'" class="p-3">
      <a
        v-if="resourceUrl"
        :href="resourceUrl"
        target="_blank"
        rel="noopener noreferrer"
        class="flex items-center gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-3 text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-dark)]"
      >
        <FileOutput class="size-5 shrink-0 text-[#2b7659]" />
        <span class="min-w-0 flex-1">
          <span class="block truncate text-sm font-medium">{{ artifactFilename }}</span>
          <span v-if="artifactMimeType" class="mt-0.5 block truncate text-[11px] text-[var(--text-tertiary)]">{{ artifactMimeType }}</span>
        </span>
        <ExternalLink class="size-4 shrink-0 text-[var(--icon-tertiary)]" />
      </a>
      <div v-else class="flex items-center gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-3">
        <FileOutput class="size-5 shrink-0 text-[#2b7659]" />
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm text-[var(--text-primary)]">{{ artifactFilename }}</p>
          <p class="text-[11px] text-[var(--text-tertiary)]">未提供可安全访问的文件链接</p>
        </div>
      </div>
    </div>

    <pre
      v-else-if="presentation.kind === 'log'"
      class="max-h-[360px] overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-xs leading-5"
      :class="logLevelClass"
    >{{ presentationText(presentation.data) }}</pre>

    <pre v-else-if="hasDisplayData" class="max-h-[360px] overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-xs leading-5 text-[var(--text-secondary)]">{{ presentationText(presentation.data) }}</pre>

    <p v-else class="px-3 py-4 text-xs text-[var(--text-tertiary)]">
      工具已完成，但没有可用于此卡片的展示数据。
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import {
  Braces,
  ChartNoAxesCombined,
  ExternalLink,
  FileOutput,
  Image as ImageIcon,
  Map as MapIcon,
  ScrollText,
  Table2,
} from 'lucide-vue-next';
import type { ToolContent } from '../types/message';
import type { ToolPresentationColumn, ToolPresentationSeries } from '../types/toolPresentation';
import {
  presentationColumns,
  presentationRows,
  presentationText,
  resolveToolPresentation,
  safeToolResourceUrl,
  summarizeGeoPresentation,
} from '../utils/toolPresentation';

const props = defineProps<{ tool: ToolContent }>();

const presentation = computed(() => resolveToolPresentation(props.tool));
const kindLabels = {
  generic: '工具结果',
  table: '数据表格',
  chart: '数据图表',
  map: '空间数据',
  image: '图像结果',
  artifact: '结果文件',
  log: '运行日志',
} as const;
const kindIcons = {
  generic: Braces,
  table: Table2,
  chart: ChartNoAxesCombined,
  map: MapIcon,
  image: ImageIcon,
  artifact: FileOutput,
  log: ScrollText,
} as const;
const kindLabel = computed(() => presentation.value ? kindLabels[presentation.value.kind] : '工具结果');
const kindIcon = computed(() => presentation.value ? kindIcons[presentation.value.kind] : Braces);

const tableRows = computed(() => presentationRows(presentation.value?.data));
const tableColumns = computed(() => presentation.value
  ? presentationColumns(presentation.value, tableRows.value)
  : []);
const columnAlignClass = (align: ToolPresentationColumn['align']) => ({
  'text-center': align === 'center',
  'text-right': align === 'right',
});
const cellText = (value: unknown) => presentationText(value, 500);

const palette = ['#2b7659', '#2563eb', '#d97706', '#7c3aed', '#dc2626', '#0891b2'];
const chartRows = computed(() => presentationRows(presentation.value?.data).slice(0, 40));
const inferredChartKeys = computed(() => {
  const keys: string[] = [];
  for (const row of chartRows.value) {
    for (const [key, value] of Object.entries(row)) {
      if (typeof value === 'number' && Number.isFinite(value) && !keys.includes(key)) keys.push(key);
      if (keys.length >= 6) return keys;
    }
  }
  return keys;
});
const chartSeries = computed(() => {
  const source: ToolPresentationSeries[] = presentation.value?.series?.length
    ? presentation.value.series
    : inferredChartKeys.value.map((key) => ({ key, label: key }));
  return source.slice(0, 6).map((series, index) => ({
    key: series.key,
    label: series.label || series.key,
    color: series.color || palette[index % palette.length],
  }));
});
const chartPoints = computed(() => chartRows.value.map((row, index) => ({
  row,
  label: String(row[presentation.value?.x_key || ''] ?? index + 1).slice(0, 24),
})));
const chartRange = computed(() => {
  const values = chartPoints.value.flatMap(({ row }) => chartSeries.value
    .map((series) => row[series.key])
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value)));
  if (!values.length) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? { min: min - 1, max: max + 1 } : { min, max };
});
const chartX = (index: number) => chartPoints.value.length <= 1
  ? 253
  : 34 + (438 * index) / (chartPoints.value.length - 1);
const chartY = (value: number) => 8 + 156 * (1 - ((value - chartRange.value.min) / (chartRange.value.max - chartRange.value.min)));
const chartPolyline = (key: string) => chartPoints.value
  .map(({ row }, index) => typeof row[key] === 'number' && Number.isFinite(row[key])
    ? `${chartX(index)},${chartY(row[key] as number)}`
    : null)
  .filter(Boolean)
  .join(' ');
const chartDots = computed(() => chartSeries.value.flatMap((series) => chartPoints.value.flatMap(({ row }, index) => {
  const value = row[series.key];
  return typeof value === 'number' && Number.isFinite(value)
    ? [{ key: `${series.key}-${index}`, x: chartX(index), y: chartY(value), color: series.color }]
    : [];
})));
const chartBars = computed(() => {
  const groupWidth = Math.min(34, 400 / Math.max(1, chartPoints.value.length));
  const barWidth = Math.max(1, groupWidth / Math.max(1, chartSeries.value.length));
  const zeroY = chartY(Math.max(chartRange.value.min, Math.min(chartRange.value.max, 0)));
  return chartPoints.value.flatMap(({ row }, pointIndex) => chartSeries.value.flatMap((series, seriesIndex) => {
    const value = row[series.key];
    if (typeof value !== 'number' || !Number.isFinite(value)) return [];
    const valueY = chartY(value);
    return [{
      key: `${series.key}-${pointIndex}`,
      x: chartX(pointIndex) - groupWidth / 2 + seriesIndex * barWidth,
      y: Math.min(valueY, zeroY),
      width: Math.max(1, barWidth - 1),
      height: Math.max(1, Math.abs(zeroY - valueY)),
      color: series.color,
    }];
  }));
});
const compactNumber = (value: number) => Number.isFinite(value)
  ? new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 5 }).format(value)
  : '';

const geoSummary = computed(() => summarizeGeoPresentation(presentation.value?.data));
const mapPoints = computed(() => {
  const points = geoSummary.value.points.slice(0, 300);
  const bounds = geoSummary.value.bounds;
  if (!bounds) return [];
  const [minX, minY, maxX, maxY] = bounds;
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  return points.map(([x, y], index) => ({
    key: index,
    x: 12 + ((x - minX) / spanX) * 456,
    y: 178 - ((y - minY) / spanY) * 166,
  }));
});

const dataRecord = computed<Record<string, unknown>>(() => {
  const data = presentation.value?.data;
  return data && typeof data === 'object' && !Array.isArray(data)
    ? data as Record<string, unknown>
    : {};
});
const artifactRecord = computed<Record<string, unknown>>(() => {
  const direct = dataRecord.value.artifact;
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    return direct as Record<string, unknown>;
  }
  const artifacts = dataRecord.value.artifacts;
  if (Array.isArray(artifacts)) {
    const first = artifacts.find((item) => item && typeof item === 'object' && !Array.isArray(item));
    if (first) return first as Record<string, unknown>;
  }
  return dataRecord.value;
});
const resourceUrl = computed(() => safeToolResourceUrl(
  presentation.value?.url || artifactRecord.value.file_url || artifactRecord.value.url,
  typeof window === 'undefined' ? undefined : window.location.origin,
));
const artifactFilename = computed(() => {
  const path = String(artifactRecord.value.path || '').replace(/\\/g, '/');
  return presentation.value?.filename
    || String(artifactRecord.value.filename || artifactRecord.value.name || path.split('/').pop() || '工具生成文件').slice(0, 255);
});
const artifactMimeType = computed(() => presentation.value?.mime_type
  || String(artifactRecord.value.mime_type || artifactRecord.value.type || '').slice(0, 160));
const hasDisplayData = computed(() => {
  const data = presentation.value?.data;
  return data !== null && data !== undefined && data !== '';
});
const logLevelClass = computed(() => ({
  'text-[var(--text-secondary)]': !presentation.value?.level || presentation.value.level === 'debug' || presentation.value.level === 'info',
  'text-amber-700 dark:text-amber-300': presentation.value?.level === 'warning',
  'text-red-700 dark:text-red-300': presentation.value?.level === 'error',
}));
</script>
