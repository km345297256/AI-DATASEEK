<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-[var(--text-secondary)]">Shapefile 预览</span>
        <span v-if="summary">{{ summary }}</span>
        <select v-if="layerChoices.length > 1" v-model="selectedLayerKey" class="max-w-[260px] rounded border border-[var(--border-main)] bg-white px-2 py-1 text-xs text-[var(--text-secondary)]" aria-label="选择 Shapefile 图层">
          <option v-for="layer in layerChoices" :key="layer.key" :value="layer.key">{{ layer.label }}</option>
        </select>
      </div>
      <span v-if="projectionSummary" class="max-w-[360px] truncate" :title="projectionText">{{ projectionSummary }}</span>
    </div>

    <div v-if="warnings.length" role="status" class="shrink-0 border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900">
      <p v-for="warning in warnings" :key="warning">{{ warning }}</p>
    </div>

    <div v-if="status" class="flex min-h-0 flex-1 items-center justify-center p-4">
      <div class="max-w-[520px] rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        {{ status }}
      </div>
    </div>

    <div v-else class="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_220px] gap-3 p-4">
      <div class="grid min-h-0 grid-cols-[180px_minmax(0,1fr)] overflow-hidden rounded-xl border border-[var(--border-main)] bg-white">
        <aside class="min-h-0 overflow-auto border-r border-[var(--border-main)] bg-[var(--background-menu-white)] p-2">
          <div class="mb-2 px-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-tertiary)]">图层</div>
          <button
            v-for="layer in layerChoices"
            :key="layer.key"
            type="button"
            class="mb-1 flex w-full items-center gap-2 rounded px-2 py-2 text-left text-xs transition-colors"
            :class="selectedLayerKey === layer.key ? 'bg-[#e8f1ec] text-[#245b42]' : 'text-[var(--text-secondary)] hover:bg-[var(--background-gray-main)]'"
            @click="selectedLayerKey = layer.key"
          >
            <span class="size-2 shrink-0 rounded-full bg-[#2878d3]" />
            <span class="min-w-0 flex-1 truncate" :title="layer.label">{{ layer.label }}</span>
          </button>
          <div v-if="!layerChoices.length" class="px-2 text-xs text-[var(--text-tertiary)]">暂无图层</div>
        </aside>
        <div class="relative min-h-0 overflow-hidden bg-[#eef2f4]">
          <div class="absolute left-3 top-3 z-10 flex flex-col overflow-hidden rounded border border-[var(--border-main)] bg-white shadow-sm">
            <button type="button" class="size-8 text-lg text-[var(--text-secondary)] hover:bg-gray-50" title="放大" @click="zoom(1 / 1.35)">+</button>
            <button type="button" class="size-8 border-t border-[var(--border-main)] text-lg text-[var(--text-secondary)] hover:bg-gray-50" title="缩小" @click="zoom(1.35)">−</button>
            <button type="button" class="size-8 border-t border-[var(--border-main)] text-xs text-[var(--text-secondary)] hover:bg-gray-50" title="复位" @click="resetView">⌂</button>
          </div>
          <label class="absolute right-3 top-3 z-10 flex items-center gap-2 rounded border border-[var(--border-main)] bg-white px-2 py-1.5 text-xs text-[var(--text-secondary)] shadow-sm">
            <input v-model="showBasemap" type="checkbox" class="accent-[#2878d3]" /> 地图底图
          </label>
          <div class="absolute right-3 top-12 z-10 flex gap-2">
            <button type="button" class="rounded border px-2 py-1.5 text-xs shadow-sm" :class="selectionMode ? 'border-[#2878d3] bg-[#e8f1ec] text-[#245b42]' : 'border-[var(--border-main)] bg-white text-[var(--text-secondary)]'" :aria-pressed="selectionMode" title="选择与框内区域相交的完整要素，不裁剪几何" @click="toggleSelectionMode">{{ selectionMode ? '退出框选' : '框选要素' }}</button>
            <button v-if="selectedIndices.size || selectionRect" type="button" class="rounded border border-[var(--border-main)] bg-white px-2 py-1.5 text-xs text-[var(--text-secondary)] shadow-sm" @click="clearSelection">清除选择</button>
          </div>
        <svg
          v-if="viewBox && renderFeatures.length"
          ref="mapElement"
          class="h-full w-full select-none touch-none"
          :class="selectionMode ? 'cursor-crosshair' : 'cursor-default'"
          aria-label="Shapefile 地图"
          tabindex="0"
          :viewBox="viewBox"
          preserveAspectRatio="xMidYMid meet"
          @pointerdown="startSelection"
          @pointermove="moveSelection"
          @pointerup="finishSelection"
          @pointercancel="cancelSelection"
          @lostpointercapture="cancelSelection"
          @keydown.esc.prevent="exitSelectionMode"
          @dragstart.prevent>
          <template v-if="showBasemap && basemapTiles.length">
            <image v-for="tile in basemapTiles" :key="tile.key" :href="tile.url" :x="tile.x" :y="tile.y" :width="tile.width" :height="tile.height" preserveAspectRatio="none" opacity="0.72" pointer-events="none" />
          </template>
          <g>
            <template v-for="{ geometry, recordIndex: geometryIndex } in renderFeatures" :key="geometryIndex">
              <circle
                v-if="geometry.type === 'Point'"
                @click.stop="selectFeature(geometryIndex, $event)"
                :cx="geometry.coordinates[0]"
                :cy="flipY(geometry.coordinates[1])"
                :r="pointRadius"
                :fill="selectedIndices.has(geometryIndex) ? '#dc2626' : '#2563eb'"
                fill-opacity="0.85" />
              <template v-else-if="geometry.type === 'MultiPoint'">
                <circle
                  v-for="(point, pointIndex) in geometry.coordinates"
                  :key="pointIndex"
                  @click.stop="selectFeature(geometryIndex, $event)"
                  :cx="point[0]"
                  :cy="flipY(point[1])"
                  :r="pointRadius"
                  :fill="selectedIndices.has(geometryIndex) ? '#dc2626' : '#2563eb'"
                  fill-opacity="0.85" />
              </template>
              <path
                v-else-if="geometry.type === 'LineString'"
                @click.stop="selectFeature(geometryIndex, $event)"
                :d="linePath(geometry.coordinates)"
                fill="none"
                :stroke="selectedIndices.has(geometryIndex) ? '#dc2626' : '#0f766e'"
                :stroke-width="strokeWidth"
                stroke-linejoin="round"
                stroke-linecap="round" />
              <template v-else-if="geometry.type === 'MultiLineString'">
                <path
                  v-for="(line, lineIndex) in geometry.coordinates"
                  :key="lineIndex"
                  @click.stop="selectFeature(geometryIndex, $event)"
                  :d="linePath(line)"
                  fill="none"
                  :stroke="selectedIndices.has(geometryIndex) ? '#dc2626' : '#0f766e'"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round"
                  stroke-linecap="round" />
              </template>
              <path
                v-else-if="geometry.type === 'Polygon'"
                @click.stop="selectFeature(geometryIndex, $event)"
                :d="polygonPath(geometry.coordinates)"
                :fill="selectedIndices.has(geometryIndex) ? '#dc2626' : '#16a34a'"
                fill-opacity="0.28"
                fill-rule="evenodd"
                :stroke="selectedIndices.has(geometryIndex) ? '#b91c1c' : '#15803d'"
                :stroke-width="strokeWidth"
                stroke-linejoin="round" />
              <template v-else-if="geometry.type === 'MultiPolygon'">
                <path
                  v-for="(polygon, polygonIndex) in geometry.coordinates"
                  :key="polygonIndex"
                  @click.stop="selectFeature(geometryIndex, $event)"
                  :d="polygonPath(polygon)"
                  :fill="selectedIndices.has(geometryIndex) ? '#dc2626' : '#16a34a'"
                  fill-opacity="0.28"
                  fill-rule="evenodd"
                  :stroke="selectedIndices.has(geometryIndex) ? '#b91c1c' : '#15803d'"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round" />
              </template>
            </template>
          </g>
          <rect v-if="selectionRect" data-testid="selection-rectangle" :x="selectionRect[0]" :y="-selectionRect[3]" :width="selectionRect[2] - selectionRect[0]" :height="selectionRect[3] - selectionRect[1]" fill="#2878d3" fill-opacity="0.14" stroke="#2878d3" stroke-width="1" vector-effect="non-scaling-stroke" stroke-dasharray="4 3" pointer-events="none" />
        </svg>
          <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">暂无可绘制的几何要素</div>
          <span v-if="showBasemap && basemapTiles.length" class="absolute bottom-2 right-2 rounded bg-white/85 px-1.5 py-0.5 text-[10px] text-gray-600">© OpenStreetMap contributors</span>
        <div v-if="selectionMessage" role="status" class="pointer-events-none absolute bottom-3 left-3 max-w-[min(420px,calc(100%-24px))] rounded border border-[#f0c36a] bg-white/95 px-3 py-2 text-xs text-[var(--text-secondary)] shadow-sm">
          {{ selectionMessage }}
        </div>
        </div>
      </div>

      <div class="flex min-h-0 flex-col overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]">
        <div class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-main)] px-3 py-2 text-xs text-[var(--text-tertiary)]">
          <span>属性表 · {{ fields.length }} 个原始字段</span>
          <label class="flex items-center gap-2">显示
            <select v-model="tableFilter" aria-label="属性表范围" class="rounded border border-[var(--border-main)] bg-white px-2 py-1">
              <option value="all">全部记录（{{ tableRows.length }}）</option>
              <option value="selected">仅已选记录（{{ selectedAttributeCount }}）</option>
            </select>
          </label>
          <span>已选 {{ selectedIndices.size }} 条 · 共 {{ tableRows.length }} 条属性记录</span>
        </div>
        <div ref="tableElement" class="min-h-0 flex-1 overflow-auto">
          <table v-if="fields.length && previewRows.length" class="w-full min-w-[720px] text-left text-xs">
            <thead class="sticky top-0 bg-[var(--background-menu-white)] text-[var(--text-tertiary)]">
              <tr>
                <th v-for="field in fields" :key="field" class="border-b border-[var(--border-main)] px-3 py-2">{{ field }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--border-main)]">
              <tr v-for="row in previewRows" :key="row.recordIndex" :data-record-index="row.recordIndex" :aria-selected="selectedIndices.has(row.recordIndex)" :class="selectedIndices.has(row.recordIndex) ? 'bg-[#fff7e6]' : ''" class="cursor-pointer" tabindex="0" @click="selectRow(row.recordIndex, $event)" @keydown.enter.prevent="selectRow(row.recordIndex)">
                <td v-for="field in fields" :key="field" class="max-w-[220px] truncate px-3 py-2 text-[var(--text-secondary)]" :title="String(row.values[field] ?? '')">
                  {{ row.values[field] ?? '-' }}
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="flex h-full items-center justify-center px-3 text-sm text-[var(--text-tertiary)]">{{ tableFilter === 'selected' ? '当前选择没有可显示的属性记录' : '未找到 DBF 属性数据' }}</div>
        </div>
        <div class="flex shrink-0 items-center justify-end gap-3 border-t border-[var(--border-main)] px-3 py-1.5 text-xs text-[var(--text-secondary)]">
          <span>共 {{ filteredRows.length }} 条 · 每页 {{ pageSize }} 条 · 第 {{ tablePage }} / {{ pageCount }} 页</span>
          <button type="button" class="disabled:opacity-40" :disabled="tablePage <= 1" @click="tablePage--">上一页</button>
          <button type="button" class="disabled:opacity-40" :disabled="tablePage >= pageCount" @click="tablePage++">下一页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import { loadPluginBytes } from '../../visualizations/runtime';
