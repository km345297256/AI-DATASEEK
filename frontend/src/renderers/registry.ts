import type { RendererInfo } from '@/api/renderer';

/** Legacy records are retained for migration, never executable.
 * Runtime preview capability selection lives exclusively in visualizations/.
 */
export type RendererDefinition = RendererInfo & { editable: boolean };
export function rendererDefinitionsFromConfigs(configs: RendererInfo[]): RendererDefinition[] {
  return configs.map((config) => ({ ...config, editable: true }));
}
export function listRenderers(): RendererDefinition[] { return []; }
export function listBuiltinRenderers(): RendererDefinition[] { return []; }
