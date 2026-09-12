/** Inert, reader-specific geometry; never a dynamic renderer or solver. */
import type { VisualizationResult } from '../runtime';
export const GRO_WARNING = 'GRO 坐标为 nm、速度为 nm/ps、声明时间为 ps；不推断元素或化学键，不展开周期边界，不拟合或跨帧插值。';
export const MESH_WARNING = '仅绘制已存储网格线框与节点值或单元顶点均值位置上的值；不提取外表面、不插值、不变形、不拟合或执行求解器。坐标与场单位未声明。';
export type GeometryMode = 'gro-trajectory' | 'simulation-mesh';
type Obj = Record<string, any>;
export type Vec3 = [number, number, number];
export interface Frame { id: number; atoms: number; time_ps: number | null; velocities: boolean; box: number[] }
export interface Field { id: string; name: string; association: 'point' | 'cell'; components: number; tuples: number }
export interface Atom { residue_number: number; residue_name: string; atom_name: string; atom_number: number }
export interface MeshCell { type: number; points: number[] }
export interface GeometryData {
  mode: GeometryMode; metadata: Obj; frames: Frame[]; fields: Field[];
  trajectory?: { positions: Vec3[]; atoms: Atom[]; velocities: Vec3[] | null };
  mesh?: { points: Vec3[]; cells: MeshCell[]; field: { id: string; association: 'point' | 'cell'; component: number; values: (number | null)[] } | null };
}
const sizes: Record<number, number> = {3:2,5:3,9:4,10:4,12:8,13:6,14:5};
export const CELL_EDGES: Record<number, number[][]> = {
  3:[[0,1]],5:[[0,1],[1,2],[2,0]],9:[[0,1],[1,2],[2,3],[3,0]],10:[[0,1],[1,2],[2,0],[0,3],[1,3],[2,3]],
  12:[[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]],
  13:[[0,1],[1,2],[2,0],[3,4],[4,5],[5,3],[0,3],[1,4],[2,5]],14:[[0,1],[1,2],[2,3],[3,0],[0,4],[1,4],[2,4],[3,4]],
};
function need(ok: unknown): asserts ok { if (!ok) throw new Error('几何结果与受限格式、目录、所选帧或标量分量不一致。'); }
const own = (v: object,k: PropertyKey) => Object.prototype.hasOwnProperty.call(v,k);
function keys(v: unknown, names: string): v is Obj { const list = names ? names.split(' ') : []; return !!v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === list.length && list.every(k => own(v, k)); }
const int = (v: unknown, lo: number, hi: number): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= lo && v <= hi;
const finite = (v: unknown, bound = 1e12): v is number => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= bound;
const triple = (v: unknown, bound: number): v is Vec3 => Array.isArray(v) && v.length === 3 && v.every(x => finite(x, bound));
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const safeName = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 128 && !/[<>\x00-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
export function geometrySelection(mode: GeometryMode, catalog: GeometryData, frame: unknown, field: unknown, component: unknown): Record<string, unknown> {
  if (mode === 'gro-trajectory') { need(int(frame, 0, catalog.frames.length - 1)); return {frame}; }
  need(field === null || typeof field === 'string');
  const selected = catalog.fields.find(f => f.id === field);
  need(field === null ? component === 0 : selected && int(component, 0, selected.components - 1));
  return {field, component};
}
export function geometryCatalogIdentity(data: GeometryData) { return JSON.stringify([data.mode, data.metadata, data.frames, data.fields]); }
export function parseGeometryData(result: VisualizationResult, mode: GeometryMode, kind: 'tree' | 'geometry', options: Record<string, unknown>, size?: number, filename?: string): GeometryData {
  need(result.contract_version === 2 && result.kind === kind && /^[a-f0-9]{64}$/.test(result.version) && /^[a-f0-9]{64}$/.test(result.revision) && result.sampled === false);
  need(same(result.warnings, [mode === 'gro-trajectory' ? GRO_WARNING : MESH_WARNING]));
  const p = result.payload, m = result.metadata, field = kind === 'tree' ? 'tree' : mode === 'gro-trajectory' ? 'trajectory' : 'mesh';
  need(keys(p, `media_type view_kind choices selected ${field}`) && p.media_type === 'application/json' && p.view_kind === kind);
  const selection=p.selected;need(keys(selection,Object.keys(options).join(' ')) && Object.keys(options).every(k=>selection[k]===options[k]));
  need(m && m.input_mode === 'whole' && int(m.source_bytes, 1, 16777216) && (size === undefined || m.source_bytes === size) && (filename === undefined || filename.split('.').pop()?.toLowerCase() === m.format));
  const out: GeometryData = {mode, metadata: m, frames: [], fields: []};
  if (mode === 'gro-trajectory') {
    need(keys(m, 'format input_mode source_bytes frame_count total_atoms coordinate_unit velocity_unit time_unit topology_consistency limits') && m.format === 'gro' && m.coordinate_unit === 'nm' && m.velocity_unit === 'nm/ps' && m.time_unit === 'ps' && m.topology_consistency === 'ordered GRO identifiers; bonds and elements unspecified');
    need(same(m.limits, {max_frames:64,max_atoms:8192,max_total_atoms:131072}) && int(m.frame_count,1,64) && int(m.total_atoms,1,131072));
    need(keys(p.choices, 'frames') && Array.isArray(p.choices.frames) && p.choices.frames.length === m.frame_count);
    out.frames = p.choices.frames.map((f: unknown, i: number) => {
      need(keys(f, 'id atoms time_ps velocities box') && f.id === i && int(f.atoms,1,8192) && (f.time_ps === null || finite(f.time_ps)) && typeof f.velocities === 'boolean');
      need(Array.isArray(f.box) && f.box.length === 9 && f.box.every((x: unknown) => finite(x,1e6)) && [3,4,6].every(j => f.box[j] === 0) && (f.box.every((x: number) => x === 0) || [0,1,2].every(j => f.box[j] > 0)));
      return f as Frame;
    });
    need(out.frames.every(f => f.atoms === out.frames[0]!.atoms) && out.frames.reduce((sum,f) => sum+f.atoms,0) === m.total_atoms);
    if (kind === 'tree') need(keys(options, '') && same(p.tree,out.frames.map(f => ({path:`/frames/${f.id}`,node_type:'array',attributes:{label:`Frame ${f.id+1}`}}))));
    else {
      need(keys(options, 'frame') && int(options.frame,0,out.frames.length-1)); const f = out.frames[options.frame]!, g = p.trajectory;
      need(keys(g,'positions atoms velocities') && Array.isArray(g.positions) && g.positions.length === f.atoms && g.positions.every((v: unknown) => triple(v,1e6)));
      need(Array.isArray(g.atoms) && g.atoms.length === f.atoms && g.atoms.every((a: unknown) => keys(a,'residue_number residue_name atom_name atom_number') && int(a.residue_number,0,99999) && int(a.atom_number,0,99999) && ['residue_name','atom_name'].every(k => typeof a[k] === 'string' && /^[A-Za-z0-9_+*'-]{1,5}$/.test(a[k]))));
      need(f.velocities ? Array.isArray(g.velocities) && g.velocities.length === f.atoms && g.velocities.every((v: unknown) => triple(v,1e6)) : g.velocities === null);
      out.trajectory = g as GeometryData['trajectory'];
    }
  } else {
    need(keys(m,'format input_mode source_bytes point_count cell_count cell_types field_values coordinate_unit field_unit limits value_semantics') && m.format === 'vtu' && m.coordinate_unit === 'unspecified' && m.field_unit === 'unspecified' && m.value_semantics === 'stored component; no interpolation or deformation');
    need(same(m.limits,{max_points:4096,max_cells:2048,max_fields:32,max_components:9,max_field_values:131072}) && int(m.point_count,1,4096) && int(m.cell_count,1,2048) && int(m.field_values,0,131072));
    need(Array.isArray(m.cell_types) && m.cell_types.length > 0 && m.cell_types.every((t: unknown) => int(t,3,14) && own(sizes,t)) && same(m.cell_types,[...new Set(m.cell_types)].sort((a,b) => Number(a)-Number(b))));
    need(keys(p.choices,'fields') && Array.isArray(p.choices.fields) && p.choices.fields.length <= 32); const counts = {point:0,cell:0};
    out.fields = p.choices.fields.map((f: unknown) => {
      need(keys(f,'id name association components tuples') && (f.association === 'point' || f.association === 'cell') && safeName(f.name) && int(f.components,1,9) && f.tuples === m[`${f.association}_count`]);
      const a = f.association as 'point' | 'cell'; need(f.id === `${a[0]}-${counts[a]++}`); return f as Field;
    });
    need(out.fields.reduce((sum,f) => sum+f.components*f.tuples,0) === m.field_values);
    if (kind === 'tree') need(keys(options,'') && same(p.tree,[{path:'/mesh',node_type:'array',attributes:{label:'VTU mesh'}}]));
    else {
      need(keys(options,'field component')); geometrySelection(mode,out,0,options.field,options.component); const g = p.mesh;
      need(keys(g,'points cells field') && Array.isArray(g.points) && g.points.length === m.point_count && g.points.every((v: unknown) => triple(v,1e12)));
      const pointCount=m.point_count as number;
      need(Array.isArray(g.cells) && g.cells.length === m.cell_count && g.cells.every((c: unknown) => keys(c,'type points') && int(c.type,3,14) && own(sizes,c.type) && Array.isArray(c.points) && c.points.length === sizes[c.type] && c.points.every((i: unknown) => int(i,0,pointCount-1)) && new Set(c.points).size === c.points.length));
      need(same(m.cell_types,[...new Set(g.cells.map((c: MeshCell) => c.type))].sort((a,b) => Number(a)-Number(b))));
      if (options.field === null) need(g.field === null);
      else { const f = out.fields.find(f => f.id === options.field)!; need(keys(g.field,'id association component values') && g.field.id === f.id && g.field.association === f.association && g.field.component === options.component && Array.isArray(g.field.values) && g.field.values.length === f.tuples && g.field.values.every((v: unknown) => v === null || finite(v))); }
      out.mesh = g as GeometryData['mesh'];
    }
  }
  need(new TextEncoder().encode(JSON.stringify(result)).length <= 2097152); return out;
}
export interface SceneGeometry { points: Vec3[]; edges: number[][]; markers: Vec3[]; values: (number | null)[] | null; association: 'atoms' | 'point' | 'cell' | 'none' }
export function sceneGeometry(data: GeometryData): SceneGeometry {
  if (data.trajectory) return {points:data.trajectory.positions,edges:[],markers:data.trajectory.positions,values:null,association:'atoms'};
  need(data.mesh); const m = data.mesh, edges: number[][] = [], seen = new Set<string>();
  for (const c of m.cells) for (const [a,b] of CELL_EDGES[c.type]!) { const pair = [c.points[a!]!,c.points[b!]!].sort((a,b)=>a-b), key = pair.join(','); if (!seen.has(key)) { seen.add(key); edges.push(pair); } }
  const field = m.field;
  // Cell markers are vertex arithmetic means, not volume centroids or interpolation.
  const markers = field?.association === 'cell' ? m.cells.map(c => [0,1,2].map(d => c.points.reduce((sum,i) => sum + m.points[i]![d]! / c.points.length,0)) as Vec3) : m.points;
  return {points:m.points,edges,markers,values:field?.values ?? null,association:field?.association ?? 'none'};
}
export function localScene(scene: SceneGeometry) {
  need(scene.points.length > 0 && scene.points.length <= 8192 && scene.markers.length <= 8192 && scene.edges.length <= 24576);
  const origin = scene.points[0]!, deltas = scene.points.map(p => p.map((v,i)=>v-origin[i]!) as Vec3);
  const scale = Math.max(0,...deltas.flat().map(Math.abs)) || 1;
  return {origin,scale,positions:Float32Array.from(deltas.flat(),v=>v/scale),markers:Float32Array.from(scene.markers.flatMap(p=>p.map((v,i)=>(v-origin[i]!)/scale)))};
}
