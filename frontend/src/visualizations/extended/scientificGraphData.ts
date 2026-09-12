import type { ElementDefinition } from 'cytoscape';

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const exact = (v: unknown, keys: string[]): v is Record<string, unknown> => record(v) && Object.keys(v).length === keys.length && Object.keys(v).every(k => keys.includes(k));
const integer = (v: unknown, min: number, max: number): v is number => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const fail = (): never => { throw new Error('科学网络图响应未通过类型、拓扑或预算检查 / Invalid bounded graph response.'); };
const formats: Record<string, string[]> = { json: ['dataseek-graph-json-v1'], graphml: ['graphml-flat-static'], gexf: ['gexf-1.2-static', 'gexf-1.3-static'] };
export const GRAPH_WARNINGS = ['仅支持有界静态简单图；不支持自环、平行边、层级、动态图、外部资源或可执行样式。', '布局仅用于展示；不推断生物学关系，不把边权重解释为距离或因果强度。'];
export interface GraphNode { id: string; key: string; label: string; group: string | null }
export interface GraphEdge { id: string; key: string | null; source: string; target: string; label: string; weight: number | null }
export interface ScientificGraphData { directed: boolean; nodes: GraphNode[]; edges: GraphEdge[]; version: string; format: string; sourceBytes: number }
function text(v: unknown, id = false, empty = false): v is string {
  return typeof v === 'string' && [...v].length >= (empty ? 0 : 1) && [...v].length <= (id ? 128 : 256)
    && !/[\p{C}<>]|\b[a-z][a-z0-9+.-]{1,20}:\/\/|\b(?:data|javascript|file):|(?:^|\s)[/\\]|[A-Za-z]:[/\\]/iu.test(v)
    && (!id || /^[A-Za-z0-9_.:-]{1,128}$/.test(v));
}
export function graphFormatForFile(filename: string): string[] {
  const extension = filename.toLowerCase().split('.').pop();
  return extension && ['json', 'graphml', 'gexf'].includes(extension) ? [extension] : [];
}
export function parseScientificGraphData(raw: unknown, sourceBytes?: number, filename?: string): ScientificGraphData {
  if (!record(raw) || raw.contract_version !== 2 || raw.type !== 'scientific-graph' || raw.reader !== undefined && raw.reader !== 'scientific-graph'
    || raw.kind !== 'graph' || raw.view_kind !== undefined && raw.view_kind !== 'graph' || raw.media_type !== 'application/json'
    || raw.sampled !== false || JSON.stringify(raw.warnings) !== JSON.stringify(GRAPH_WARNINGS)
    || typeof raw.version !== 'string' || !/^[a-f0-9]{64}$/.test(raw.version)
    || raw.revision !== undefined && (typeof raw.revision !== 'string' || !/^[a-f0-9]{64}$/.test(raw.revision))) return fail();
  const allowed = ['contract_version', 'type', 'reader', 'kind', 'view_kind', 'media_type', 'graph', 'metadata', 'warnings', 'sampled', 'version', 'revision'];
  if (Object.keys(raw).some(k => !allowed.includes(k))) return fail();
  // Public envelope adds a small, fixed set of version fields to the 1 MiB private payload.
  if (new TextEncoder().encode(JSON.stringify(raw)).length > 1024 ** 2 + 1024) return fail();
  const meta = raw.metadata, graph = raw.graph;
  if (!exact(meta, ['format', 'dialect', 'input_mode', 'source_bytes', 'node_count', 'edge_count', 'simple'])
    || typeof meta.format !== 'string' || !Object.prototype.hasOwnProperty.call(formats, meta.format) || typeof meta.dialect !== 'string' || !formats[meta.format]!.includes(meta.dialect)
    || meta.input_mode !== 'whole' || meta.simple !== true || !integer(meta.source_bytes, 1, 4 * 1024 ** 2)
    || sourceBytes !== undefined && meta.source_bytes !== sourceBytes || filename !== undefined && !graphFormatForFile(filename).includes(meta.format)
    || !exact(graph, ['directed', 'nodes', 'edges']) || typeof graph.directed !== 'boolean'
    || !Array.isArray(graph.nodes) || graph.nodes.length < 1 || graph.nodes.length > 1000 || !Array.isArray(graph.edges) || graph.edges.length > 3000
    || !integer(meta.node_count, 1, 1000) || !integer(meta.edge_count, 0, 3000) || meta.node_count !== graph.nodes.length || meta.edge_count !== graph.edges.length) return fail();
  const keys = new Set<string>(), ids = new Set<string>();
  for (const [i, node] of graph.nodes.entries()) {
    if (!exact(node, ['id', 'key', 'label', 'group']) || node.id !== `n${i}` || !text(node.key, true) || !text(node.label) || node.group !== null && !text(node.group) || keys.has(node.key)) return fail();
    keys.add(node.key); ids.add(node.id as string);
  }
  const edgeKeys = new Set<string>(), pairs = new Set<string>();
  for (const [i, edge] of graph.edges.entries()) {
    if (!exact(edge, ['id', 'key', 'source', 'target', 'label', 'weight']) || edge.id !== `e${i}`
      || edge.key !== null && (!text(edge.key, true) || edgeKeys.has(edge.key)) || typeof edge.source !== 'string' || typeof edge.target !== 'string'
      || !ids.has(edge.source) || !ids.has(edge.target) || edge.source === edge.target || !text(edge.label, false, true)
      || edge.weight !== null && (typeof edge.weight !== 'number' || !Number.isFinite(edge.weight) || Math.abs(edge.weight) > 1e12)) return fail();
    if (edge.key !== null) edgeKeys.add(edge.key as string);
    const pair = JSON.stringify(graph.directed ? [edge.source, edge.target] : [edge.source, edge.target].sort());
    if (pairs.has(pair)) return fail(); pairs.add(pair);
  }
  return { directed: graph.directed, nodes: graph.nodes as GraphNode[], edges: graph.edges as GraphEdge[], format: meta.format, sourceBytes: meta.source_bytes, version: raw.version };
}
/** Never forward user fields such as classes/style/position/parent to Cytoscape. */
export function graphElements(data: ScientificGraphData): ElementDefinition[] {
  return [...data.nodes.map(n => ({ group: 'nodes' as const, data: { id: n.id, label: n.label } })),
    ...data.edges.map(e => ({ group: 'edges' as const, data: { id: e.id, source: e.source, target: e.target, label: e.label } }))];
}
