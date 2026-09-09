import { apiClient, type ApiResponse } from './client';
import { parseVisualizationCatalog, type VisualizationCatalog } from '../visualizations/contract';

export async function getVisualizationCatalog(signal?: AbortSignal): Promise<VisualizationCatalog> {
  const response = await apiClient.get<ApiResponse<unknown>>('/visualizations', { signal, params: { contract_version: 2 } });
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
  contract_version: 1;
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
export async function getScientificVisualization(fileId: string, options: ScientificOptions, signal: AbortSignal): Promise<ScientificVisualization> {
  const response = await apiClient.post<ApiResponse<ScientificVisualization>>(`/files/${encodeURIComponent(fileId)}/visualization`, options, { signal });
  const data = response.data.data;
  if (!data || data.contract_version !== 1 || data.plugin_id !== options.plugin_id
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
