import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { requestVisualization } from '../runtime';
export { loadPluginBytes } from '../runtime';

export interface ExtendedPreview {
  contract_version: 2;
  type: string;
  [key: string]: unknown;
}
export async function requestPreview(file: FileInfo, plugin: VisualizationPlugin, options: Record<string, unknown>, signal: AbortSignal): Promise<ExtendedPreview> {
  const result = await requestVisualization(file, plugin, 'preview', options, signal);
  return { ...result.payload, contract_version: 2, type: plugin.reader, kind: result.payload.view_kind ?? result.kind, version: result.version, revision: result.revision, metadata: result.metadata, warnings: result.warnings, sampled: result.sampled };
}
