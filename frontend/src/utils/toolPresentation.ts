import type { ToolContent } from '../types/message';
import type {
  ToolChartType,
  ToolPresentation,
  ToolPresentationColumn,
  ToolPresentationKind,
  ToolPresentationSeries,
} from '../types/toolPresentation';
import { isSensitiveToolDisplayKey, sanitizeToolDisplayText } from './toolDisplay.ts';

const KINDS = new Set<ToolPresentationKind>([
  'auto',
  'generic',
  'table',
  'chart',
  'map',
  'image',
  'artifact',
  'log',
]);
const CHART_TYPES = new Set<ToolChartType>(['line', 'bar', 'area', 'scatter']);
const SAFE_COLOR = /^(?:#[0-9a-f]{3,8}|[a-z]{1,20}|(?:rgb|hsl)a?\([0-9.,% ]+\))$/i;
const MAX_NODES = 4_000;
/** Maximum UTF-8 size of the complete compact-JSON `presentation.data`. */
export const MAX_TOOL_PRESENTATION_DATA_BYTES = 256_000;
const MAX_DEPTH = 7;
const MAX_ITEMS = 200;
const MAX_FIELDS = 80;
const MAX_STRING_LENGTH = 20_000;
const SIZE_LIMIT_MARKER = '[内容过多，已省略]';
const NESTING_LIMIT_MARKER = '[内容层级过深，已省略]';

type DisplayBudget = { nodes: number };
type BoundedValue = { value: unknown; bytes: number };

const utf8Encoder = new TextEncoder();

export interface ResolvedToolPresentation extends ToolPresentation {
  kind: Exclude<ToolPresentationKind, 'auto'>;
  data: unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  !!value && typeof value === 'object' && !Array.isArray(value)
);

const safeText = (value: unknown, limit: number): string | undefined => {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') return undefined;
  const text = sanitizeToolDisplayText(value).trim();
  return text ? text.slice(0, limit) : undefined;
};

const safeKey = (value: unknown): string | undefined => {
  const key = safeText(value, 120);
  return key && !isSensitiveToolDisplayKey(key) ? key : undefined;
};

const compactJsonBytes = (value: unknown): number => {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) return Number.POSITIVE_INFINITY;
  return utf8Encoder.encode(serialized).byteLength;
};

const fitJsonValue = (value: unknown, maxBytes: number): BoundedValue | undefined => {
  let safeValue = value;
  let bytes: number;
  try {
    bytes = compactJsonBytes(safeValue);
  } catch {
    safeValue = null;
    bytes = compactJsonBytes(safeValue);
  }
  if (bytes <= maxBytes) return { value: safeValue, bytes };
  const markerBytes = compactJsonBytes(SIZE_LIMIT_MARKER);
  return markerBytes <= maxBytes
    ? { value: SIZE_LIMIT_MARKER, bytes: markerBytes }
    : undefined;
};

const fitJsonString = (value: string, maxBytes: number): BoundedValue | undefined => {
  const fullBytes = compactJsonBytes(value);
  if (fullBytes <= maxBytes) return { value, bytes: fullBytes };
  if (compactJsonBytes('') > maxBytes) return undefined;

  // Array.from iterates Unicode code points, so truncation never creates a
  // broken surrogate pair while finding the longest prefix that fits.
  const codePoints = Array.from(value);
  let low = 0;
  let high = codePoints.length;
  while (low < high) {
    const midpoint = Math.floor((low + high + 1) / 2);
    if (compactJsonBytes(codePoints.slice(0, midpoint).join('')) <= maxBytes) {
      low = midpoint;
    } else {
      high = midpoint - 1;
    }
  }
  const truncated = codePoints.slice(0, low).join('');
  return { value: truncated, bytes: compactJsonBytes(truncated) };
};

