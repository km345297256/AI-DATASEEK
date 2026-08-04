import type { Component } from 'vue';
import FileIcon from '@/components/icons/FileIcon.vue';
import ImageFilePreview from '@/components/filePreviews/ImageFilePreview.vue';
import ObjFilePreview from '@/components/filePreviews/ObjFilePreview.vue';
import TiffFilePreview from '@/components/filePreviews/TiffFilePreview.vue';
import ShapefilePreview from '@/components/filePreviews/ShapefilePreview.vue';
import HtmlFilePreview from '@/components/filePreviews/HtmlFilePreview.vue';
import type { RendererInfo } from '@/api/renderer';

export type RendererKind = 'builtin' | 'api' | 'component';

export interface RendererDefinition {
  id: string;
  name: string;
  description: string;
  kind: RendererKind;
  extensions: string[];
  preview: Component;
  icon: Component;
  enabled: boolean;
  scope?: 'global' | 'user';
  user_id?: string | null;
  owner_user_id?: string | null;
  api_url?: string | null;
  entry?: string | null;
  config?: Record<string, any>;
  editable?: boolean;
  installed?: boolean;
  source?: 'official' | 'personal';
}

const builtinRenderers: RendererDefinition[] = [
  {
    id: 'builtin-png-image',
    name: 'PNG Image Renderer',
    description: 'Built-in renderer for PNG images using a signed file URL.',
    kind: 'builtin',
    extensions: ['png'],
    preview: ImageFilePreview,
    icon: FileIcon,
    enabled: true,
    scope: 'global',
    editable: false,
    installed: true,
    source: 'official',
  },
  {
    id: 'builtin-obj-online3dviewer',
    name: 'OBJ 3D Model Renderer',
    description: 'Built-in renderer for OBJ models using Online3DViewer.',
    kind: 'builtin',
    extensions: ['obj'],
    preview: ObjFilePreview,
    icon: FileIcon,
    enabled: true,
    scope: 'global',
    editable: false,
    installed: true,
    source: 'official',
  },
  {
    id: 'builtin-tiff-image',
    name: 'TIFF Image Renderer',
    description: 'Built-in renderer for TIFF and GeoTIFF images using browser-side decoding.',
    kind: 'builtin',
    extensions: ['tif', 'tiff'],
    preview: TiffFilePreview,
    icon: FileIcon,
    enabled: true,
    scope: 'global',
    editable: false,
    installed: true,
    source: 'official',
  },
  {
    id: 'builtin-shapefile',
    name: 'Shapefile Renderer',
    description: 'Built-in renderer for Shapefile geometry and DBF attributes.',
    kind: 'builtin',
    extensions: ['shp', 'shx', 'dbf', 'prj', 'cpg'],
    preview: ShapefilePreview,
    icon: FileIcon,
    enabled: true,
    scope: 'global',
    editable: false,
    installed: true,
    source: 'official',
  },
  {
    id: 'builtin-html',
    name: 'HTML Renderer',
    description: 'Built-in renderer for HTML files with task-local relative asset resolution.',
    kind: 'builtin',
    extensions: ['html', 'htm'],
    preview: HtmlFilePreview,
    icon: FileIcon,
    enabled: true,
    scope: 'global',
    editable: false,
    installed: true,
    source: 'official',
  },
];

let configuredRenderers: RendererDefinition[] = [];

export function listRenderers(): RendererDefinition[] {
  return [...builtinRenderers, ...configuredRenderers];
}

export function rendererDefinitionsFromConfigs(configs: RendererInfo[]): RendererDefinition[] {
  return configs.map((config) => ({
    id: config.id,
    name: config.name,
    description: config.description,
    kind: config.kind,
    extensions: config.extensions,
    preview: ImageFilePreview,
    icon: FileIcon,
    enabled: config.enabled,
    scope: config.scope,
    user_id: config.user_id,
    owner_user_id: config.owner_user_id,
    api_url: config.api_url,
    entry: config.entry,
    config: config.config,
    editable: true,
    installed: config.installed,
    source: config.source,
  }));
}

export function mergeRendererConfigs(configs: RendererInfo[]): RendererDefinition[] {
  configuredRenderers = rendererDefinitionsFromConfigs(configs);
  return listRenderers();
}

export function listBuiltinRenderers(): RendererDefinition[] {
  return [...builtinRenderers];
}

export function findRendererByFilename(filename: string): RendererDefinition | null {
  const extension = filename.split('.').pop()?.toLowerCase();
  if (!extension) return null;
  return listRenderers().find((renderer) => renderer.enabled && renderer.extensions.includes(extension)) || null;
}
