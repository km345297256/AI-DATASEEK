/** Inert columnar schema. Big integers/decimals must never pass through Number. */
export const COLUMNAR_SEMANTICS = 'source order; integers and decimals are exact strings; nonfinite floats are null; no SQL or aggregation';
export const COLUMNAR_LIMITS = { max_rows: 200, max_columns: 32, max_schema_columns: 128, max_metadata_bytes: 1048576, max_block_bytes: 4194304, max_decoded_bytes: 16777216, max_scan_rows: 262144 };
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).length === names.length && names.every(k => Object.prototype.hasOwnProperty.call(v, k));
const integer = (v: unknown, max = Number.MAX_SAFE_INTEGER, min = 0): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const label = (v: unknown): v is string => typeof v === 'string' && [...v].length >= 1 && [...v].length <= 128 && !/[<>\x00-\x1f\x7f]|\/Users\/|\/home\/|\/private\/|\/tmp\/|https?:\/\/|file:|[A-Za-z]:\\/i.test(v);
const types = new Set(['bool', 'int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64', 'float32', 'float64', 'string', 'decimal128']);
function fail(): never { throw new Error('列式窗口的结构、选择或读取预算无效。'); }
export interface ColumnarColumn { id: number; label: string; type: string; nullable: boolean; precision: number | null; scale: number | null }
export interface ColumnarSelection { columns: number[]; row_offset: number; row_limit: number }
export type ColumnarCell = string | number | boolean | null;
export interface ColumnarData {
  columns: ColumnarColumn[]; selected: ColumnarSelection | null; rows: ColumnarCell[][] | null;
  totalRows: number; totalGroups: number; sourceBytes: number; readBytes: number; reads: number;
  scanRows: number; decodedBytes: number; groupsRead: number; format: string;
}
export function validateColumnarSelection(input: unknown, catalog?: ColumnarData): ColumnarSelection {
  if (!record(input) || !keys(input, ['columns', 'row_offset', 'row_limit']) || !Array.isArray(input.columns)
    || input.columns.length < 1 || input.columns.length > 32 || input.columns.some(c => !integer(c, catalog ? catalog.columns.length - 1 : 127))
    || new Set(input.columns).size !== input.columns.length || !integer(input.row_offset) || !integer(input.row_limit, 200, 1)
    || catalog && (input.row_offset > catalog.totalRows || input.row_offset === catalog.totalRows && catalog.totalRows !== 0)) fail();
  return { columns: [...input.columns] as number[], row_offset: input.row_offset, row_limit: input.row_limit };
}
export function columnarSelectionsEqual(a: ColumnarSelection, b: ColumnarSelection): boolean {
  return a.row_offset === b.row_offset && a.row_limit === b.row_limit && JSON.stringify(a.columns) === JSON.stringify(b.columns);
}
function validCell(value: unknown, c: ColumnarColumn): value is ColumnarCell {
  if (value === null) return c.nullable || c.type.startsWith('float');
  if (c.type === 'bool') return typeof value === 'boolean';
  if (/^u?int/.test(c.type)) {
    if (typeof value !== 'string' || value.length > 21 || !/^(?:0|-[1-9][0-9]*|[1-9][0-9]*)$/.test(value)) return false;
    const bits = BigInt(c.type.match(/\d+$/)![0]), n = BigInt(value);
    return c.type.startsWith('u') ? n >= BigInt(0) && n < BigInt(2) ** bits : n >= -(BigInt(2) ** (bits - BigInt(1))) && n < BigInt(2) ** (bits - BigInt(1));
  }
  if (c.type.startsWith('float')) return typeof value === 'number' && Number.isFinite(value);
  if (c.type === 'string') return typeof value === 'string' && new TextEncoder().encode(value).length <= 2048;
  if (c.type === 'decimal128') {
    if (typeof value !== 'string' || value.length > 80 || !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value)) return false;
    const parts = value.replace(/^-/, '').split('.'), digits = parts.join('').replace(/^0+/, '') || '0';
    return (parts[1]?.length ?? 0) === c.scale && digits.length <= c.precision!;
  }
  return false;
}
export function parseColumnarWindow(kind: 'tree' | 'table', payload: Record<string, unknown>, metadata: Record<string, unknown>, expected?: ColumnarSelection): ColumnarData {
  if (!keys(payload, ['media_type', 'view_kind', 'choices', 'selected', kind]) || payload.media_type !== 'application/json' || payload.view_kind !== kind
    || !record(payload.choices) || !keys(payload.choices, ['columns']) || !Array.isArray(payload.choices.columns) || payload.choices.columns.length < 1 || payload.choices.columns.length > 128) fail();
  const columns: ColumnarColumn[] = payload.choices.columns.map((c, i) => {
    if (!record(c) || !keys(c, ['id', 'label', 'type', 'nullable', 'precision', 'scale']) || !integer(c.id, 127) || c.id !== i
      || !label(c.label) || typeof c.type !== 'string' || !types.has(c.type) || typeof c.nullable !== 'boolean') fail();
    if (c.type === 'decimal128') { if (!integer(c.precision, 38, 1) || !integer(c.scale, c.precision)) fail(); }
    else if (c.precision !== null || c.scale !== null) fail();
    return c as unknown as ColumnarColumn;
  });
  const m = metadata;
  if (!keys(m, ['format', 'container', 'input_mode', 'value_semantics', 'source_bytes', 'read_bytes', 'read_requests', 'total_rows', 'total_columns', 'total_groups', 'metadata_bytes', 'groups_read', 'scan_rows', 'decoded_bytes', 'blocks_checked', 'nonfinite_values', 'limits'])
    || typeof m.format !== 'string' || !['parquet', 'parq', 'arrow', 'feather'].includes(m.format)
    || m.container !== (['parquet', 'parq'].includes(m.format) ? 'Parquet' : 'Arrow IPC file') || m.input_mode !== 'window' || m.value_semantics !== COLUMNAR_SEMANTICS
    || !integer(m.source_bytes, 8 * 1024 ** 3, 16) || !integer(m.read_bytes, 8388608, 1) || !integer(m.read_requests, 128, 1) || m.read_bytes < m.read_requests
    || !integer(m.total_rows) || !integer(m.total_columns, 128, 1) || m.total_columns !== columns.length || !integer(m.total_groups, m.container === 'Parquet' ? 1024 : 64)
    || !integer(m.metadata_bytes, Math.min(1048576, m.source_bytes), 1) || !integer(m.groups_read, Math.min(32, m.total_groups))
    || !integer(m.scan_rows, Math.min(262144, m.total_rows)) || !integer(m.decoded_bytes, 16777216) || !integer(m.blocks_checked, 12288) || !integer(m.nonfinite_values, 6400)
    || !record(m.limits) || !keys(m.limits, Object.keys(COLUMNAR_LIMITS)) || Object.entries(COLUMNAR_LIMITS).some(([k, v]) => (m.limits as Record<string, unknown>)[k] !== v)) fail();
  let selected: ColumnarSelection | null = null, rows: ColumnarCell[][] | null = null;
  const result: ColumnarData = { columns, selected, rows, totalRows: m.total_rows, totalGroups: m.total_groups, sourceBytes: m.source_bytes, readBytes: m.read_bytes, reads: m.read_requests, scanRows: m.scan_rows, decodedBytes: m.decoded_bytes, groupsRead: m.groups_read, format: m.format };
  if (kind === 'tree') {
    if (!record(payload.selected) || Object.keys(payload.selected).length || !Array.isArray(payload.tree) || payload.tree.length !== columns.length
      || [m.groups_read, m.scan_rows, m.decoded_bytes, m.blocks_checked, m.nonfinite_values].some(v => v !== 0)) fail();
    for (const [i, node] of payload.tree.entries()) {
      if (!record(node) || !keys(node, ['path', 'node_type', 'attributes']) || node.path !== `/column-${i}` || node.node_type !== 'column'
        || !record(node.attributes) || !keys(node.attributes, ['label', 'type']) || node.attributes.label !== columns[i]!.label || node.attributes.type !== columns[i]!.type) fail();
    }
  } else {
    selected = validateColumnarSelection(payload.selected, result);
    if (expected && !columnarSelectionsEqual(selected, validateColumnarSelection(expected, result))) fail();
    const t = payload.table, count = Math.min(selected.row_limit, m.total_rows - selected.row_offset);
    if (!record(t) || !keys(t, ['columns', 'rows', 'row_offset', 'total_rows', 'total_columns'])
      || JSON.stringify(t.columns) !== JSON.stringify(selected.columns.map(c => columns[c]!.label)) || t.row_offset !== selected.row_offset
      || t.total_rows !== m.total_rows || t.total_columns !== columns.length || !Array.isArray(t.rows) || t.rows.length !== count
      || m.scan_rows < count || count > 0 && !m.groups_read) fail();
    let nullFloats = 0;
    rows = t.rows.map(row => {
      if (!Array.isArray(row) || row.length !== selected!.columns.length || row.some((v, i) => !validCell(v, columns[selected!.columns[i]!]!))) fail();
      row.forEach((v, i) => { if (v === null && columns[selected!.columns[i]!]!.type.startsWith('float')) nullFloats++; });
      return row as ColumnarCell[];
    });
    if (m.nonfinite_values > nullFloats) fail();
  }
  if (new TextEncoder().encode(JSON.stringify({ payload, metadata })).length > 2097152) fail();
  return { ...result, selected, rows };
}
