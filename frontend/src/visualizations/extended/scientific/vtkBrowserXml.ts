/** vtk.js XMLReader's browser-only create(...).root().node interface.
 * XMLWriter and appended/binary features are intentionally not implemented. */
export function create(source: string) {
  if (typeof source !== 'string' || source.length > 8 * 1024 * 1024 || /<!DOCTYPE|<!ENTITY|<AppendedData|compressor\s*=|format\s*=\s*["'](?:binary|appended)/i.test(source)) throw new Error('VTK 浏览器读取仅允许有界的 ASCII XML。');
  const document = new DOMParser().parseFromString(source, 'application/xml');
  if (document.querySelector('parsererror') || document.documentElement?.tagName !== 'VTKFile') throw new Error('VTK XML 结构无效。');
  return { root: () => ({ node: document.documentElement, filter: () => { throw new Error('此插件不支持 VTK appended 数据。'); } }) };
}
