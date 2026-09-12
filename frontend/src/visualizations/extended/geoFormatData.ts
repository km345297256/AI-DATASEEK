export const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const number = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const oneOf = (v: unknown, values: string[]) => typeof v === 'string' && values.includes(v);
const fail = (): never => { throw new Error('地理预览响应未通过结构或预算检查。'); };
const exact = (v: unknown, fields: string[]): v is Record<string, unknown> => object(v) && Object.keys(v).length === fields.length && Object.keys(v).every(k => fields.includes(k));
const extent = (v: unknown): v is number[] => Array.isArray(v) && v.length === 4 && v.every(number) && v[0] < v[2] && v[1] < v[3] && Number.isFinite(v[2] - v[0]) && Number.isFinite(v[3] - v[1]);
export interface GeoData { metadata: Record<string, unknown>; array?: { shape: number[]; values: (number | null)[] }; geojson?: { type: 'FeatureCollection'; features: Record<string, unknown>[] }; }
export function parseGeoData(raw: unknown): GeoData {
  if (!object(raw) || raw.kind !== 'map' || !object(raw.metadata) || ('array' in raw) === ('geojson' in raw)) return fail();
  const meta = raw.metadata;
  if ('array' in raw) {
    if (!exact(meta, ['format', 'crs', 'extent', 'source_extent', 'source_shape', 'nodata', 'registration', 'row_order', 'sampling', 'crs_source']) || !oneOf(meta.format, ['ESRI ASCII', 'Surfer DSAA']) || !oneOf(meta.crs, ['unknown', 'EPSG:4326', 'EPSG:3857']) || !extent(meta.extent) || !extent(meta.source_extent) || !Array.isArray(meta.source_shape) || meta.source_shape.length !== 2 || !meta.source_shape.every(v => Number.isSafeInteger(v) && v > 0) || meta.source_shape[0] * meta.source_shape[1] > 1048576 || !number(meta.nodata) || !oneOf(meta.registration, ['cell-center', 'cell-corner', 'node']) || meta.row_order !== 'north-to-south' || meta.sampling !== 'nearest cell-centre sample; no aggregation' || !oneOf(meta.crs_source, ['user', 'unspecified'])) return fail();
    if (meta.crs === 'EPSG:4326' && !(meta.extent[0]! >= -180 && meta.extent[2]! <= 180 && meta.extent[1]! >= -90 && meta.extent[3]! <= 90)) return fail();
    if (meta.crs === 'EPSG:3857' && meta.extent.some(v => Math.abs(v) > 20037508.34279)) return fail();
    const a = raw.array;
    if (!exact(a, ['shape', 'dimensions', 'values']) || !Array.isArray(a.shape) || a.shape.length !== 2 || a.shape.some((v, i) => v !== Math.min(Number((meta.source_shape as number[])[i]), 128)) || !Array.isArray(a.dimensions) || a.dimensions.join(',') !== 'y,x' || !Array.isArray(a.values) || a.values.length !== Number(a.shape[0]) * Number(a.shape[1]) || a.values.length > 16384 || !a.values.every(v => v === null || number(v))) return fail();
    return { metadata: meta, array: { shape: a.shape as number[], values: a.values as (number | null)[] } };
  }
  if (!exact(meta, ['format', 'crs', 'crs_source', 'feature_count', 'coordinate_count']) || meta.format !== 'KML 2.2' || meta.crs !== 'EPSG:4326' || meta.crs_source !== 'format') return fail();
  const g = raw.geojson;
  if (!exact(g, ['type', 'features']) || g.type !== 'FeatureCollection' || !Array.isArray(g.features) || !g.features.length || g.features.length > 512 || meta.feature_count !== g.features.length) return fail();
  let points = 0;
  for (const f of g.features) {
    if (!exact(f, ['type', 'properties', 'geometry']) || f.type !== 'Feature' || !exact(f.properties, ['name']) || typeof f.properties.name !== 'string' || Array.from(f.properties.name).length > 256 || !exact(f.geometry, ['type', 'coordinates']) || !oneOf(f.geometry.type, ['Point', 'LineString', 'Polygon'])) return fail();
    const type = f.geometry.type, coords = f.geometry.coordinates;
    const lines = type === 'Point' ? [[coords]] : type === 'LineString' ? [coords] : coords;
    if (!Array.isArray(lines) || !lines.length) return fail();
    for (const line of lines) {
      if (!Array.isArray(line) || line.length < (type === 'Point' ? 1 : type === 'LineString' ? 2 : 4)) return fail();
      for (const p of line) {
        if (!Array.isArray(p) || p.length !== 2 || !p.every(number) || p[0] < -180 || p[0] > 180 || p[1] < -90 || p[1] > 90 || ++points > 16384) return fail();
      }
      if (type === 'Polygon' && JSON.stringify(line[0]) !== JSON.stringify(line[line.length - 1])) return fail();
    }
  }
  if (meta.coordinate_count !== points) return fail();
  return { metadata: meta, geojson: g as unknown as GeoData['geojson'] };
}
export function rasterPixels(values: (number | null)[]): { pixels: Uint8ClampedArray; min: number | null; max: number | null; invalid: number } {
  let min = Infinity, max = -Infinity, invalid = 0;
  for (const v of values) if (v === null) invalid++; else { min = Math.min(min, v); max = Math.max(max, v); }
  const pixels = new Uint8ClampedArray(values.length * 4), scale = Math.max(Math.abs(min), Math.abs(max), 1), span = max / scale - min / scale;
  values.forEach((v, i) => { const c = v !== null && span ? Math.max(0, Math.min(255, Math.round((v / scale - min / scale) / span * 255))) : 0; pixels.set([c, Math.round(190 * (1 - c / 255)), 255 - c, v === null ? 0 : 255], i * 4); });
  return { pixels, min: Number.isFinite(min) ? min : null, max: Number.isFinite(max) ? max : null, invalid };
}
