// Fixed upstream subpaths ship JavaScript without discoverable export-map types.
// Keep these declarations local to trusted adapters, never use a dynamic path.
declare module 'plotly.js-cartesian-dist-min' {
  import * as Plotly from 'plotly.js';
  export default Plotly;
}
declare module 'jsroot/core' {
  export function create(name: string): Record<string, any>;
  export function createHistogram(name: string, nx: number, ny?: number): Record<string, any>;
  export function createTGraph(n: number, x: number[], y: number[]): Record<string, any>;
  export const settings: { ContextMenu: boolean; ToolBar: boolean | string; Latex: number };
}
declare module 'jsroot/draw' {
  export function draw(element: HTMLElement, object: Record<string, any>, options: string): Promise<unknown>;
  export function cleanup(element: HTMLElement): void;
}
declare module '@kitware/vtk.js/Rendering/Profiles/Geometry';
declare module '@kitware/vtk.js/Rendering/Profiles/Volume';
