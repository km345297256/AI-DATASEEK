/** Public, versioned capability contract. No script, URL or component entry is executable. */
export const ADAPTER_IDS = ['image', 'tiff', 'shapefile', 'molecular', 'obj', 'html', 'markdown', 'text', 'csv', 'scientific-map', 'scientific-series', 'scientific-image', 'scientific-quality', 'v2-plotly', 'v2-h5web', 'v2-vtk', 'v2-jsroot', 'v2-rdkit', 'v2-molstar', 'v2-nmrium', 'v2-openlayers', 'v2-maplibre', 'v2-cesium', 'v2-aladin', 'v2-metpy', 'v2-igv', 'v2-viv', 'v2-niivue', 'v2-fastqc', 'v2-pdfjs', 'v2-word', 'v2-excel', 'v2-powerpoint'] as const;
export type VisualizationAdapter = typeof ADAPTER_IDS[number];
export type VisualizationKind = 'image' | 'map' | 'series' | 'table' | 'text' | 'structure' | 'document';
export interface VisualizationPlugin {
  contract_version: 1 | 2;
  id: string;
  version: string;
  name: string;
  description: string;
  extensions: string[];
  filenames: string[];
  view_kind: VisualizationKind;
  adapter: VisualizationAdapter;
  data_kind: 'file' | 'scientific' | 'extended';
  reader: 'netcdf' | 'fits' | 'fastq' | 'binary' | 'tabular' | 'hdf5' | 'rdkit' | 'metpy' | 'fastqc' | 'office' | 'excel' | 'root' | 'jcamp' | null;
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
const kinds = new Set(['image', 'map', 'series', 'table', 'text', 'structure', 'document']);
const adapterKinds: Record<VisualizationAdapter, VisualizationKind> = {
  image: 'image', tiff: 'image', shapefile: 'map', molecular: 'structure', obj: 'structure',
  html: 'document', markdown: 'document', text: 'text', csv: 'table',
  'scientific-map': 'map', 'scientific-series': 'series', 'scientific-image': 'image', 'scientific-quality': 'series',
  'v2-plotly': 'series', 'v2-h5web': 'image', 'v2-vtk': 'structure', 'v2-jsroot': 'series',
  'v2-rdkit': 'image', 'v2-molstar': 'structure', 'v2-nmrium': 'series', 'v2-openlayers': 'map',
  'v2-maplibre': 'map', 'v2-cesium': 'map', 'v2-aladin': 'map', 'v2-metpy': 'image',
  'v2-igv': 'series', 'v2-viv': 'image', 'v2-niivue': 'image', 'v2-fastqc': 'table',
  'v2-pdfjs': 'document', 'v2-word': 'document', 'v2-excel': 'table', 'v2-powerpoint': 'document',
};
const extendedReaders: Partial<Record<VisualizationAdapter, VisualizationPlugin['reader']>> = {
  'v2-plotly': 'tabular', 'v2-h5web': 'hdf5', 'v2-vtk': 'binary', 'v2-jsroot': 'root',
  'v2-rdkit': 'rdkit', 'v2-molstar': 'binary', 'v2-nmrium': 'jcamp', 'v2-openlayers': 'binary',
  'v2-maplibre': 'binary', 'v2-cesium': 'binary', 'v2-aladin': 'binary', 'v2-metpy': 'metpy',
  'v2-igv': 'binary', 'v2-viv': 'binary', 'v2-niivue': 'binary', 'v2-fastqc': 'fastqc',
  'v2-pdfjs': 'binary', 'v2-word': 'office', 'v2-excel': 'excel', 'v2-powerpoint': 'office',
};

/** Reject the catalog as a whole on incompatible contracts: never revive a fallback renderer. */
export function parseVisualizationCatalog(value: unknown): VisualizationCatalog {
  const catalog = value as VisualizationCatalog;
  if (!catalog || catalog.engine !== 'cordis' || typeof catalog.revision !== 'string' || !Array.isArray(catalog.plugins) || catalog.plugins.length > 256) {
    throw new Error('可视化插件目录格式不兼容，请更新服务后重试。');
  }
  const ids = new Set<string>();
  for (const plugin of catalog.plugins) {
    if (!plugin || ![1, 2].includes(plugin.contract_version) || typeof plugin.id !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(plugin.id) || ids.has(plugin.id)
      || typeof plugin.name !== 'string' || typeof plugin.description !== 'string' || typeof plugin.version !== 'string'
      || !(ADAPTER_IDS as readonly string[]).includes(plugin.adapter) || !kinds.has(plugin.view_kind)
      || adapterKinds[plugin.adapter] !== plugin.view_kind
      || !Array.isArray(plugin.extensions) || plugin.extensions.length > 128 || !plugin.extensions.every((item) => typeof item === 'string' && /^[a-z0-9][a-z0-9.-]{0,31}$/i.test(item))
      || !Array.isArray(plugin.filenames) || !plugin.filenames.every((item) => typeof item === 'string' && item.length > 0 && !/[\\/]/.test(item))
      || typeof plugin.enabled !== 'boolean' || typeof plugin.default_enabled !== 'boolean' || !Number.isSafeInteger(plugin.priority) || Math.abs(plugin.priority) > 1000
      || !Array.isArray(plugin.permissions) || plugin.permissions.length !== 1 || plugin.permissions[0] !== 'file:read'
      || !plugin.limits || !Number.isSafeInteger(plugin.limits.max_input_bytes) || plugin.limits.max_input_bytes <= 0 || plugin.limits.max_input_bytes > 512 * 1024 * 1024
      || !Number.isSafeInteger(plugin.limits.max_output_bytes) || plugin.limits.max_output_bytes <= 0 || plugin.limits.max_output_bytes > 16 * 1024 * 1024) {
      throw new Error('可视化插件协议不兼容，已停止加载预览。');
    }
    if (plugin.adapter.startsWith('v2-')) {
      if (plugin.contract_version !== 2 || plugin.data_kind !== 'extended' || extendedReaders[plugin.adapter] !== plugin.reader) {
        throw new Error('可视化插件 v2 读取协议不兼容。');
      }
      ids.add(plugin.id);
      continue;
    }
    if (plugin.contract_version !== 1) throw new Error('旧版适配器不可使用新版协议。');
    const scientific = plugin.adapter.startsWith('scientific-');
    if (scientific !== (plugin.data_kind === 'scientific') || (!scientific && (plugin.data_kind !== 'file' || plugin.reader !== null))
      || (scientific && !['netcdf', 'fits', 'fastq'].includes(plugin.reader ?? ''))
      || (plugin.adapter === 'scientific-map' && plugin.reader !== 'netcdf')
      || (plugin.adapter === 'scientific-image' && plugin.reader !== 'fits')
      || (plugin.adapter === 'scientific-quality' && plugin.reader !== 'fastq')
      || (plugin.adapter === 'scientific-series' && !['netcdf', 'fits'].includes(plugin.reader ?? ''))) {
      throw new Error('可视化插件数据读取协议不兼容。');
    }
    ids.add(plugin.id);
  }
  return catalog;
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

export const viewKindLabel = (kind: VisualizationKind): string => ({ image: '图像', map: '地图', series: '数值曲线', table: '表格', text: '文本', structure: '三维结构', document: '文档' }[kind]);