import type { VisualizationPlugin } from '../../visualizations/contract';
import { usePreviewLoad, type PreviewLoad } from '../../composables/usePreviewLoad';
import { useFilePanel } from '../../composables/useFilePanel';
import { filePreviewIdentity, pluginPreviewIdentity } from '../../visualizations/previewIdentity';
import { geometryBounds, geometryIntersectsBounds, polygonFromShapefileRings, type Bounds, type Geometry, type Point } from './shapefileSelection';

type DbfRow = Record<string, string | number | boolean | null>;

interface DbfField {
  name: string;
  type: string;
  length: number;
  decimal: number;
}

const props = defineProps<{
  file: FileInfo;
  plugin: VisualizationPlugin;
}>();

const { relatedFiles } = useFilePanel();
const status = ref('');
const warnings = ref<string[]>([]);
const selectedLayerKey = ref('');
// Array positions are source record identities, not display row positions.
// Null SHP geometries and deleted DBF records must not shift later records.
const geometries = ref<Array<Geometry | null>>([]);
const attributes = ref<Array<DbfRow | null>>([]);
const attributeFields = ref<string[]>([]);
const projectionText = ref('');
const bounds = ref<[number, number, number, number] | null>(null);
const viewBounds = ref<[number, number, number, number] | null>(null);
const selectedIndices = ref<Set<number>>(new Set());
const showBasemap = ref(true);
const selectionMode = ref(false);
const selectionRect = ref<[number, number, number, number] | null>(null);
const selectionStart = ref<Point | null>(null);
const mapElement = ref<SVGSVGElement | null>(null);
const tableElement = ref<HTMLElement | null>(null);
const tableFilter = ref<'all' | 'selected'>('all');
const tablePage = ref(1);
const pageSize = 100;
const selectionAttempted = ref(false);
let pointerId: number | null = null;
let pointerTarget: SVGSVGElement | null = null;
let pointerStart: Point | null = null;
const loads = usePreviewLoad();

