import type { FileInfo } from '../api/file';
import type { VisualizationPlugin } from './contract';
import { requestVisualization, type VisualizationResult } from './runtime';

export interface SvgPreview {
  blob: Blob;
  width: number;
  height: number;
  sourceWidth?: number;
  sourceHeight?: number;
  warnings: string[];
}

/** Only the isolated renderer's bounded PNG reaches the DOM, never SVG markup. */
export function parseSvgPreview(result: VisualizationResult, maxBytes: number): SvgPreview {
  const { payload, metadata } = result;
  const invalid = () => new Error('SVG 预览响应无效，请重新打开文件。');
  const encoded = payload.data_base64;
  if (result.kind !== 'media' || payload.media_type !== 'image/png' || payload.view_kind !== 'image'
    || metadata.source_format !== 'SVG' || metadata.rasterized !== true
    || !Number.isSafeInteger(maxBytes) || maxBytes <= 0 || maxBytes > 16 * 1024 * 1024
    || typeof encoded !== 'string' || encoded.length < 44 || encoded.length > Math.ceil(maxBytes / 3) * 4
    || encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw invalid();
  let decoded: string;
  try { decoded = atob(encoded); } catch { throw invalid(); }
  if (decoded.length > maxBytes || btoa(decoded) !== encoded) throw invalid();
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
  if (bytes.length < 33 || ![137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82]
    .every((byte, index) => bytes[index] === byte)) throw invalid();
  const header = new DataView(bytes.buffer);
  const width = header.getUint32(16), height = header.getUint32(20);
  if (width < 1 || height < 1 || width > 2048 || height > 2048
    || metadata.width !== width || metadata.height !== height) throw invalid();
  return {
    blob: new Blob([bytes], { type: 'image/png' }), width, height,
    sourceWidth: typeof metadata.source_width === 'number' && Number.isFinite(metadata.source_width) && metadata.source_width > 0 ? metadata.source_width : undefined,
    sourceHeight: typeof metadata.source_height === 'number' && Number.isFinite(metadata.source_height) && metadata.source_height > 0 ? metadata.source_height : undefined,
    warnings: result.warnings,
  };
}

export async function loadSvgPreview(file: FileInfo, plugin: VisualizationPlugin, signal: AbortSignal): Promise<SvgPreview> {
  signal.throwIfAborted();
  if (plugin.adapter !== 'svg' || plugin.reader !== 'svg' || !plugin.enabled) throw new Error('SVG 可视化插件未启用。');
  if (file.size != null && (!Number.isFinite(file.size) || file.size < 0 || file.size > plugin.limits.max_input_bytes)) {
    throw new Error('SVG 文件超过交互式预览大小上限，仍可下载原文件。');
  }
  const result = await requestVisualization(file, plugin, 'preview', { kind: 'image' }, signal);
  signal.throwIfAborted();
  return parseSvgPreview(result, plugin.limits.max_output_bytes);
}
