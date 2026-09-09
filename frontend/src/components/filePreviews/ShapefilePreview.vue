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
            <button type="button" class="size-8 text-lg text-[var(--text-secondary)] hover:bg-gray-50" title="放大" @click="zoom(1.35)">+</button>
            <button type="button" class="size-8 border-t border-[var(--border-main)] text-lg text-[var(--text-secondary)] hover:bg-gray-50" title="缩小" @click="zoom(0.74)">−</button>
            <button type="button" class="size-8 border-t border-[var(--border-main)] text-xs text-[var(--text-secondary)] hover:bg-gray-50" title="复位" @click="resetView">⌂</button>
          </div>
          <label class="absolute right-3 top-3 z-10 flex items-center gap-2 rounded border border-[var(--border-main)] bg-white px-2 py-1.5 text-xs text-[var(--text-secondary)] shadow-sm">
            <input v-model="showBasemap" type="checkbox" class="accent-[#2878d3]" /> 地图底图
          </label>
          <button type="button" class="absolute right-3 top-12 z-10 rounded border px-2 py-1.5 text-xs shadow-sm" :class="selectionMode ? 'border-[#2878d3] bg-[#e8f1ec] text-[#245b42]' : 'border-[var(--border-main)] bg-white text-[var(--text-secondary)]'" @click="selectionMode = !selectionMode">{{ selectionMode ? '取消框选' : '框选区域' }}</button>
        <svg
          v-if="viewBox && geometries.length"
          ref="mapElement"
          class="h-full w-full cursor-crosshair"
          :viewBox="viewBox"
          preserveAspectRatio="xMidYMid meet"
          @pointerdown="startSelection"
          @pointermove="moveSelection"
          @pointerup="finishSelection"
          @pointerleave="finishSelection">
          <template v-if="showBasemap && basemapTiles.length">
            <image v-for="tile in basemapTiles" :key="tile.key" :href="tile.url" :x="tile.x" :y="tile.y" :width="tile.width" :height="tile.height" preserveAspectRatio="none" opacity="0.72" />
          </template>
          <g>
            <template v-for="(geometry, geometryIndex) in geometries" :key="`${geometry.type}-${geometryIndex}`">
              <circle
                v-if="geometry.type === 'Point'"
                @click.stop="selectFeature(geometryIndex)"
                :cx="geometry.coordinates[0]"
                :cy="flipY(geometry.coordinates[1])"
                :r="pointRadius"
                :fill="selectedIndex === geometryIndex ? '#dc2626' : '#2563eb'"
                fill-opacity="0.85" />
              <template v-else-if="geometry.type === 'MultiPoint'">
                <circle
                  v-for="(point, pointIndex) in geometry.coordinates"
                  :key="pointIndex"
                  @click.stop="selectFeature(geometryIndex)"
                  :cx="point[0]"
                  :cy="flipY(point[1])"
                  :r="pointRadius"
                  :fill="selectedIndex === geometryIndex ? '#dc2626' : '#2563eb'"
                  fill-opacity="0.85" />
              </template>
              <path
                v-else-if="geometry.type === 'LineString'"
                @click.stop="selectFeature(geometryIndex)"
                :d="linePath(geometry.coordinates)"
                fill="none"
                :stroke="selectedIndex === geometryIndex ? '#dc2626' : '#0f766e'"
                :stroke-width="strokeWidth"
                stroke-linejoin="round"
                stroke-linecap="round" />
              <template v-else-if="geometry.type === 'MultiLineString'">
                <path
                  v-for="(line, lineIndex) in geometry.coordinates"
                  :key="lineIndex"
                  @click.stop="selectFeature(geometryIndex)"
                  :d="linePath(line)"
                  fill="none"
                :stroke="selectedIndex === geometryIndex ? '#dc2626' : '#0f766e'"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round"
                  stroke-linecap="round" />
              </template>
              <path
                v-else-if="geometry.type === 'Polygon'"
                @click.stop="selectFeature(geometryIndex)"
                :d="polygonPath(geometry.coordinates)"
                :fill="selectedIndex === geometryIndex ? '#dc2626' : '#16a34a'"
                fill-opacity="0.28"
                :stroke="selectedIndex === geometryIndex ? '#b91c1c' : '#15803d'"
                :stroke-width="strokeWidth"
                stroke-linejoin="round" />
              <template v-else-if="geometry.type === 'MultiPolygon'">
                <path
                  v-for="(polygon, polygonIndex) in geometry.coordinates"
                  :key="polygonIndex"
                  @click.stop="selectFeature(geometryIndex)"
                  :d="polygonPath(polygon)"
                  :fill="selectedIndex === geometryIndex ? '#dc2626' : '#16a34a'"
                  fill-opacity="0.28"
                  :stroke="selectedIndex === geometryIndex ? '#b91c1c' : '#15803d'"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round" />
              </template>
            </template>
          </g>
          <rect v-if="selectionRect" :x="selectionRect[0]" :y="-selectionRect[3]" :width="selectionRect[2] - selectionRect[0]" :height="selectionRect[3] - selectionRect[1]" fill="#2878d3" fill-opacity="0.14" stroke="#2878d3" stroke-dasharray="4 3" />
        </svg>
          <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">暂无可绘制的几何要素</div>
          <span v-if="showBasemap && basemapTiles.length" class="absolute bottom-2 right-2 rounded bg-white/85 px-1.5 py-0.5 text-[10px] text-gray-600">© OpenStreetMap contributors</span>
        <div v-if="selectedIndex !== null" class="absolute bottom-3 left-3 max-w-[min(420px,calc(100%-24px))] rounded border border-[#f0c36a] bg-white/95 px-3 py-2 text-xs text-[var(--text-secondary)] shadow-sm">
          已选择第 {{ selectedIndex + 1 }} 个要素 · 可在下方属性表中查看详情
        </div>
        </div>
      </div>

      <div class="min-h-0 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]">
        <div class="flex items-center justify-between border-b border-[var(--border-main)] px-3 py-2 text-xs text-[var(--text-tertiary)]">
          <span>属性表预览</span>
          <span>前 {{ previewRows.length }} / {{ attributes.length }} 条</span>
        </div>
        <div class="h-[176px] overflow-auto">
          <table v-if="fields.length && previewRows.length" class="w-full min-w-[720px] text-left text-xs">
            <thead class="sticky top-0 bg-[var(--background-menu-white)] text-[var(--text-tertiary)]">
              <tr>
                <th v-for="field in fields" :key="field" class="border-b border-[var(--border-main)] px-3 py-2">{{ field }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--border-main)]">
              <tr v-for="(row, rowIndex) in previewRows" :key="rowIndex" :class="selectedIndex === rowIndex ? 'bg-[#fff7e6]' : ''" class="cursor-pointer" @click="selectedIndex = rowIndex">
                <td v-for="field in fields" :key="field" class="max-w-[220px] truncate px-3 py-2 text-[var(--text-secondary)]" :title="String(row[field] ?? '')">
                  {{ row[field] ?? '-' }}
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">未找到 DBF 属性数据</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { getFileDownloadUrl, type FileInfo } from '../../api/file';
import { usePreviewLoad, type PreviewLoad } from '../../composables/usePreviewLoad';
import { useFilePanel } from '../../composables/useFilePanel';