const renderFeatures = computed(() => geometries.value.flatMap((geometry, recordIndex) =>
  geometry && attributes.value[recordIndex] !== null && geometryBounds(geometry) ? [{ geometry, recordIndex }] : []));

const getExtension = (filename: string) => filename.split('.').pop()?.toLowerCase() || '';
const stripExtension = (filename: string) => filename.replace(/\.[^/.]+$/, '');
const logicalPath = (file: FileInfo) => String(file.metadata?.logical_path || file.relative_path || file.filename);
// Match the server's full logical stem, including directory and case.
const groupKey = (file: FileInfo) => stripExtension(logicalPath(file));

const layerChoices = computed(() => {
  const groups = new Map<string, { key: string; label: string; shp: FileInfo }>();
  for (const file of relatedFiles.value) {
    if (getExtension(file.filename) !== 'shp') continue;
    const key = groupKey(file);
    groups.set(key, { key, label: logicalPath(file).replace(/\.[^.]+$/, ''), shp: file });
  }
  return Array.from(groups.values());
});

const activeFile = computed(() => layerChoices.value.find(layer => layer.key === selectedLayerKey.value)?.shp || props.file);
const shapefileGroup = computed(() => {
  const source = activeFile.value;
  const key = groupKey(source);
  return new Map([...relatedFiles.value, props.file, source]
    .filter(file => groupKey(file) === key).map(file => [getExtension(file.filename), file]));
});
const groupExtensions = ['shp', 'dbf', 'prj', 'shx', 'cpg'];
const loadIdentity = computed(() => JSON.stringify([
  groupKey(activeFile.value), pluginPreviewIdentity(props.plugin),
  ...groupExtensions.map(extension => {
    const file = shapefileGroup.value.get(extension);
    return file ? filePreviewIdentity(file) : null;
  }),
  // The clicked entry and the related-file list can refresh independently.
  // Observe both sources of version evidence: one stale DTO must not mask a
  // newer revision. Actual reads still use the authorized server-side file ID.
  groupKey(props.file) === groupKey(activeFile.value) ? filePreviewIdentity(props.file) : null,
  relatedFiles.value.filter(file => groupKey(file) === groupKey(activeFile.value)
    && groupExtensions.includes(getExtension(file.filename))).map(filePreviewIdentity).sort(),
]));

