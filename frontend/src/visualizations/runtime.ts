import { BASE_URL } from '../api/client';
import { getFileDownloadUrl, type FileInfo, type FilePreviewPage, type MolecularPreviewPreparation } from '../api/file';
import type { VisualizationPlugin } from './contract';
import { readBoundedBinary } from './boundedBinary';

export type VisualizationOperation = 'bytes' | 'page' | 'preview' | 'prepare';
export interface VisualizationResult {
  contract_version: 2;
  plugin_id: string;
  version: string;
  revision: string;
  kind: 'page' | 'series' | 'raster' | 'table' | 'array' | 'tree' | 'media' | 'report' | 'molecule' | 'resources';
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  warnings: string[];
  sampled: boolean;
}
// One load owns implicit pins; closing/reopening a view never inherits stale state.
// Page and slice controls explicitly carry their version across separate loads.
const versions = new WeakMap<AbortSignal, Map<string, string>>();
const versionKey = (file: FileInfo, plugin: VisualizationPlugin) => `${file.file_id}:${plugin.id}:${plugin.version}`;
function pinVersion(file: FileInfo, plugin: VisualizationPlugin, version: string, signal: AbortSignal) {
  const entries = versions.get(signal) ?? new Map<string, string>();
  entries.set(versionKey(file, plugin), version); versions.set(signal, entries);
}
const VERSION = /^[a-f0-9]{64}$/;
const kinds = new Set(['page', 'series', 'raster', 'table', 'array', 'tree', 'media', 'report', 'molecule', 'resources']);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
function requireOperation(plugin: VisualizationPlugin, operation: VisualizationOperation | 'job') {
  if (!plugin.enabled || plugin.contract_version !== 2 || !plugin.capabilities.operations.includes(operation)) throw new Error('可视化插件未启用或不支持此操作。');
}
function sharedPage() { return typeof window !== 'undefined' && window.location.pathname.startsWith('/share/'); }
function failure(status: number): Error {
  return Object.assign(new Error(({ 403: '该插件已停用或没有文件访问权限。', 404: '文件或插件不存在。', 409: '文件或插件版本变化，请重新打开预览。', 422: '此文件或参数无法安全预览，请核对格式及读取选项。' } as Record<number, string>)[status] ?? '隔离预览暂不可用，请检查服务及插件依赖。'), { status });
}
async function fetchVisualization(file: FileInfo, plugin: VisualizationPlugin, operation: VisualizationOperation, options: Record<string, unknown>, signal: AbortSignal) {
  requireOperation(plugin, operation);
  // Shared page/prepare retain the same server-side owner authority as before;
  // the location never grants access or bypasses the backend file gate.
  if (sharedPage() && !plugin.capabilities.shared) throw new Error('共享页面不开放此读取操作。');
  const { kind, version, ...readerOptions } = options;
  const pinnedVersion = Object.prototype.hasOwnProperty.call(options, 'version') ? version : versions.get(signal)?.get(versionKey(file, plugin));
  const response = await fetch(`${BASE_URL}/files/${encodeURIComponent(file.file_id)}/visualization`, {
    method: 'POST', credentials: 'same-origin', signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plugin_id: plugin.id, operation, version: readerOptions.resource_id ? undefined : pinnedVersion, kind, options: readerOptions }),
  });
  if (!response.ok) { await response.body?.cancel(); throw failure(response.status); }
  return response;
}
export function parseVisualizationResult(value: unknown, plugin: VisualizationPlugin): VisualizationResult {
  const fields = ['contract_version', 'plugin_id', 'version', 'revision', 'kind', 'payload', 'metadata', 'warnings', 'sampled'];
  if (!record(value) || Object.keys(value).length !== fields.length || Object.keys(value).some(key => !fields.includes(key))
    || value.contract_version !== 2 || value.plugin_id !== plugin.id || typeof value.version !== 'string' || !VERSION.test(value.version)
    || typeof value.revision !== 'string' || !VERSION.test(value.revision) || !kinds.has(String(value.kind)) || !record(value.payload) || !record(value.metadata)
    || !Array.isArray(value.warnings) || !value.warnings.every(item => typeof item === 'string') || typeof value.sampled !== 'boolean') {
    throw new Error('预览响应不符合统一插件协议。');
  }
  return value as unknown as VisualizationResult;
}
export async function readVisualizationResult(response: Response, plugin: VisualizationPlugin, signal: AbortSignal): Promise<VisualizationResult> {
  if (!response.ok) { await response.body?.cancel(); throw failure(response.status); }
  const bytes = await readBoundedBinary(response, plugin.limits.max_output_bytes + 4096, signal);
  const envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  if (envelope.code !== 0) throw new Error('预览读取失败。');
  return parseVisualizationResult(envelope.data, plugin);
}
export async function requestVisualization(file: FileInfo, plugin: VisualizationPlugin, operation: Exclude<VisualizationOperation, 'bytes'>, options: Record<string, unknown>, signal: AbortSignal) {
  const result = await readVisualizationResult(await fetchVisualization(file, plugin, operation, options, signal), plugin, signal);
  pinVersion(file, plugin, result.version, signal);
  return result;
}
/** Shared files retain the existing signed-resource authority, only for opted-in readers. */
async function sharedBytes(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal) {
  if (!plugin.capabilities.shared) throw new Error('此插件不支持共享页面。');
  const { getVisualizationCatalog } = await import('../api/visualization');
  const check = async () => {
    const catalog = await getVisualizationCatalog(signal);
    const current = catalog.plugins.find(item => item.id === plugin.id);
    if (!current?.enabled || !current.capabilities.shared || current.version !== plugin.version) throw failure(403);
  };
  await check();
  const url = await getFileDownloadUrl(file);
  if (signal.aborted) throw new DOMException('Preview cancelled', 'AbortError');
  const response = await fetch(url, { signal });
  if (!response.ok) { await response.body?.cancel(); throw failure(response.status); }
  const bytes = await readBoundedBinary(response, plugin.limits.max_input_bytes, signal);
  await check();
  return bytes;
}
export async function loadPluginBytes(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal, resource?: FileInfo): Promise<ArrayBuffer> {
  requireOperation(plugin, 'bytes');
  const target = resource ?? file;
  const limit = plugin.limits.max_input_bytes;
  if (target.size != null && (!Number.isFinite(target.size) || target.size < 0 || target.size > limit)) throw new Error('文件超过交互式预览大小上限。');
  if (sharedPage()) return sharedBytes(target, plugin, signal);
  const response = await fetchVisualization(file, plugin, 'bytes', resource ? { resource_id: resource.file_id } : {}, signal);
  const version = response.headers.get('X-Preview-Version');
  const revision = response.headers.get('X-Visualization-Revision');
  if (!version || !VERSION.test(version) || response.headers.get('X-Visualization-Plugin') !== plugin.id || !revision || !VERSION.test(revision)) {
    await response.body?.cancel(); throw new Error('文件流缺少统一插件协议标识。');
  }
  const bytes = await readBoundedBinary(response, limit, signal);
  pinVersion(target, plugin, version, signal);
  return bytes;
}
export async function loadPluginText(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal): Promise<string> {
  return new TextDecoder('utf-8', { fatal: false }).decode(await loadPluginBytes(file, plugin, signal));
}
export async function getPluginPage(file: FileInfo, plugin: VisualizationPlugin, options: Record<string, unknown>, signal: AbortSignal): Promise<FilePreviewPage> {
  const result = await requestVisualization(file, plugin, 'page', options, signal);
  const p = result.payload;
  if (result.kind !== 'page' || typeof p.text !== 'string' || !Array.isArray(p.headers) || p.headers.length > 50 || !p.headers.every(cell => typeof cell === 'string')
    || !Array.isArray(p.rows) || p.rows.length > 100 || !p.rows.every(row => Array.isArray(row) && row.length <= 50 && row.every(cell => typeof cell === 'string'))
    || !Number.isSafeInteger(p.offset) || Number(p.offset) < 0 || !(p.next_offset === null || Number.isSafeInteger(p.next_offset) && Number(p.next_offset) > Number(p.offset))
    || !Number.isSafeInteger(p.total_bytes) || Number(p.total_bytes) < 0 || !Number.isSafeInteger(p.bytes_read) || Number(p.bytes_read) < 0 || Number(p.bytes_read) > plugin.limits.max_input_bytes
    || typeof p.header_pending !== 'boolean' || typeof p.columns_truncated !== 'boolean' || ![null, ',', '\t'].includes(p.delimiter as string | null)) throw new Error('分页预览响应无效。');
  return { ...p, version: result.version } as unknown as FilePreviewPage;
}
export async function preparePluginMolecule(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal): Promise<MolecularPreviewPreparation> {
  const result = await requestVisualization(file, plugin, 'prepare', {}, signal);
  if (result.kind !== 'molecule' || typeof result.payload.source_name !== 'string' || !['cif', 'pdb', 'sdf', 'xyz', 'mol2', 'vasp'].includes(String(result.payload.source_format))) throw new Error('分子结构元数据无效。');
  return result.payload as unknown as MolecularPreviewPreparation;
}
export function visualizationJobsPath(file: FileInfo, plugin: VisualizationPlugin) {
  requireOperation(plugin, 'job');
  return `/files/${encodeURIComponent(file.file_id)}/visualization/jobs`;
}
