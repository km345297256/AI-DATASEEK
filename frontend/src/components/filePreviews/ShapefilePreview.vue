<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-[var(--text-secondary)]">Shapefile 预览</span>
        <span v-if="summary">{{ summary }}</span>
      </div>
      <span v-if="projectionSummary" class="max-w-[360px] truncate" :title="projectionText">{{ projectionSummary }}</span>
    </div>

    <div v-if="status" class="flex min-h-0 flex-1 items-center justify-center p-4">
      <div class="max-w-[520px] rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        {{ status }}
      </div>
    </div>

    <div v-else class="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_220px] gap-3 p-4">
      <div class="min-h-0 overflow-hidden rounded-xl border border-[var(--border-main)] bg-white">
        <svg
          v-if="viewBox && geometries.length"
          class="h-full w-full"
          :viewBox="viewBox"
          preserveAspectRatio="xMidYMid meet">
          <g>
            <template v-for="geometry in geometries" :key="`${geometry.type}-${JSON.stringify(geometry.coordinates)}`">
              <circle
                v-if="geometry.type === 'Point'"
                :cx="geometry.coordinates[0]"
                :cy="flipY(geometry.coordinates[1])"
                :r="pointRadius"
                fill="#2563eb"
                fill-opacity="0.85" />
              <template v-else-if="geometry.type === 'MultiPoint'">
                <circle
                  v-for="(point, pointIndex) in geometry.coordinates"
                  :key="pointIndex"
                  :cx="point[0]"
                  :cy="flipY(point[1])"
                  :r="pointRadius"
                  fill="#2563eb"
                  fill-opacity="0.85" />
              </template>
              <path
                v-else-if="geometry.type === 'LineString'"
                :d="linePath(geometry.coordinates)"
                fill="none"
                stroke="#0f766e"
                :stroke-width="strokeWidth"
                stroke-linejoin="round"
                stroke-linecap="round" />
              <template v-else-if="geometry.type === 'MultiLineString'">
                <path
                  v-for="(line, lineIndex) in geometry.coordinates"
                  :key="lineIndex"
                  :d="linePath(line)"
                  fill="none"
                  stroke="#0f766e"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round"
                  stroke-linecap="round" />
              </template>
              <path
                v-else-if="geometry.type === 'Polygon'"
                :d="polygonPath(geometry.coordinates)"
                fill="#16a34a"
                fill-opacity="0.28"
                stroke="#15803d"
                :stroke-width="strokeWidth"
                stroke-linejoin="round" />
              <template v-else-if="geometry.type === 'MultiPolygon'">
                <path
                  v-for="(polygon, polygonIndex) in geometry.coordinates"
                  :key="polygonIndex"
                  :d="polygonPath(polygon)"
                  fill="#16a34a"
                  fill-opacity="0.28"
                  stroke="#15803d"
                  :stroke-width="strokeWidth"
                  stroke-linejoin="round" />
              </template>
            </template>
          </g>
        </svg>
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
              <tr v-for="(row, rowIndex) in previewRows" :key="rowIndex">
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
const geometries = ref<Geometry[]>([]);
const attributes = ref<Array<Record<string, string | number | boolean | null>>>([]);
const projectionText = ref('');
const bounds = ref<[number, number, number, number] | null>(null);
let loadVersion = 0;

const getExtension = (filename: string) => filename.split('.').pop()?.toLowerCase() || '';
const stripExtension = (filename: string) => filename.replace(/\.[^/.]+$/, '');
const basename = (filename: string) => filename.split('/').pop() || filename;
const groupKey = (filename: string) => stripExtension(basename(filename)).toLowerCase();

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
  if (!bounds.value) return '';
  const [minX, minY, maxX, maxY] = bounds.value;
  const width = Math.max(maxX - minX, 1);
  const height = Math.max(maxY - minY, 1);
  const pad = Math.max(width, height) * 0.04;
  return `${minX - pad} ${-maxY - pad} ${width + pad * 2} ${height + pad * 2}`;
});

const pointRadius = computed(() => {
  if (!bounds.value) return 1;
  const [minX, minY, maxX, maxY] = bounds.value;
  return Math.max(maxX - minX, maxY - minY, 1) * 0.004;
});

const strokeWidth = computed(() => pointRadius.value * 0.6);

const flipY = (y: number) => -y;
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

const fetchBuffer = async (file: FileInfo) => {
  const url = await getFileDownloadUrl(file);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.arrayBuffer();
};

const loadShapefile = async () => {
  const currentVersion = ++loadVersion;
  status.value = '正在加载 Shapefile...';
  geometries.value = [];
  attributes.value = [];
  projectionText.value = '';
  bounds.value = null;

  try {
    const currentKey = groupKey(props.file.filename);
    const candidates = relatedFiles.value.length ? relatedFiles.value : [props.file];
    const group = candidates.filter((file) => groupKey(file.filename) === currentKey);
    const byExtension = new Map(group.map((file) => [getExtension(file.filename), file]));
    const shp = byExtension.get('shp') || (getExtension(props.file.filename) === 'shp' ? props.file : null);
    const dbf = byExtension.get('dbf');
    const prj = byExtension.get('prj');

    if (!shp) {
      status.value = '未找到同名 .shp 文件。请从文件列表点击 .shp 文件，或确保同名文件已上传到当前任务。';
      return;
    }
    if (!dbf) {
      status.value = '已找到 .shp，但缺少同名 .dbf，当前只能预览几何，无法显示属性表。';
    }

    const parsedGeometries = parseShp(await fetchBuffer(shp));
    if (currentVersion !== loadVersion) return;
    geometries.value = parsedGeometries;
    bounds.value = calculateBounds(parsedGeometries);

    if (dbf) {
      attributes.value = parseDbf(await fetchBuffer(dbf));
      if (currentVersion !== loadVersion) return;
    }
    if (prj) {
      const text = new TextDecoder('utf-8', { fatal: false }).decode(await fetchBuffer(prj));
      if (currentVersion !== loadVersion) return;
      projectionText.value = text.trim();
    }

    status.value = parsedGeometries.length ? '' : '未解析到可预览的 Shapefile 几何。';
  } catch (error) {
    console.error('Failed to render Shapefile:', error);
    status.value = 'Shapefile 预览失败。请确认文件未损坏，且 .shp/.dbf/.shx 属于同一组。';
  }
};

watch(() => props.file, loadShapefile, { immediate: true });
</script>