const summary = computed(() => {
  if (!renderFeatures.value.length) return '';
  const types = Array.from(new Set(renderFeatures.value.map(({ geometry }) => geometry.type))).join(', ');
  return `${renderFeatures.value.length} 个要素 · ${types}`;
});

const projectionSummary = computed(() => {
  if (!projectionText.value) return '坐标系未知';
  const match = projectionText.value.match(/PROJCS\["([^"]+)"|GEOGCS\["([^"]+)"/);
  return match?.[1] || match?.[2] || '包含 PRJ 坐标系定义';
});

const fields = computed(() => {
  const names = new Set(attributeFields.value);
  attributes.value.forEach((row) => row && Object.keys(row).forEach((key) => names.add(key)));
  return Array.from(names);
});

const tableRows = computed(() => attributes.value.flatMap((values, recordIndex) => values ? [{ recordIndex, values }] : []));
const selectedAttributeCount = computed(() => tableRows.value.filter(row => selectedIndices.value.has(row.recordIndex)).length);
const filteredRows = computed(() => tableFilter.value === 'selected'
  ? tableRows.value.filter(row => selectedIndices.value.has(row.recordIndex)) : tableRows.value);
const pageCount = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize)));
const previewRows = computed(() => filteredRows.value.slice((tablePage.value - 1) * pageSize, tablePage.value * pageSize));
const selectionMessage = computed(() => {
  if (selectedIndices.value.size) {
    const geometryCount = renderFeatures.value.filter(feature => selectedIndices.value.has(feature.recordIndex)).length;
    const missingAttributes = selectedIndices.value.size - selectedAttributeCount.value;
    return `已选择 ${selectedIndices.value.size} 条记录 · ${geometryCount} 个完整要素${missingAttributes ? ` · ${missingAttributes} 条无可用属性` : ''}${geometryCount < selectedIndices.value.size ? ' · 部分记录无可绘制几何' : ''}`;
  }
  if (selectionAttempted.value) return '框选未命中任何要素';
  return selectionMode.value ? '拖动选择相交的完整要素，不裁剪几何；Esc 退出' : '';
});

watch(tableFilter, () => { tablePage.value = 1; }, { flush: 'sync' });
watch(pageCount, count => { tablePage.value = Math.min(tablePage.value, count); });
watch(tablePage, () => { if (tableElement.value) tableElement.value.scrollTop = 0; });

const viewBox = computed(() => {
  if (!viewBounds.value) return '';
  const [minX, minY, maxX, maxY] = viewBounds.value;
  const width = Math.max(maxX - minX, 1e-12);
  const height = Math.max(maxY - minY, 1e-12);
  const pad = Math.max(width, height) * 0.04;
  return `${minX - pad} ${-maxY - pad} ${width + pad * 2} ${height + pad * 2}`;
});