const boundedDisplayValueWithSize = (
  value: unknown,
  depth: number,
  budget: DisplayBudget,
  maxBytes: number,
): BoundedValue | undefined => {
  if (budget.nodes <= 0) return fitJsonValue(SIZE_LIMIT_MARKER, maxBytes);
  budget.nodes -= 1;
  if (value === null || value === undefined || typeof value === 'boolean') {
    return fitJsonValue(value === undefined ? null : value, maxBytes);
  }
  if (typeof value === 'number') {
    return fitJsonValue(Number.isFinite(value) ? value : null, maxBytes);
  }
  if (typeof value === 'string') {
    const text = sanitizeToolDisplayText(value).slice(0, MAX_STRING_LENGTH);
    return fitJsonString(text, maxBytes);
  }
  if (depth >= MAX_DEPTH) return fitJsonValue(NESTING_LIMIT_MARKER, maxBytes);
  if (Array.isArray(value)) {
    const result: unknown[] = [];
    let bytes = 2; // opening and closing brackets
    if (bytes > maxBytes) return undefined;
    for (const item of value.slice(0, MAX_ITEMS)) {
      if (budget.nodes <= 0) break;
      const separatorBytes = result.length ? 1 : 0;
      const child = boundedDisplayValueWithSize(
        item,
        depth + 1,
        budget,
        maxBytes - bytes - separatorBytes,
      );
      if (!child) break;
      result.push(child.value);
      bytes += separatorBytes + child.bytes;
    }
    return { value: result, bytes };
  }
  if (isRecord(value)) {
    const result: Record<string, unknown> = {};
    let bytes = 2; // opening and closing braces
    if (bytes > maxBytes) return undefined;
    for (const [rawKey, item] of Object.entries(value).slice(0, MAX_FIELDS)) {
      if (budget.nodes <= 0) break;
      const key = safeKey(rawKey);
      if (!key || Object.prototype.hasOwnProperty.call(result, key)) continue;
      const separatorBytes = Object.keys(result).length ? 1 : 0;
      const entryOverhead = separatorBytes + compactJsonBytes(key) + 1; // comma + key + colon
      const childBudget = maxBytes - bytes - entryOverhead;
      if (childBudget < 0) break;
      const child = boundedDisplayValueWithSize(
        item,
        depth + 1,
        budget,
        childBudget,
      );
      if (!child) break;
      result[key] = child.value;
      bytes += entryOverhead + child.bytes;
    }
    return { value: result, bytes };
  }
  const text = safeText(value, MAX_STRING_LENGTH);
  return text === undefined
    ? fitJsonValue(null, maxBytes)
    : fitJsonString(text, maxBytes);
};

const boundedDisplayValue = (value: unknown): unknown => {
  const fitted = boundedDisplayValueWithSize(
    value,
    0,
    { nodes: MAX_NODES },
    MAX_TOOL_PRESENTATION_DATA_BYTES,
  );
  if (!fitted) return SIZE_LIMIT_MARKER;
  // This final guard is deliberately based on the actual browser serializer.
  // It remains fail-closed if future value types change the accounting rules.
  return compactJsonBytes(fitted.value) <= MAX_TOOL_PRESENTATION_DATA_BYTES
    ? fitted.value
    : SIZE_LIMIT_MARKER;
};

const normalizeColumns = (value: unknown): ToolPresentationColumn[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const columns: ToolPresentationColumn[] = [];
  for (const raw of value.slice(0, 32)) {
    const source = typeof raw === 'string' ? { key: raw } : raw;
    if (!isRecord(source)) continue;
    const key = safeKey(source.key);
    if (!key) continue;
    const align = source.align;
    columns.push({
      key,
      label: safeText(source.label, 160),
      align: align === 'left' || align === 'center' || align === 'right' ? align : undefined,
    });
  }
  return columns.length ? columns : undefined;
};

const normalizeSeries = (value: unknown): ToolPresentationSeries[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const series: ToolPresentationSeries[] = [];
  for (const raw of value.slice(0, 16)) {
    const source = typeof raw === 'string' ? { key: raw } : raw;
    if (!isRecord(source)) continue;
    const key = safeKey(source.key);
    if (!key) continue;
    const color = safeText(source.color, 32);
    series.push({
      key,
      label: safeText(source.label, 160),
      color: color && SAFE_COLOR.test(color) ? color : undefined,
    });
  }
  return series.length ? series : undefined;
};

export const safeToolResourceUrl = (value: unknown, origin?: string): string | undefined => {
  if (typeof value !== 'string' || value.length > 2_048) return undefined;
  const raw = value.trim();
  if (!raw.startsWith('/') || raw.startsWith('//')) return undefined;
  try {
    const base = origin || 'http://dataseek.local';
    const parsed = new URL(raw, base);
    if (parsed.origin !== new URL(base).origin) return undefined;
    if (parsed.username || parsed.password) return undefined;
    if (parsed.hash) return undefined;
    if (!/^\/api\/v1\/files\/[A-Za-z0-9._-]{1,255}$/.test(parsed.pathname)) return undefined;
    const query = [...parsed.searchParams.entries()];
    if (query.length) {
      if (query.length !== 2) return undefined;
      const signatures = parsed.searchParams.getAll('signature');
      const expirations = parsed.searchParams.getAll('expires');
      if (signatures.length !== 1 || expirations.length !== 1) return undefined;
      if (!/^[0-9a-f]{64}$/i.test(signatures[0])) return undefined;
      if (!/^\d{1,12}$/.test(expirations[0])) return undefined;
    }
    return raw;
  } catch {
    return undefined;
  }
};

