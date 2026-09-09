import { defineAsyncComponent, markRaw, type Component } from 'vue';
import type { VisualizationAdapter } from './contract';

// Build-time trusted capability bindings. Manifest strings never become import URLs,
// iframe sources, API destinations, executable code, or arbitrary component props.
const trustedAdapters: Record<VisualizationAdapter, Component> = {
  image: markRaw(defineAsyncComponent(() => import('../components/filePreviews/ImageFilePreview.vue'))),
  tiff: markRaw(defineAsyncComponent(() => import('../components/filePreviews/TiffFilePreview.vue'))),
  shapefile: markRaw(defineAsyncComponent(() => import('../components/filePreviews/ShapefilePreview.vue'))),
  molecular: markRaw(defineAsyncComponent(() => import('../components/filePreviews/MolecularStructurePreview.vue'))),
  obj: markRaw(defineAsyncComponent(() => import('../components/filePreviews/ObjFilePreview.vue'))),
  html: markRaw(defineAsyncComponent(() => import('../components/filePreviews/HtmlFilePreview.vue'))),
  markdown: markRaw(defineAsyncComponent(() => import('../components/filePreviews/MarkdownFilePreview.vue'))),
  text: markRaw(defineAsyncComponent(() => import('../components/filePreviews/CodeFilePreview.vue'))),
  csv: markRaw(defineAsyncComponent(() => import('../components/filePreviews/CsvFilePreview.vue'))),
  'scientific-map': markRaw(defineAsyncComponent(() => import('./ScientificFilePreview.vue'))),
  'scientific-series': markRaw(defineAsyncComponent(() => import('./ScientificFilePreview.vue'))),
  'scientific-image': markRaw(defineAsyncComponent(() => import('./ScientificFilePreview.vue'))),
  'scientific-quality': markRaw(defineAsyncComponent(() => import('./ScientificFilePreview.vue'))),
};
export const getVisualizationAdapter = (adapter: VisualizationAdapter): Component | null => trustedAdapters[adapter] ?? null;
