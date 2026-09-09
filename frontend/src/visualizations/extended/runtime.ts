import { BASE_URL } from '../../api/client';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { readBoundedBinary } from '../boundedBinary';

export interface ExtendedPreview {
  contract_version: 2;
  type: string;
  [key: string]: unknown;
}
// Scope versions to the current file object, never a URL or persistent browser storage.
const versions = new WeakMap<FileInfo, string>();
async function fetchPreview(file: FileInfo, plugin: VisualizationPlugin, options: Record<string, unknown>, signal: AbortSignal, binary: boolean) {
  if (!plugin.enabled || plugin.contract_version !== 2 || plugin.data_kind !== 'extended') throw new Error('可视化插件未启用或协议不兼容。');
  const { kind, ...readerOptions } = options;
  const response = await fetch(`${BASE_URL}/files/${encodeURIComponent(file.file_id)}/${binary ? 'visualization-content' : 'visualization-v2'}`, {
    method: 'POST', credentials: 'same-origin', signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plugin_id: plugin.id, version: versions.get(file), kind, options: readerOptions }),
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(({ 403: '该插件已停用，请在插件管理中启用。', 404: '文件或插件不存在。', 409: '文件或插件版本变化，请重新打开预览。', 422: '此文件或参数无法安全预览，请核对格式及读取选项。' } as Record<number, string>)[response.status] ?? '隔离预览暂不可用，请检查服务及插件依赖。');
  }
  return response;
}

export async function loadPluginBytes(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal): Promise<ArrayBuffer> {
  const limit = Math.min(64 * 1024 * 1024, plugin.limits.max_input_bytes);
  if (file.size != null && file.size > limit) throw new Error('文件超过交互式预览大小上限。');
  const response = await fetchPreview(file, plugin, {}, signal, true);
  const bytes = await readBoundedBinary(response, limit, signal);
  const version = response.headers.get('X-Preview-Version');
  if (version && /^[a-f0-9]{64}$/.test(version)) versions.set(file, version);
  return bytes;
}

export async function requestPreview(file: FileInfo, plugin: VisualizationPlugin, options: Record<string, unknown>, signal: AbortSignal): Promise<ExtendedPreview> {
  const response = await fetchPreview(file, plugin, options, signal, false);
  const bytes = await readBoundedBinary(response, Math.min(8 * 1024 * 1024, plugin.limits.max_output_bytes) + 4096, signal);
  const envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  const value = envelope.data;
  if (envelope.code !== 0 || !value || value.contract_version !== 2 || value.plugin_id !== plugin.id || value.type !== plugin.reader || typeof value.version !== 'string' || !/^[a-f0-9]{64}$/.test(value.version)) throw new Error('预览返回了不兼容的 v2 数据。');
  versions.set(file, value.version);
  return value;
}