interface BasemapTile { key: string; url: string; x: number; y: number; width: number; height: number }
const basemapTiles = computed<BasemapTile[]>(() => {
  const extent = bounds.value;
  if (!showBasemap.value || !extent || !/WGS.*84|4326|GCS_WGS/i.test(projectionText.value)) return [];
  const [minLon, minLat, maxLon, maxLat] = extent;
  if (minLon < -180 || maxLon > 180 || minLat < -85 || maxLat > 85) return [];
  const zoomLevel = Math.max(2, Math.min(12, Math.round(Math.log2(360 / Math.max(maxLon - minLon, 0.05))) - 1));
  const n = 2 ** zoomLevel;
  const lonToX = (lon: number) => ((lon + 180) / 360) * n;
  const latToY = (lat: number) => (1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2 * n;
  const startX = Math.max(0, Math.floor(lonToX(minLon)) - 1);
  const endX = Math.min(n - 1, Math.floor(lonToX(maxLon)) + 1);
  const startY = Math.max(0, Math.floor(latToY(maxLat)) - 1);
  const endY = Math.min(n - 1, Math.floor(latToY(minLat)) + 1);
  const xToLon = (x: number) => x / n * 360 - 180;
  const yToLat = (y: number) => (180 / Math.PI) * Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n)));
  const tiles: BasemapTile[] = [];
  for (let x = startX; x <= endX; x += 1) {
    for (let y = startY; y <= endY; y += 1) {
      const west = xToLon(x); const east = xToLon(x + 1);
      const north = yToLat(y); const south = yToLat(y + 1);
      tiles.push({ key: `${zoomLevel}/${x}/${y}`, url: `https://tile.openstreetmap.org/${zoomLevel}/${x}/${y}.png`, x: west, y: -north, width: east - west, height: north - south });
    }
  }
  return tiles;
});

const pointRadius = computed(() => {
  if (!viewBounds.value) return 1;
  const [minX, minY, maxX, maxY] = viewBounds.value;
  return Math.max(maxX - minX, maxY - minY, 1e-12) * 0.004;
});

const strokeWidth = computed(() => pointRadius.value * 0.6);

