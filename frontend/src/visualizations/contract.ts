/** One data-only capability contract. Registry entries cannot introduce executable code or URLs. */
import { VISUALIZATION_ADAPTERS, VISUALIZATION_CONTRACT_VERSION } from './adapters.generated.ts';
import type { VisualizationAdapter, VisualizationKind, VisualizationReader, VisualizationOperation, VisualizationInputMode } from './adapters.generated.ts';
export type { VisualizationAdapter, VisualizationKind, VisualizationReader, VisualizationOperation, VisualizationInputMode } from './adapters.generated.ts';
export const ADAPTER_IDS = Object.keys(VISUALIZATION_ADAPTERS) as VisualizationAdapter[];

export interface VisualizationCapabilities {
  operations: VisualizationOperation[];
  input_mode: VisualizationInputMode;
  shared: boolean;
}
export interface VisualizationPlugin {
  contract_version: 2;
  id: string;
  version: string;
  name: string;
  description: string;
  extensions: string[];
  filenames: string[];
  view_kind: VisualizationKind;
  adapter: VisualizationAdapter;
  reader: VisualizationReader;
  capabilities: VisualizationCapabilities;
  default_enabled: boolean;
  enabled: boolean;
  priority: number;
  permissions: ['file:read'];
  limits: { max_input_bytes: number; max_output_bytes: number };
}
export interface VisualizationCatalog {
  engine: 'cordis';
  revision: string;
  plugins: VisualizationPlugin[];
}

const descriptorFields = ['contract_version', 'id', 'version', 'name', 'description', 'extensions',
  'filenames', 'view_kind', 'adapter', 'reader', 'capabilities', 'default_enabled', 'enabled',
  'priority', 'permissions', 'limits'];
function exactRecord(value: unknown, fields: readonly string[]): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === fields.length && Object.keys(value).every(field => fields.includes(field));
}
function validMatchers(value: unknown, pattern: RegExp): value is string[] {
  return Array.isArray(value) && value.length <= 128 && new Set(value).size === value.length
    && value.every(item => typeof item === 'string' && pattern.test(item));
}

/** Reject an incompatible catalog as a whole; never revive an unregistered fallback renderer. */
export function parseVisualizationCatalog(value: unknown): VisualizationCatalog {
  if (!exactRecord(value, ['engine', 'revision', 'plugins']) || value.engine !== 'cordis'
      || typeof value.revision !== 'string' || !Array.isArray(value.plugins) || value.plugins.length > 256) {
    throw new Error('可视化插件目录格式不兼容，请更新服务后重试。');
  }
  const ids = new Set<string>();
  for (const raw of value.plugins) {
    if (!exactRecord(raw, descriptorFields)) throw new Error('可视化插件协议包含缺失或未知字段。');
    const plugin = raw as unknown as VisualizationPlugin;
    if (plugin.contract_version !== VISUALIZATION_CONTRACT_VERSION || typeof plugin.id !== 'string'
      || !/^[a-z][a-z0-9-]{0,63}$/.test(plugin.id) || ids.has(plugin.id)
      || typeof plugin.name !== 'string' || !plugin.name.trim() || plugin.name.length > 120
      || typeof plugin.description !== 'string' || plugin.description.length > 1000
      || typeof plugin.version !== 'string' || !/^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/.test(plugin.version)
      || typeof plugin.adapter !== 'string' || !Object.prototype.hasOwnProperty.call(VISUALIZATION_ADAPTERS, plugin.adapter)
      || !validMatchers(plugin.extensions, /^[a-z0-9][a-z0-9.-]{0,31}$/)
      || !validMatchers(plugin.filenames, /^(?:[a-z0-9][a-z0-9._-]{0,127}|\.zattrs)$/)
      || (!plugin.extensions.length && !plugin.filenames.length)
      || typeof plugin.enabled !== 'boolean' || typeof plugin.default_enabled !== 'boolean'
      || !Number.isSafeInteger(plugin.priority) || Math.abs(plugin.priority) > 1000
      || !Array.isArray(plugin.permissions) || plugin.permissions.length !== 1 || plugin.permissions[0] !== 'file:read'
      || !exactRecord(plugin.limits, ['max_input_bytes', 'max_output_bytes'])
      || !Number.isSafeInteger(plugin.limits.max_input_bytes) || plugin.limits.max_input_bytes <= 0 || plugin.limits.max_input_bytes > 512 * 1024 * 1024
      || !Number.isSafeInteger(plugin.limits.max_output_bytes) || plugin.limits.max_output_bytes <= 0 || plugin.limits.max_output_bytes > 16 * 1024 * 1024) {
      throw new Error('可视化插件协议不兼容，已停止加载预览。');
    }
    const spec = VISUALIZATION_ADAPTERS[plugin.adapter];
    if (plugin.view_kind !== spec.view_kind || !(spec.readers as readonly unknown[]).includes(plugin.reader)
      || !exactRecord(plugin.capabilities, ['operations', 'input_mode', 'shared'])
      || !Array.isArray(plugin.capabilities.operations)
      || plugin.capabilities.operations.length !== spec.capabilities.operations.length
      || new Set(plugin.capabilities.operations).size !== plugin.capabilities.operations.length
      || plugin.capabilities.operations.some(operation => !(spec.capabilities.operations as readonly unknown[]).includes(operation))
      || plugin.capabilities.input_mode !== spec.capabilities.input_mode
      || plugin.capabilities.shared !== spec.capabilities.shared) {
      throw new Error('可视化插件能力声明与批准的适配器规范不兼容。');
    }
    ids.add(plugin.id);
  }
  return value as unknown as VisualizationCatalog;
}

export function matchingVisualizations(plugins: readonly VisualizationPlugin[], filename: string): VisualizationPlugin[] {
  const name = filename.split(/[\\/]/).pop()?.toLowerCase() ?? '';
  return plugins.filter((plugin) => plugin.enabled && (plugin.filenames.some((item) => item.toLowerCase() === name)
    || plugin.extensions.some((item) => name.endsWith(`.${item.toLowerCase()}`))))
    .sort((left, right) => right.priority - left.priority || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0));
}

export function selectVisualization(plugins: readonly VisualizationPlugin[], filename: string, selectedId: string | null): VisualizationPlugin | null {
  const candidates = matchingVisualizations(plugins, filename);
  return candidates.find((plugin) => plugin.id === selectedId) ?? candidates[0] ?? null;
}

export const viewKindLabel = (kind: VisualizationKind): string => ({ image: '图像', map: '地图', series: '数值曲线', table: '表格', text: '文本', structure: '三维结构', document: '文档', tree: '结构树', media: '音视频', graph: '关系网络' }[kind]);