const resultPayload = (tool: ToolContent): unknown => {
  if (tool.presentation?.data !== undefined && tool.presentation.data !== null) {
    return tool.presentation.data;
  }
  let value: unknown = tool.content;
  if (isRecord(value) && 'result' in value) value = value.result;
  if (isRecord(value) && 'data' in value && (
    'success' in value || 'message' in value || Object.keys(value).length === 1
  )) {
    value = value.data;
  }
  return value;
};

/** Validate a descriptor again at the browser boundary and attach safe result data. */
export const resolveToolPresentation = (tool: ToolContent): ResolvedToolPresentation | null => {
  const raw = tool.presentation;
  if (!raw || !KINDS.has(raw.kind) || raw.kind === 'auto') return null;
  const chartType = raw.chart_type;
  const level = raw.level;
  return {
    kind: raw.kind,
    title: safeText(raw.title, 160),
    description: safeText(raw.description, 1_000),
    data: boundedDisplayValue(resultPayload(tool)),
    columns: normalizeColumns(raw.columns),
    series: normalizeSeries(raw.series),
    x_key: safeKey(raw.x_key),
    chart_type: chartType && CHART_TYPES.has(chartType) ? chartType : undefined,
    url: safeToolResourceUrl(raw.url),
    mime_type: safeText(raw.mime_type, 160),
    filename: safeText(raw.filename, 255),
    level: level === 'debug' || level === 'info' || level === 'warning' || level === 'error'
      ? level
      : undefined,
  };
};

export const presentationRows = (data: unknown): Record<string, unknown>[] => {
  const candidates: unknown[] = [data];
  if (isRecord(data)) {
    candidates.push(data.rows, data.preview, data.records, data.items, data.points, data.data);
    for (const nested of [data.summary, data.result]) {
      if (!isRecord(nested)) continue;
      candidates.push(
        nested.rows,
        nested.preview,
        nested.records,
        nested.items,
        nested.points,
        nested.data,
      );
    }
  }
  const value = candidates.find(Array.isArray);
  return Array.isArray(value) ? value.filter(isRecord).slice(0, 100) : [];
};

export const presentationColumns = (
  presentation: ResolvedToolPresentation,
  rows: Record<string, unknown>[],
): ToolPresentationColumn[] => {
  if (presentation.columns?.length) return presentation.columns.slice(0, 16);
  const keys: string[] = [];
  for (const row of rows.slice(0, 20)) {
    for (const key of Object.keys(row)) {
      if (!keys.includes(key) && !isSensitiveToolDisplayKey(key)) keys.push(key);
      if (keys.length >= 12) return keys.map((item) => ({ key: item, label: item }));
    }
  }
  return keys.map((item) => ({ key: item, label: item }));
};

export const presentationText = (value: unknown, maxLength = 30_000): string => {
  if (typeof value === 'string') return sanitizeToolDisplayText(value).slice(0, maxLength);
  try {
    return JSON.stringify(value, null, 2).slice(0, maxLength);
  } catch {
    return sanitizeToolDisplayText(value).slice(0, maxLength);
  }
};

export interface GeoPresentationSummary {
  featureCount: number;
  geometryTypes: string[];
  bounds?: [number, number, number, number];
  points: Array<[number, number]>;
}

export const summarizeGeoPresentation = (data: unknown): GeoPresentationSummary => {
  const points: Array<[number, number]> = [];
  const geometryTypes = new Set<string>();
  let featureCount = 0;

  const visitCoordinates = (value: unknown, depth = 0) => {
    if (!Array.isArray(value) || depth > 8 || points.length >= 1_000) return;
    if (
      value.length >= 2
      && typeof value[0] === 'number'
      && Number.isFinite(value[0])
      && typeof value[1] === 'number'
      && Number.isFinite(value[1])
    ) {
      points.push([value[0], value[1]]);
      return;
    }
    for (const item of value.slice(0, 1_000)) visitCoordinates(item, depth + 1);
  };

  const visitGeometry = (value: unknown) => {
    if (!isRecord(value)) return;
    if (typeof value.type === 'string') geometryTypes.add(value.type.slice(0, 80));
    visitCoordinates(value.coordinates);
    if (Array.isArray(value.geometries)) value.geometries.slice(0, 200).forEach(visitGeometry);
  };

  if (isRecord(data) && data.type === 'FeatureCollection' && Array.isArray(data.features)) {
    featureCount = data.features.length;
    data.features.slice(0, 1_000).forEach((feature) => {
      if (isRecord(feature)) visitGeometry(feature.geometry);
    });
  } else if (isRecord(data) && data.type === 'Feature') {
    featureCount = 1;
    visitGeometry(data.geometry);
  } else {
    visitGeometry(data);
  }

  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  const bounds = points.length
    ? [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] as [number, number, number, number]
    : undefined;
  return { featureCount, geometryTypes: [...geometryTypes], bounds, points };
};
