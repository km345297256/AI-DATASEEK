/** Only bounded numeric values cross from readers into third-party renderers. */
export interface PreviewTable { columns: string[]; rows: Array<Array<string | number | boolean | null>>; }
export interface PreviewArray { shape: number[]; values: Array<number | null>; }
export function plainLabel(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value.replace(/[<>\u0000-\u001f]/g, '').slice(0, 160) : fallback;
}
export function parseTable(value: unknown): PreviewTable {
  const table = value as PreviewTable;
  if (!table || !Array.isArray(table.columns) || !table.columns.length || table.columns.length > 128 || !Array.isArray(table.rows) || table.rows.length > 4096) throw new Error('表格预览超出约定预算。');
  if (!table.columns.every((column) => typeof column === 'string') || !table.rows.every((row) => Array.isArray(row) && row.length === table.columns.length && row.every((cell) => cell === null || typeof cell === 'boolean' || (typeof cell === 'number' && Number.isFinite(cell)) || (typeof cell === 'string' && cell.length <= 1024)))) throw new Error('表格包含不支持的值。');
  return { columns: table.columns.map((column) => plainLabel(column)), rows: table.rows };
}
export function parseArray(value: unknown): PreviewArray {
  const array = value as PreviewArray;
  if (!array || !Array.isArray(array.shape) || array.shape.length < 1 || array.shape.length > 2 || !array.shape.every((size) => Number.isSafeInteger(size) && size > 0) || !Array.isArray(array.values) || array.values.length > 65536 || array.shape.reduce((a, b) => a * b, 1) !== array.values.length || !array.values.every((value) => value === null || (typeof value === 'number' && Number.isFinite(value)))) throw new Error('数组预览需为有界的一维或二维数值切片。');
  return array;
}
export function numericCell(value: unknown): number | null {
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string' || !value.trim()) return null;
  const number = Number(value); return Number.isFinite(number) ? number : null;
}
export function numericColumns(table: PreviewTable): number[] {
  return table.columns.map((_, index) => index).filter((index) => table.rows.some((row) => numericCell(row[index]) !== null));
}
export function numericPairs(result: Record<string, unknown>): { x: number[]; y: number[] } {
  let x: Array<number | null>, y: Array<number | null>;
  if (result.array) {
    const array = parseArray(result.array);
    if (array.shape.length !== 2 || array.shape[1] !== 2) throw new Error('坐标数据需为 N × 2 数值。');
    x = array.values.filter((_, index) => index % 2 === 0); y = array.values.filter((_, index) => index % 2 === 1);
  } else {
    const table = parseTable(result.table); x = table.rows.map((row) => numericCell(row[0])); y = table.rows.map((row) => numericCell(row[1]));
  }
  if (!x.length || x.some((value) => value === null) || y.some((value) => value === null)) throw new Error('坐标数据含缺失值。');
  return { x: x as number[], y: y as number[] };
}
export function safeText(bytes: ArrayBuffer, maxBytes: number): string {
  if (bytes.byteLength > maxBytes) throw new Error('文件超过此视图的安全解析预算。');
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}
export function assertMolecularText(text: string, format: 'pdb' | 'mmcif'): void {
  if (format === 'pdb') {
    const atoms = text.match(/^(?:ATOM  |HETATM)/gm)?.length ?? 0;
    if (!atoms || atoms > 100000) throw new Error('PDB 预览需包含 1–100000 个原子。');
  } else if (!/_atom_site\./i.test(text) || (text.match(/\n/g)?.length ?? 0) > 300000) throw new Error('此视图仅支持有界的 mmCIF 原子结构。');
}
export function assertVtkInput(bytes: ArrayBuffer, extension: string): void {
  if (bytes.byteLength > 8 * 1024 * 1024) throw new Error('网格预览限制为 8 MiB。');
  if (extension === 'stl' && bytes.byteLength >= 84) {
    const triangles = new DataView(bytes).getUint32(80, true);
    if (84 + triangles * 50 === bytes.byteLength) {
      if (triangles > 100000) throw new Error('STL 超过 100000 个三角形。');
      return;
    }
  }
  const text = safeText(bytes, 8 * 1024 * 1024);
  if (extension === 'vtp' || extension === 'vti') {
    if (/<!DOCTYPE|<!ENTITY|compressor\s*=|<AppendedData|<Array\s|format\s*=\s*["'](?:binary|appended)/i.test(text)) throw new Error('首期 VTK XML 仅允许未压缩 ASCII 数值数据，不加载外链或附加二进制。');
    let allocationCount = 1;
    for (const match of text.matchAll(/NumberOf(?:Points|Cells|Polys|Verts|Lines|Strips|Tuples)\s*=\s*["']([^"']+)/g)) {
      const count = Number(match[1]); if (!/^\d+$/.test(match[1]!) || count > 300000) throw new Error('网格元素数超出预算。'); allocationCount = Math.max(allocationCount, count);
    }
    for (const match of text.matchAll(/(?:Whole)?Extent\s*=\s*["']([^"']+)/g)) {
      const extent = match[1]!.trim().split(/\s+/).map(Number);
      if (extent.length !== 6 || extent.some((n) => !Number.isSafeInteger(n)) || [0, 2, 4].some((i) => extent[i + 1]! < extent[i]!) || [0, 2, 4].reduce((n, i) => n * (extent[i + 1]! - extent[i]! + 1), 1) > 2 * 1024 * 1024) throw new Error('VTK 体素维度超出预算。');
      allocationCount = Math.max(allocationCount, [0, 2, 4].reduce((n, i) => n * (extent[i + 1]! - extent[i]! + 1), 1));
    }
    let estimatedBytes = 0, arrays = 0;
    for (const match of text.matchAll(/<DataArray\b([^>]*)>/gi)) {
      const attribute = /NumberOfComponents\s*=\s*["']([^"']+)/i.exec(match[1]!);
      const components = attribute ? Number(attribute[1]) : 1;
      if (!Number.isSafeInteger(components) || components < 1 || components > 9) throw new Error('VTK 数值分量数超出预算。');
      estimatedBytes += allocationCount * components * 8;
      if (++arrays > 32 || estimatedBytes > 32 * 1024 * 1024) throw new Error('VTK 声明的解码数组超出 32 MiB 预算。');
    }
  } else if (extension === 'obj') {
    if (/^(?:mtllib|call|csh)\s/im.test(text)) throw new Error('此网格视图不加载材质、脚本或外部文件。');
    if ((text.match(/^v\s/gm)?.length ?? 0) > 300000 || (text.match(/^f\s/gm)?.length ?? 0) > 100000) throw new Error('OBJ 网格元素数超出预算。');
  } else if (extension !== 'stl' || (text.match(/\bfacet normal\b/g)?.length ?? 0) > 100000) throw new Error('不支持此网格格式或网格超过预算。');
}
