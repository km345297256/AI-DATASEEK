import { apiClient, type ApiResponse } from './client';
import { parseVisualizationCatalog, type VisualizationCatalog } from '../visualizations/contract';
import type { VisualizationPlugin } from '../visualizations/contract';
import type { FileInfo } from './file';
import { requestVisualization } from '../visualizations/runtime';

export async function getVisualizationCatalog(signal?: AbortSignal): Promise<VisualizationCatalog> {
  const response = await apiClient.get<ApiResponse<unknown>>('/visualizations', { signal });
  return parseVisualizationCatalog(response.data.data);
}

export async function setVisualizationEnabled(id: string, enabled: boolean): Promise<VisualizationCatalog> {
  await apiClient.patch(`/visualizations/${encodeURIComponent(id)}/state`, { enabled });
  // Read the authoritative effective catalog, including Cordis lifecycle revision.
  return getVisualizationCatalog();
}

export interface ScientificVariable {
  name: string;
  dimensions: Array<{ name: string; size: number }>;
  shape: number[];
  units?: string | null;
}
export interface ScientificVisualization {
  kind: 'map' | 'series' | 'image' | 'quality';
  plugin_id: string;
  revision: string;
  version: string;
  reader: string;
  variables: ScientificVariable[];
  selected_variable: string | null;
  x_label: string;
  y_label: string;
  x: number[];
  y: Array<number | null>;
  width: number;
  height: number;
  values: Array<number | null>;
  extent: [number, number, number, number] | null;
  metadata: Record<string, unknown>;
  warnings: string[];
  sampled: boolean;
}
export interface ScientificOptions {
  plugin_id: string;
  variable?: string;
  x_dimension?: string;
  indices?: Record<string, number>;
  hdu?: number;
  version?: string;
}
export async function getScientificVisualization(file: FileInfo, plugin: VisualizationPlugin, options: ScientificOptions, signal: AbortSignal): Promise<ScientificVisualization> {
  const { plugin_id: _pluginId, ...readerOptions } = options;
  const result = await requestVisualization(file, plugin, 'preview', { ...readerOptions, version: options.version }, signal);
  const data = { ...result.payload, plugin_id: result.plugin_id, reader: plugin.reader, kind: result.payload.view_kind, revision: result.revision, version: result.version, metadata: result.metadata, warnings: result.warnings, sampled: result.sampled } as ScientificVisualization;
  if (!data || data.plugin_id !== options.plugin_id
    || !['map', 'series', 'image', 'quality'].includes(data.kind) || !Array.isArray(data.values) || data.values.length > 128 * 128
    || !Array.isArray(data.x) || data.x.length > 1000 || !Array.isArray(data.y) || data.y.length > 1000
    || !Array.isArray(data.variables) || data.variables.length > 128 || typeof data.version !== 'string'
    || !data.x.every((value) => typeof value === 'number' && Number.isFinite(value))
    || ![...data.y, ...data.values].every((value) => value === null || typeof value === 'number' && Number.isFinite(value))
    || !data.metadata || typeof data.metadata !== 'object' || Array.isArray(data.metadata)
    || !Array.isArray(data.warnings) || !data.warnings.every((value) => typeof value === 'string')
    || typeof data.x_label !== 'string' || typeof data.y_label !== 'string' || typeof data.sampled !== 'boolean') {
    throw new Error('科学数据预览响应不符合插件协议。');
  }
  if ((data.kind === 'series' || data.kind === 'quality') && data.y.length !== data.x.length) {
    throw new Error('科学数据曲线坐标不完整。');
  }
  if ((data.kind === 'map' || data.kind === 'image') && (!Number.isSafeInteger(data.width) || !Number.isSafeInteger(data.height)
    || data.width < 1 || data.height < 1 || data.width > 128 || data.height > 128 || data.values.length !== data.width * data.height)) {
    throw new Error('科学图像栅格尺寸不符合插件协议。');
  }
  if (data.kind === 'map' && (data.x.length !== data.width || data.y.length !== data.height || data.y.some((value) => value === null))) {
    throw new Error('地图插件缺少明确的经纬度栅格坐标。');
  }
  return data;
}