const flipY = (y: number) => -y;
const locateAttribute = (index: number) => {
  const position = filteredRows.value.findIndex(row => row.recordIndex === index);
  if (position < 0) return;
  tablePage.value = Math.floor(position / pageSize) + 1;
  void nextTick(() => {
    if (!selectedIndices.value.has(index)) return;
    const container = tableElement.value;
    const row = container?.querySelector<HTMLElement>(`[data-record-index="${index}"]`);
    if (container && row) {
      // Scroll only the table, never the surrounding conversation/page.
      container.scrollTop += row.getBoundingClientRect().top - container.getBoundingClientRect().top - 32;
    }
  });
};
const selectRecord = (index: number, event?: MouseEvent) => {
  if (!Number.isInteger(index) || index < 0 || (!geometries.value[index] && !attributes.value[index])) return;
  const next = event && (event.ctrlKey || event.metaKey || event.shiftKey) ? new Set(selectedIndices.value) : new Set<number>();
  if (next.has(index)) next.delete(index); else next.add(index);
  selectedIndices.value = next;
  selectionAttempted.value = false;
  selectionRect.value = null;
  locateAttribute(index);
};
const selectFeature = (index: number, event?: MouseEvent) => {
  // Pointer-up from a box drag can synthesize a click on the underlying path.
  if (selectionMode.value || pointerId !== null) return;
  selectRecord(index, event);
};
const selectRow = (index: number, event?: MouseEvent) => {
  selectRecord(index, event);
  const extent = geometryBounds(geometries.value[index] ?? null);
  if (extent) viewBounds.value = usableViewBounds(extent);
};
const svgDataPoint = (event: PointerEvent): Point | null => {
  const target = event.currentTarget as SVGSVGElement | null;
  if (!target || !viewBounds.value) return null;
  try {
    const matrix = target.getScreenCTM();
    if (!matrix) return null;
    const screenPoint = target.createSVGPoint();
    screenPoint.x = event.clientX;
    screenPoint.y = event.clientY;
    const point = screenPoint.matrixTransform(matrix.inverse());
    return Number.isFinite(point.x) && Number.isFinite(point.y) ? [point.x, -point.y] : null;
  } catch { return null; }
};
const releasePointer = () => {
  const target = pointerTarget;
  const id = pointerId;
  pointerId = null;
  pointerTarget = null;
  pointerStart = null;
  selectionStart.value = null;
  if (target && id !== null && target.hasPointerCapture(id)) target.releasePointerCapture(id);
};
const cancelSelection = (event?: PointerEvent) => {
  if (event && (pointerId === null || event.pointerId !== pointerId)) return;
  releasePointer();
  selectionRect.value = null;
};
const exitSelectionMode = () => {
  cancelSelection();
  selectionMode.value = false;
};
const toggleSelectionMode = () => {
  if (selectionMode.value) exitSelectionMode();
  else { cancelSelection(); selectionMode.value = true; mapElement.value?.focus({ preventScroll: true }); }
};
const clearSelection = () => {
  cancelSelection();
  selectedIndices.value = new Set();
  selectionAttempted.value = false;
  tableFilter.value = 'all';
  tablePage.value = 1;
};
const startSelection = (event: PointerEvent) => {
  if (!selectionMode.value || event.button !== 0 || event.isPrimary === false || pointerId !== null) return;
  const point = svgDataPoint(event);
  if (!point) return;
  event.preventDefault();
  pointerTarget = event.currentTarget as SVGSVGElement;
  pointerTarget.focus({ preventScroll: true });
  pointerId = event.pointerId;
  pointerStart = [event.clientX, event.clientY];
  selectionStart.value = point;
  selectionRect.value = null;
  try { pointerTarget.setPointerCapture(event.pointerId); } catch { cancelSelection(); }
};
const moveSelection = (event: PointerEvent) => {
  if (!selectionMode.value || !selectionStart.value || event.pointerId !== pointerId) return;
  event.preventDefault();
  const current = svgDataPoint(event);
  if (!current) return;
  selectionRect.value = [Math.min(selectionStart.value[0], current[0]), Math.min(selectionStart.value[1], current[1]), Math.max(selectionStart.value[0], current[0]), Math.max(selectionStart.value[1], current[1])];
};
const finishSelection = (event: PointerEvent) => {
  if (!selectionMode.value || !selectionStart.value || !pointerStart || event.pointerId !== pointerId) return;
  moveSelection(event);
  const selected = selectionRect.value;
  const dragged = Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) >= 3;
  releasePointer();
  if (!selected || !dragged) { selectionRect.value = null; return; }
  selectedIndices.value = new Set(renderFeatures.value
    .filter(({ geometry }) => geometryIntersectsBounds(geometry, selected)).map(({ recordIndex }) => recordIndex));
  selectionAttempted.value = true;
  tableFilter.value = 'selected';
  tablePage.value = 1;
};
const usableViewBounds = (extent: Bounds): Bounds => {
  const [minX, minY, maxX, maxY] = extent;
  const fallback = Math.max(maxX - minX, maxY - minY) || 1;
  return [minX === maxX ? minX - fallback / 2 : minX, minY === maxY ? minY - fallback / 2 : minY,
    minX === maxX ? maxX + fallback / 2 : maxX, minY === maxY ? maxY + fallback / 2 : maxY];
};
const resetView = () => { cancelSelection(); viewBounds.value = bounds.value ? usableViewBounds(bounds.value) : null; };
const zoom = (factor: number) => {
  if (!viewBounds.value || !Number.isFinite(factor) || factor <= 0) return;
  cancelSelection();
  const [minX, minY, maxX, maxY] = viewBounds.value;
  const centerX = (minX + maxX) / 2; const centerY = (minY + maxY) / 2;
  const width = (maxX - minX) * factor; const height = (maxY - minY) * factor;
  viewBounds.value = [centerX - width / 2, centerY - height / 2, centerX + width / 2, centerY + height / 2];
};
const linePath = (points: Point[]) => points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point[0]} ${flipY(point[1])}`).join(' ');
const polygonPath = (rings: Point[][]) => rings.map((ring) => `${linePath(ring)} Z`).join(' ');

const readInt32BE = (view: DataView, offset: number) => view.getInt32(offset, false);
const readInt32LE = (view: DataView, offset: number) => view.getInt32(offset, true);
const readDoubleLE = (view: DataView, offset: number) => view.getFloat64(offset, true);

const readPoint = (view: DataView, offset: number): Point => [readDoubleLE(view, offset), readDoubleLE(view, offset + 8)];

const readParts = (view: DataView, offset: number, numParts: number, numPoints: number, recordEnd: number) => {
  if (numParts < 0 || numPoints < 0 || (numParts === 0) !== (numPoints === 0)
    || offset + numParts * 4 + numPoints * 16 > recordEnd) throw new Error('Invalid SHP part bounds');
  const parts: number[] = [];
  for (let index = 0; index < numParts; index += 1) {
    const part = readInt32LE(view, offset + index * 4);
    if (part < 0 || part >= numPoints || (index === 0 ? part !== 0 : part <= parts[index - 1])) throw new Error('Invalid SHP part index');
    parts.push(part);
  }
  const pointOffset = offset + numParts * 4;
  return parts.map((start, index) => {
    const end = parts[index + 1] ?? numPoints;
    const points: Point[] = [];
    for (let pointIndex = start; pointIndex < end; pointIndex += 1) {
      points.push(readPoint(view, pointOffset + pointIndex * 16));
    }
    return points;
  });
};

const parseShp = (buffer: ArrayBuffer): Array<Geometry | null> => {
  const view = new DataView(buffer);
  if (view.byteLength < 100 || readInt32BE(view, 0) !== 9994) {
    throw new Error('Invalid Shapefile header');
  }

  const parsed: Array<Geometry | null> = [];
  let offset = 100;
  while (offset + 8 <= view.byteLength) {
    const contentLengthBytes = readInt32BE(view, offset + 4) * 2;
    const recordOffset = offset + 8;
    const recordEnd = recordOffset + contentLengthBytes;
    if (contentLengthBytes < 4 || recordEnd > view.byteLength) throw new Error('Invalid SHP record bounds');

    const shapeType = readInt32LE(view, recordOffset);
    if (shapeType === 1 || shapeType === 11 || shapeType === 21) {
      if (contentLengthBytes < 20) throw new Error('Invalid SHP point');
      parsed.push({ type: 'Point', coordinates: readPoint(view, recordOffset + 4) });
    } else if (shapeType === 3 || shapeType === 13 || shapeType === 23) {
      if (contentLengthBytes < 44) throw new Error('Invalid SHP line');
      const numParts = readInt32LE(view, recordOffset + 36);
      const numPoints = readInt32LE(view, recordOffset + 40);
      const lines = readParts(view, recordOffset + 44, numParts, numPoints, recordEnd);
      parsed.push(lines.length === 1 ? { type: 'LineString', coordinates: lines[0] } : { type: 'MultiLineString', coordinates: lines });
    } else if (shapeType === 5 || shapeType === 15 || shapeType === 25) {
      if (contentLengthBytes < 44) throw new Error('Invalid SHP polygon');
      const numParts = readInt32LE(view, recordOffset + 36);
      const numPoints = readInt32LE(view, recordOffset + 40);
      const rings = readParts(view, recordOffset + 44, numParts, numPoints, recordEnd);
      parsed.push(polygonFromShapefileRings(rings));
    } else if (shapeType === 8 || shapeType === 18 || shapeType === 28) {
      if (contentLengthBytes < 40) throw new Error('Invalid SHP multipoint');
      const numPoints = readInt32LE(view, recordOffset + 36);
      if (numPoints < 0 || recordOffset + 40 + numPoints * 16 > recordEnd) throw new Error('Invalid SHP point bounds');
      const points: Point[] = [];
      for (let index = 0; index < numPoints; index += 1) {
        points.push(readPoint(view, recordOffset + 40 + index * 16));
      }
      parsed.push({ type: 'MultiPoint', coordinates: points });
    } else {
      parsed.push(null);
    }
    offset = recordEnd;
  }
  return parsed;
};

const decodeDbfText = (bytes: Uint8Array) => {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes).trim();
  return text.replace(/\0/g, '').trim();
};

const parseDbfValue = (raw: string, field: DbfField) => {
  if (!raw) return null;
  if (field.type === 'N' || field.type === 'F') {
    const value = Number(raw);
    return Number.isFinite(value) ? value : raw;
  }
  if (field.type === 'L') {
    if (/^[YyTt]$/.test(raw)) return true;
    if (/^[NnFf]$/.test(raw)) return false;
  }
  return raw;
};

const parseDbf = (buffer: ArrayBuffer, fieldNames: string[] = []): Array<DbfRow | null> => {
  const view = new DataView(buffer);
  if (view.byteLength < 33) throw new Error('Invalid DBF header');
  const recordCount = view.getUint32(4, true);
  const headerLength = view.getUint16(8, true);
  const recordLength = view.getUint16(10, true);
  if (headerLength < 33 || headerLength > buffer.byteLength || recordLength < 1
      || headerLength + recordCount * recordLength > buffer.byteLength) {
    throw new Error('Invalid DBF record bounds');
  }
  const fieldsList: DbfField[] = [];

  for (let offset = 32; offset + 32 <= headerLength && new Uint8Array(buffer)[offset] !== 0x0d; offset += 32) {
    const descriptor = new Uint8Array(buffer, offset, 32);
    fieldsList.push({
      name: decodeDbfText(descriptor.slice(0, 11)),
      type: String.fromCharCode(descriptor[11]),
      length: descriptor[16],
      decimal: descriptor[17],
    });
  }
  if (fieldsList.some(field => !field.name || field.length < 1)
    || new Set(fieldsList.map(field => field.name)).size !== fieldsList.length
    || 1 + fieldsList.reduce((total, field) => total + field.length, 0) > recordLength) throw new Error('Invalid DBF field layout');
  fieldNames.push(...fieldsList.map(field => field.name));

  const rows: Array<DbfRow | null> = [];
  const bytes = new Uint8Array(buffer);
  for (let recordIndex = 0; recordIndex < recordCount; recordIndex += 1) {
    const recordOffset = headerLength + recordIndex * recordLength;
    if (recordOffset + recordLength > bytes.length) break;
    if (bytes[recordOffset] === 0x2a) { rows.push(null); continue; }

    const row: Record<string, string | number | boolean | null> = {};
    let fieldOffset = recordOffset + 1;
    fieldsList.forEach((field) => {
      const raw = decodeDbfText(bytes.slice(fieldOffset, fieldOffset + field.length));
      Object.defineProperty(row, field.name, { value: parseDbfValue(raw, field), enumerable: true, configurable: true, writable: true });
      fieldOffset += field.length;
    });
    rows.push(row);
  }
  return rows;
};

const parseDbfTable = (buffer: ArrayBuffer) => {
  const fields: string[] = [];
  const rows = parseDbf(buffer, fields);
  return { fields, rows };
};

const calculateBounds = (items: Array<Geometry | null>): Bounds | null => {
  let combined: Bounds | null = null;
  for (const geometry of items) {
    const extent = geometryBounds(geometry);
    if (!extent) continue;
    combined = combined ? [Math.min(combined[0], extent[0]), Math.min(combined[1], extent[1]),
      Math.max(combined[2], extent[2]), Math.max(combined[3], extent[3])] : extent;
  }
  return combined;
};

const fetchBuffer = async (source: FileInfo, file: FileInfo, load: PreviewLoad, plugin: VisualizationPlugin) => {
  return loadPluginBytes(source, plugin, load.signal, file.file_id === source.file_id ? undefined : file);
};

const loadShapefile = async () => {
  clearSelection();
  selectionMode.value = false;
  const load = loads.begin();
  const plugin = props.plugin;
  status.value = '正在加载 Shapefile...';
  warnings.value = [];
  geometries.value = [];
  attributes.value = [];
  attributeFields.value = [];
  projectionText.value = '';
  bounds.value = null;
  viewBounds.value = null;

  try {
    const byExtension = new Map(shapefileGroup.value);
    const shp = byExtension.get('shp');
    const dbf = byExtension.get('dbf');
    const prj = byExtension.get('prj');

    if (!shp) {
      status.value = '未找到同名 .shp 文件。请从文件列表点击 .shp 文件，或确保同名文件已上传到当前任务。';
      return;
    }
    const geometryBuffer = await fetchBuffer(shp, shp, load, plugin);
    if (!load.isCurrent()) return;
    const parsedGeometries = parseShp(geometryBuffer);
    geometries.value = parsedGeometries;
    bounds.value = calculateBounds(parsedGeometries);
    viewBounds.value = bounds.value ? usableViewBounds(bounds.value) : null;
    const initialView = viewBounds.value;
    status.value = '';

    if (dbf) {
      try {
        const attributeBuffer = await fetchBuffer(shp, dbf, load, plugin);
        if (!load.isCurrent()) return;
        const table = parseDbfTable(attributeBuffer);
        if (table.rows.length !== parsedGeometries.length) throw new Error('SHP/DBF record count mismatch');
        attributes.value = table.rows;
        attributeFields.value = table.fields;
        // Deleted rows remain positional placeholders but are not selectable.
        selectedIndices.value = new Set([...selectedIndices.value].filter(index => table.rows[index] !== null));
        bounds.value = calculateBounds(renderFeatures.value.map(feature => feature.geometry));
        if (viewBounds.value === initialView) viewBounds.value = bounds.value ? usableViewBounds(bounds.value) : null;
        const firstSelected = selectedIndices.value.values().next().value;
        if (firstSelected !== undefined) locateAttribute(firstSelected);
      } catch {
        if (!load.isCurrent()) return;
        warnings.value.push('DBF 属性文件读取、解析或与几何的记录对应检查失败，几何仍可预览。请重新打开预览检查配套文件。');
      }
    } else {
      warnings.value.push('缺少同名 .dbf 文件，当前仅显示几何，不显示属性表。');
    }
    if (prj) {
      try {
        const buffer = await fetchBuffer(shp, prj, load, plugin);
        if (!load.isCurrent()) return;
        const text = new TextDecoder('utf-8', { fatal: true }).decode(buffer).trim();
        if (!text) throw new Error('Empty projection');
        projectionText.value = text;
      } catch {
        if (!load.isCurrent()) return;
        warnings.value.push('PRJ 坐标系文件读取失败，按原始坐标显示几何，暂不叠加地图底图。');
      }
    } else {
      warnings.value.push('缺少同名 .prj 文件，坐标系未知；按原始坐标显示几何，暂不叠加地图底图。');
    }
  } catch {
    if (!load.isCurrent()) return;
    geometries.value = [];
    bounds.value = viewBounds.value = null;
    status.value = 'SHP 几何文件读取或解析失败。请重新打开预览；若仍失败，请检查文件访问状态与格式。';
  }
};

onBeforeUnmount(() => { cancelSelection(); });

watch(() => JSON.stringify([props.file.file_id, groupKey(props.file)]), () => {
  selectedLayerKey.value = groupKey(props.file);
}, { immediate: true });
watch(loadIdentity, () => {
  void loadShapefile();
}, { immediate: true });
</script>