type Point = [number, number];

type Geometry =
  | { type: 'Point'; coordinates: Point }
  | { type: 'MultiPoint'; coordinates: Point[] }
  | { type: 'LineString'; coordinates: Point[] }
  | { type: 'MultiLineString'; coordinates: Point[][] }
  | { type: 'Polygon'; coordinates: Point[][] }
  | { type: 'MultiPolygon'; coordinates: Point[][][] };

interface DbfField {
  name: string;
  type: string;
  length: number;
  decimal: number;
}

const props = defineProps<{
  file: FileInfo;
}>();

const { relatedFiles } = useFilePanel();
const status = ref('');
const selectedLayerKey = ref('');
const geometries = ref<Geometry[]>([]);
const attributes = ref<Array<Record<string, string | number | boolean | null>>>([]);
const projectionText = ref('');
const bounds = ref<[number, number, number, number] | null>(null);
const viewBounds = ref<[number, number, number, number] | null>(null);
const selectedIndex = ref<number | null>(null);
const showBasemap = ref(true);
const selectionMode = ref(false);
const selectionRect = ref<[number, number, number, number] | null>(null);
const selectionStart = ref<Point | null>(null);
const loads = usePreviewLoad();

const getExtension = (filename: string) => filename.split('.').pop()?.toLowerCase() || '';
const stripExtension = (filename: string) => filename.replace(/\.[^/.]+$/, '');
const logicalPath = (file: FileInfo) => String(file.metadata?.logical_path || file.relative_path || file.filename);
const groupKey = (file: FileInfo) => stripExtension(logicalPath(file)).toLowerCase();

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

const summary = computed(() => {
  if (!geometries.value.length) return '';
  const types = Array.from(new Set(geometries.value.map((geometry) => geometry.type))).join(', ');
  return `${geometries.value.length} 个要素 · ${types}`;
});

