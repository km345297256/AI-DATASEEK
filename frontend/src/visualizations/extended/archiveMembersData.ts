const refPattern = /^member-[a-f0-9]{64}$/;
export interface ArchiveResource { name: string; kind: 'file' | 'directory'; bytes: number; memberId: string | null; }
export interface MemberPage { mode: 'directory' | 'text'; offset: number; total: number; resources: ArchiveResource[]; lines: Array<[number, string]>; name: string; }
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const integer = (v: unknown, maximum: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0 && v <= maximum;
const label = (v: unknown, maximum: number): v is string => typeof v === 'string' && [...v].length <= maximum && !/[\x00-\x1f]/.test(v);
const fields = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length && Object.keys(value).every(key => keys.includes(key));
export function parseMemberPage(payload: Record<string, unknown>, metadata: Record<string, unknown>, memberId: string | null, offset: number): MemberPage {
  const table = payload.table;
  const mode = memberId ? 'text' : 'directory';
  const common = ['format', 'source_bytes', 'member_limit_bytes', 'writes_source', 'recursive', 'mode'];
  if (!record(table) || metadata.mode !== mode || typeof metadata.format !== 'string' || !['zip', 'tar'].includes(metadata.format)
    || !fields(table, ['columns', 'rows', 'row_offset', 'column_offset', 'total_rows', 'total_columns'])
    || !fields(metadata, [...common, ...(mode === 'text' ? ['member_id', 'member_name', 'member_bytes', 'checksum_verified', 'encoding', 'line_char_limit', 'member_text_only'] : ['contents_verified'])])
    || !integer(metadata.source_bytes, 64 * 1024 ** 2) || metadata.source_bytes === 0 || !integer(offset, 262144)
    || metadata.writes_source !== false || metadata.recursive !== false || metadata.member_limit_bytes !== 262144
    || !integer(table.total_rows, mode === 'text' ? 262145 : 4096) || table.row_offset !== offset || table.column_offset !== 0
    || offset >= Math.max(1, table.total_rows) || !Array.isArray(table.rows) || table.rows.length !== Math.min(200, table.total_rows - offset)) throw new Error('归档成员页超出批准协议。');
  const page: MemberPage = { mode, offset, total: table.total_rows, resources: [], lines: [], name: '' };
  if (mode === 'directory') {
    if (JSON.stringify(table.columns) !== JSON.stringify(['成员', '类型', '声明大小（字节）', '可预览', '预览标识']) || table.total_columns !== 5 || metadata.contents_verified !== false) throw new Error('归档资源目录无效。');
    const ids = new Set();
    for (const row of table.rows) {
      if (!Array.isArray(row) || row.length !== 5 || !label(row[0], 512) || !['file', 'directory'].includes(row[1]) || !integer(row[2], 536870912) || typeof row[3] !== 'boolean'
        || (row[3] ? row[1] !== 'file' || row[2] > 262144 || typeof row[4] !== 'string' || !refPattern.test(row[4]) || ids.has(row[4]) : row[4] !== null)) throw new Error('归档成员标识无效。');
      if (row[4]) ids.add(row[4]);
      page.resources.push({ name: row[0], kind: row[1], bytes: row[2], memberId: row[4] });
    }
  } else {
    if (!memberId || !refPattern.test(memberId) || metadata.member_id !== memberId || !label(metadata.member_name, 512) || metadata.member_text_only !== true || metadata.encoding !== 'utf-8'
      || !integer(metadata.member_bytes, 262144) || metadata.line_char_limit !== 1024 || metadata.checksum_verified !== (metadata.format === 'zip')
      || JSON.stringify(table.columns) !== JSON.stringify(['行号', '文本']) || table.total_columns !== 2) throw new Error('成员内容不属于所选资源。');
    page.name = metadata.member_name;
    for (const [i, row] of table.rows.entries()) {
      if (!Array.isArray(row) || row.length !== 2 || row[0] !== offset + i + 1 || !label(row[1], 1024)) throw new Error('成员文本页无效。');
      page.lines.push([row[0], row[1]]);
    }
  }
  return page;
}
