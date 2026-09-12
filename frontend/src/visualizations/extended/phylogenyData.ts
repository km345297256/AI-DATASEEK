/** Inert Newick display contract and bounded, read-only rectangular layout. */
import type { VisualizationResult } from '../runtime';
export const PHYLOGENY_WARNINGS = [
  '仅显示单棵 Newick 树；显示起点不表示已确认的生物学根，不进行重定根或系统发育推断。',
  '内部节点标签（包括数字）仍是标签，不自动解释为支持度；枝长单位未知，根的入枝长不参与坐标。',
  '缺失枝长不补零；只有全部非根枝长明确且并非全零时才提供枝长模式，数值坐标用于展示。',
];
const formats = new Set(['nwk', 'newick', 'tree', 'tre']);
const numberPattern = /^\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?$/;
const resource = /[<>{}]|\b[a-z][a-z0-9+.-]{1,20}:\/\/|\b(?:data|javascript|file):|(?:^|\s)[/\\]|[A-Za-z]:[/\\]/i;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).length === names.length && names.every(k => Object.prototype.hasOwnProperty.call(v, k));
const int = (v: unknown, min: number, max: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max;
function require(value: unknown): asserts value { if (!value) throw new Error('系统树响应未通过结构、语义或资源校验。'); }
export interface PhylogenyNode { id: string; parent: string | null; label: string | null; length: string | null }
export interface PhylogenyData { nodes: PhylogenyNode[]; children: number[][]; depths: number[]; distances: (number | null)[]; descendants: number[]; leaves: number; maxDepth: number; missingLengths: number; branchLengthMode: boolean; sourceBytes: number; format: string }
export function branchNumber(value: unknown): number | null {
  if (value === null) return null;
  require(typeof value === 'string' && value.length >= 1 && value.length <= 32 && numberPattern.test(value));
  // Check exact decimal magnitude before IEEE conversion, including values
  // just above 1e9 / just below 1e-12 that Number would round to the boundary.
  const [mantissa, exponent] = value.replace(/^\+/, '').split(/[eE]/), decimalPlaces = mantissa!.split('.')[1]?.length ?? 0;
  const digits = mantissa!.replace('.', '').replace(/^0+/, ''), order = digits.length - 1 + Number(exponent ?? 0) - decimalPlaces;
  require(!digits || order >= -12 && (order < 9 || order === 9 && digits.replace(/0+$/, '') === '1'));
  const n = Number(value); require(Number.isFinite(n) && n >= 0 && n <= 1e9 && (!digits || n >= 1e-12));
  return n;
}
export function parsePhylogenyData(result: VisualizationResult, fileSize?: number, filename?: string): PhylogenyData {
  require(result.contract_version === 2 && result.kind === 'tree' && /^[0-9a-f]{64}$/.test(result.version) && /^[0-9a-f]{64}$/.test(result.revision));
  require(result.sampled === false && JSON.stringify(result.warnings) === JSON.stringify(PHYLOGENY_WARNINGS));
  const p = result.payload, m = result.metadata;
  require(record(p) && keys(p, ['media_type', 'view_kind', 'phylogeny']) && p.media_type === 'application/json' && p.view_kind === 'tree');
  const tree = p.phylogeny;
  require(record(tree) && keys(tree, ['root', 'rootedness', 'nodes']) && tree.root === 'n0' && tree.rootedness === 'unspecified' && Array.isArray(tree.nodes) && tree.nodes.length >= 2 && tree.nodes.length <= 1000);
  const children: number[][] = tree.nodes.map(() => []), depths: number[] = [], distances: (number | null)[] = [], stack: number[] = [];
  let missing = 0, positive = false;
  const nodes: PhylogenyNode[] = tree.nodes.map((n, i) => {
    require(record(n) && keys(n, ['id', 'parent', 'label', 'length']) && n.id === `n${i}`);
    require(n.label === null || typeof n.label === 'string' && [...n.label].length >= 1 && [...n.label].length <= 256 && !/\p{C}/u.test(n.label) && !resource.test(n.label));
    const length = branchNumber(n.length);
    if (i === 0) { require(n.parent === null); depths.push(0); distances.push(0); }
    else {
      require(typeof n.parent === 'string' && /^n(?:0|[1-9][0-9]{0,2})$/.test(n.parent));
      const parent = Number(n.parent.slice(1)); require(parent < i && stack.includes(parent));
      while (stack[stack.length - 1] !== parent) stack.pop();
      depths.push(depths[parent]! + 1); children[parent]!.push(i);
      const previous = distances[parent]; distances.push(length === null || previous === null ? null : previous! + length);
      if (length === null) missing++; else if (length > 0) positive = true;
    }
    require(depths[i]! <= 64); stack.push(i);
    return n as unknown as PhylogenyNode;
  });
  require(children[0]!.length > 0);
  const leaves = children.filter(c => c.length === 0).length, maxDepth = Math.max(...depths), branchLengthMode = missing === 0 && positive;
  require(record(m) && keys(m, ['format', 'dialect', 'input_mode', 'source_bytes', 'node_count', 'leaf_count', 'max_depth', 'missing_lengths', 'root_length_present', 'branch_length_mode']));
  require(typeof m.format === 'string' && formats.has(m.format) && m.dialect === 'newick-single-v1' && m.input_mode === 'whole' && int(m.source_bytes, 1, 4194304));
  require(m.node_count === nodes.length && m.leaf_count === leaves && m.max_depth === maxDepth && m.missing_lengths === missing && m.root_length_present === (nodes[0]!.length !== null) && m.branch_length_mode === branchLengthMode);
  require(fileSize === undefined || int(fileSize, 1, 4194304) && m.source_bytes === fileSize);
  require(filename === undefined || filename.split('.').pop()?.toLowerCase() === m.format);
  require(new TextEncoder().encode(JSON.stringify(result)).length <= 1048576);
  const descendants = nodes.map(() => 0);
  for (let i = nodes.length - 1; i > 0; i--) descendants[Number(nodes[i]!.parent!.slice(1))]! += 1 + descendants[i]!;
  return { nodes, children, depths, distances, descendants, leaves, maxDepth, missingLengths: missing, branchLengthMode, sourceBytes: m.source_bytes, format: m.format };
}
export interface TreePoint { index: number; x: number; y: number; terminal: boolean; collapsed: boolean }
export interface TreeLayout { points: TreePoint[]; paths: { id: string; d: string }[]; height: number; width: number; scale: number }
export function layoutPhylogeny(data: PhylogenyData, mode: 'topology' | 'length', collapsed: ReadonlySet<number>): TreeLayout {
  require(mode === 'topology' || mode === 'length' && data.branchLengthMode);
  const visible: number[] = [], allowed = new Set<number>(), y = new Map<number, number>();
  for (let i = 0; i < data.nodes.length; i++) {
    const parent = data.nodes[i]!.parent;
    if (parent === null || allowed.has(Number(parent.slice(1)))) { visible.push(i); if (!collapsed.has(i)) allowed.add(i); }
  }
  let row = 0;
  for (const i of visible) if (!data.children[i]!.length || collapsed.has(i)) y.set(i, 32 + row++ * 28);
  for (let k = visible.length - 1; k >= 0; k--) {
    const i = visible[k]!;
    if (!y.has(i)) { const children = data.children[i]!; y.set(i, (y.get(children[0]!)! + y.get(children[children.length - 1]!)!) / 2); }
  }
  const scale = mode === 'length' ? Math.max(...data.distances.map(v => v ?? 0)) : data.maxDepth;
  require(scale > 0 && Number.isFinite(scale));
  const points = visible.map(index => ({ index, x: 32 + (mode === 'length' ? data.distances[index]! : data.depths[index]!) / scale * 600, y: y.get(index)!, terminal: !data.children[index]!.length || collapsed.has(index), collapsed: collapsed.has(index) && !!data.children[index]!.length }));
  const positions = new Map(points.map(p => [p.index, p]));
  const paths = points.filter(p => p.index > 0).map(p => { const parent = positions.get(Number(data.nodes[p.index]!.parent!.slice(1)))!; return { id: data.nodes[p.index]!.id, d: `M${parent.x},${parent.y}V${p.y}H${p.x}` }; });
  return { points, paths, width: 1100, height: Math.max(160, 64 + Math.max(row - 1, 0) * 28), scale };
}