const projectionSummary = computed(() => {
  if (!projectionText.value) return '坐标系未知';
  const match = projectionText.value.match(/PROJCS\["([^"]+)"|GEOGCS\["([^"]+)"/);
  return match?.[1] || match?.[2] || '包含 PRJ 坐标系定义';
});

const fields = computed(() => {
  const names = new Set<string>();
  attributes.value.slice(0, 20).forEach((row) => Object.keys(row).forEach((key) => names.add(key)));
  return Array.from(names).slice(0, 12);
});

const previewRows = computed(() => attributes.value.slice(0, 100));

const viewBox = computed(() => {
  if (!viewBounds.value) return '';
  const [minX, minY, maxX, maxY] = viewBounds.value;
  const width = Math.max(maxX - minX, 1);
  const height = Math.max(maxY - minY, 1);
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
  if (!bounds.value) return 1;
  const [minX, minY, maxX, maxY] = bounds.value;
  return Math.max(maxX - minX, maxY - minY, 1) * 0.004;
});

const strokeWidth = computed(() => pointRadius.value * 0.6);

const flipY = (y: number) => -y;
const selectFeature = (index: number) => { selectedIndex.value = index; };
const svgDataPoint = (event: PointerEvent): Point | null => {
  const target = event.currentTarget as SVGSVGElement | null;
  if (!target || !viewBounds.value) return null;
  const rect = target.getBoundingClientRect();
  const [minX, minY, maxX, maxY] = viewBounds.value;
  const x = minX + ((event.clientX - rect.left) / rect.width) * (maxX - minX);
  const displayY = -maxY + ((event.clientY - rect.top) / rect.height) * (maxY - minY);
  return [x, -displayY];
};
const startSelection = (event: PointerEvent) => {
  if (!selectionMode.value) return;
  selectionStart.value = svgDataPoint(event);
  selectionRect.value = null;
};
const moveSelection = (event: PointerEvent) => {
  if (!selectionMode.value || !selectionStart.value) return;
  const current = svgDataPoint(event);
  if (!current) return;
  selectionRect.value = [Math.min(selectionStart.value[0], current[0]), Math.min(selectionStart.value[1], current[1]), Math.max(selectionStart.value[0], current[0]), Math.max(selectionStart.value[1], current[1])];
};
const finishSelection = (event: PointerEvent) => {
  if (!selectionMode.value || !selectionStart.value) return;
  moveSelection(event);
  const selected = selectionRect.value;
  selectionStart.value = null;
  if (!selected) return;
  const overlaps = (geometry: Geometry) => {
    const extent = calculateBounds([geometry]);
    return extent && extent[2] >= selected[0] && extent[0] <= selected[2] && extent[3] >= selected[1] && extent[1] <= selected[3];
  };
  const first = geometries.value.findIndex(overlaps);
  selectedIndex.value = first >= 0 ? first : null;
};
const resetView = () => { viewBounds.value = bounds.value ? [...bounds.value] as [number, number, number, number] : null; };
const zoom = (factor: number) => {
  if (!viewBounds.value) return;
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

const readParts = (view: DataView, offset: number, numParts: number, numPoints: number) => {
  const parts: number[] = [];
  for (let index = 0; index < numParts; index += 1) {
    parts.push(readInt32LE(view, offset + index * 4));
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

const parseShp = (buffer: ArrayBuffer): Geometry[] => {
  const view = new DataView(buffer);
  if (view.byteLength < 100 || readInt32BE(view, 0) !== 9994) {
    throw new Error('Invalid Shapefile header');
  }

  const parsed: Geometry[] = [];
  let offset = 100;
  while (offset + 8 <= view.byteLength) {
    const contentLengthBytes = readInt32BE(view, offset + 4) * 2;
    const recordOffset = offset + 8;
    if (contentLengthBytes <= 0 || recordOffset + contentLengthBytes > view.byteLength) break;

    const shapeType = readInt32LE(view, recordOffset);
    if (shapeType === 1 || shapeType === 11 || shapeType === 21) {
      parsed.push({ type: 'Point', coordinates: readPoint(view, recordOffset + 4) });
    } else if (shapeType === 3 || shapeType === 13 || shapeType === 23) {
      const numParts = readInt32LE(view, recordOffset + 36);
      const numPoints = readInt32LE(view, recordOffset + 40);
      const lines = readParts(view, recordOffset + 44, numParts, numPoints);
      parsed.push(lines.length === 1 ? { type: 'LineString', coordinates: lines[0] } : { type: 'MultiLineString', coordinates: lines });
    } else if (shapeType === 5 || shapeType === 15 || shapeType === 25) {
      const numParts = readInt32LE(view, recordOffset + 36);
      const numPoints = readInt32LE(view, recordOffset + 40);
      const rings = readParts(view, recordOffset + 44, numParts, numPoints);
      parsed.push({ type: 'Polygon', coordinates: rings });
    } else if (shapeType === 8 || shapeType === 18 || shapeType === 28) {
      const numPoints = readInt32LE(view, recordOffset + 36);
      const points: Point[] = [];
      for (let index = 0; index < numPoints; index += 1) {
        points.push(readPoint(view, recordOffset + 40 + index * 16));
      }
      parsed.push({ type: 'MultiPoint', coordinates: points });
    }
    offset = recordOffset + contentLengthBytes;
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

const parseDbf = (buffer: ArrayBuffer) => {
  const view = new DataView(buffer);
  if (view.byteLength < 32) return [];
  const recordCount = view.getUint32(4, true);
  const headerLength = view.getUint16(8, true);
  const recordLength = view.getUint16(10, true);
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

  const rows: Array<Record<string, string | number | boolean | null>> = [];
  const bytes = new Uint8Array(buffer);
  for (let recordIndex = 0; recordIndex < recordCount; recordIndex += 1) {
    const recordOffset = headerLength + recordIndex * recordLength;
    if (recordOffset + recordLength > bytes.length) break;
    if (bytes[recordOffset] === 0x2a) continue;

    const row: Record<string, string | number | boolean | null> = {};
    let fieldOffset = recordOffset + 1;
    fieldsList.forEach((field) => {
      const raw = decodeDbfText(bytes.slice(fieldOffset, fieldOffset + field.length));
      row[field.name] = parseDbfValue(raw, field);
      fieldOffset += field.length;
    });
    rows.push(row);
  }
  return rows;
};

const collectPoints = (geometry: Geometry): Point[] => {
  if (geometry.type === 'Point') return [geometry.coordinates];
  if (geometry.type === 'MultiPoint' || geometry.type === 'LineString') return geometry.coordinates;
  if (geometry.type === 'MultiLineString' || geometry.type === 'Polygon') return geometry.coordinates.flat();
  return geometry.coordinates.flat(2);
};

const calculateBounds = (items: Geometry[]): [number, number, number, number] | null => {
  const points = items.flatMap(collectPoints);
  if (!points.length) return null;
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};

const fetchBuffer = async (file: FileInfo, load: PreviewLoad) => {
  const url = await getFileDownloadUrl(file);
  load.assertCurrent();
  const response = await fetch(url, { signal: load.signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.arrayBuffer();
};

const loadShapefile = async () => {
  const load = loads.begin();
  status.value = '正在加载 Shapefile...';
  geometries.value = [];
  attributes.value = [];
  projectionText.value = '';
  bounds.value = null;
  viewBounds.value = null;
  selectedIndex.value = null;

  try {
    const currentKey = groupKey(activeFile.value);
    const candidates = relatedFiles.value.length ? relatedFiles.value : [props.file];
    const group = candidates.filter((file) => groupKey(file) === currentKey);
    const byExtension = new Map(group.map((file) => [getExtension(file.filename), file]));
    const shp = byExtension.get('shp') || (getExtension(activeFile.value.filename) === 'shp' ? activeFile.value : null);
    const dbf = byExtension.get('dbf');
    const prj = byExtension.get('prj');

    if (!shp) {
      status.value = '未找到同名 .shp 文件。请从文件列表点击 .shp 文件，或确保同名文件已上传到当前任务。';
      return;
    }
    if (!dbf) {
      status.value = '已找到 .shp，但缺少同名 .dbf，当前只能预览几何，无法显示属性表。';
    }

    const geometryBuffer = await fetchBuffer(shp, load);
    if (!load.isCurrent()) return;
    const parsedGeometries = parseShp(geometryBuffer);
    geometries.value = parsedGeometries;
    bounds.value = calculateBounds(parsedGeometries);
    viewBounds.value = bounds.value ? [...bounds.value] as [number, number, number, number] : null;

    if (dbf) {
      const attributeBuffer = await fetchBuffer(dbf, load);
      if (!load.isCurrent()) return;
      attributes.value = parseDbf(attributeBuffer);
    }
    if (prj) {
      const buffer = await fetchBuffer(prj, load);
      if (!load.isCurrent()) return;
      const text = new TextDecoder('utf-8', { fatal: false }).decode(buffer);
      projectionText.value = text.trim();
    }

    status.value = parsedGeometries.length ? '' : '未解析到可预览的 Shapefile 几何。';
  } catch (error) {
    if (!load.isCurrent()) return;
    console.error('Failed to render Shapefile:', error);
    status.value = 'Shapefile 预览失败。请确认文件未损坏，且 .shp/.dbf/.shx 属于同一组。';
  }
};

watch([() => props.file, selectedLayerKey], () => {
  if (!selectedLayerKey.value && layerChoices.value.length) selectedLayerKey.value = groupKey(props.file);
  void loadShapefile();
}, { immediate: true });
watch(relatedFiles, () => {
  if (!selectedLayerKey.value && layerChoices.value.length) selectedLayerKey.value = groupKey(props.file);
  void loadShapefile();
});
</script>
