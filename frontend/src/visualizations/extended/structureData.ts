export interface StructuredNode {
  path: string; node_type: string;
  attributes: { label: string; value?: string | boolean | null; children_count?: number };
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
// The Python reader budgets Unicode characters, not JavaScript UTF-16 code units.
const text = (value: unknown, limit = 512): value is string => typeof value === 'string' && value.length <= limit * 2 && Array.from(value).length <= limit;
const scalar = (value: unknown) => value === null || typeof value === 'boolean' || text(value) || (typeof value === 'number' && Number.isFinite(value));
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const types = new Set(['object', 'array', 'string', 'number', 'element', 'attribute', 'text', 'null', 'boolean']);
export function parentPath(path: string) { return path.slice(0, path.lastIndexOf('/')); }
/** Names and values remain escaped Vue text; paths are opaque integer positions, never filenames. */
export function parseStructuredTree(value: unknown): StructuredNode[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 256) throw new Error('结构树节点数量不符合有界预览协议。');
  const paths = new Set<string>();
  return value.map((node, index) => {
    if (!record(node) || !text(node.path, 100) || !/^\/0(?:\/(?:0|[1-9][0-9]{0,6})){0,8}$/.test(node.path)
      || paths.has(node.path) || (index === 0 ? node.path !== '/0' : !paths.has(parentPath(node.path)))
      || typeof node.node_type !== 'string' || !types.has(node.node_type) || !record(node.attributes)
      || !text(node.attributes.label) || ('value' in node.attributes && !(node.attributes.value === null || typeof node.attributes.value === 'boolean' || text(node.attributes.value)))
      || ('children_count' in node.attributes && !integer(node.attributes.children_count))) throw new Error('结构树响应包含无效节点或未批准的数据类型。');
    paths.add(node.path);
    return { path: node.path, node_type: node.node_type, attributes: {
      label: node.attributes.label,
      ...('value' in node.attributes ? { value: node.attributes.value as string | boolean | null } : {}),
      ...('children_count' in node.attributes ? { children_count: node.attributes.children_count as number } : {}),
    } };
  });
}
export function visibleTree(nodes: StructuredNode[], expanded: ReadonlySet<string>, search = '') {
  const query = search.trim().slice(0, 128).toLocaleLowerCase();
  if (query) {
    const visible = new Set<string>();
    for (const node of nodes) if (`${node.attributes.label} ${node.attributes.value ?? ''}`.toLocaleLowerCase().includes(query)) {
      let path = node.path;
      while (path) { visible.add(path); path = parentPath(path); }
    }
    return nodes.filter(node => visible.has(node.path));
  }
  return nodes.filter(node => {
    let parent = parentPath(node.path);
    while (parent) { if (!expanded.has(parent)) return false; parent = parentPath(parent); }
    return true;
  });
}
export interface ArchiveTable {
  columns: string[]; rows: Array<Array<string | number | boolean | null>>;
  row_offset: number; column_offset: number; total_rows: number; total_columns: number;
}
export function parseArchiveTable(value: unknown): ArchiveTable {
  if (!record(value) || !Array.isArray(value.columns) || value.columns.length < 1 || value.columns.length > 16
    || !value.columns.every(column => text(column, 128)) || !Array.isArray(value.rows) || value.rows.length > 200
    || !value.rows.every(row => Array.isArray(row) && row.length === (value.columns as unknown[]).length && row.every(scalar))
    || !integer(value.row_offset) || value.row_offset > 4095 || value.column_offset !== 0
    || !integer(value.total_rows) || value.total_rows < value.row_offset + value.rows.length
    || value.total_columns !== value.columns.length) throw new Error('压缩包目录响应不符合分页预算或单元格协议。');
  return value as unknown as ArchiveTable;
}
