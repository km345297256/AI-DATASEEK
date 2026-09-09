/** OOXML validation runs inside an opaque, network-disabled reading frame. */
import JSZip from 'jszip';

const MiB = 1024 * 1024;
const fail = (): never => { throw new Error('DOCX 包结构或安全预算不符合要求；请使用 Word 转换预览。'); };
export interface DocxEntry { name: string; method: number; offset: number; size: number; compressed: number }

export function inspectDocxZip(bytes: Uint8Array): DocxEntry[] {
  if (bytes.length < 22 || bytes.length > 16 * MiB) return fail();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (view.getUint32(i, true) === 0x06054b50 && i + 22 + view.getUint16(i + 20, true) === bytes.length) { end = i; break; }
  }
  if (end < 0 || view.getUint16(end + 4, true) || view.getUint16(end + 6, true)) return fail();
  const count = view.getUint16(end + 10, true), start = view.getUint32(end + 16, true);
  if (!count || count > 512 || count !== view.getUint16(end + 8, true) || start + view.getUint32(end + 12, true) !== end) return fail();
  const entries: DocxEntry[] = [], names = new Set<string>();
  let cursor = start, total = 0, xmlTotal = 0;
  const utf8 = new TextDecoder('utf-8', { fatal: true });
  for (let i = 0; i < count; i++) {
    if (cursor + 46 > end || view.getUint32(cursor, true) !== 0x02014b50) return fail();
    const flags = view.getUint16(cursor + 8, true), method = view.getUint16(cursor + 10, true);
    const compressed = view.getUint32(cursor + 20, true), size = view.getUint32(cursor + 24, true);
    const nameLength = view.getUint16(cursor + 28, true), extraLength = view.getUint16(cursor + 30, true), commentLength = view.getUint16(cursor + 32, true);
    const local = view.getUint32(cursor + 42, true);
    if (flags & 0x2041 || ![0, 8].includes(method) || view.getUint16(cursor + 34, true) || cursor + 46 + nameLength + extraLength + commentLength > end) return fail();
    const name = utf8.decode(bytes.subarray(cursor + 46, cursor + 46 + nameLength));
    if (!name || /[\\\x00-\x1f:#%]/.test(name) || name.startsWith('/') || name.split('/').some(part => part === '..' || part === '.') || names.has(name.toLowerCase())) return fail();
    names.add(name.toLowerCase());
    if (local + 30 > start || view.getUint32(local, true) !== 0x04034b50 || view.getUint16(local + 6, true) !== flags || view.getUint16(local + 8, true) !== method) return fail();
    const localNameLength = view.getUint16(local + 26, true), localExtraLength = view.getUint16(local + 28, true);
    const offset = local + 30 + localNameLength + localExtraLength;
    if (offset + compressed > start || utf8.decode(bytes.subarray(local + 30, local + 30 + localNameLength)) !== name) return fail();
    if (!(flags & 8) && (view.getUint32(local + 18, true) !== compressed || view.getUint32(local + 22, true) !== size)) return fail();
    if (size > 16 * MiB || (total += size) > 32 * MiB || size > Math.max(1024 * 1024, compressed * 250)) return fail();
    if (/\.(xml|rels)$/i.test(name) && (size > 2 * MiB || (xmlTotal += size) > 8 * MiB)) return fail();
    if (name.endsWith('/')) { if (size) return fail(); }
    else entries.push({ name, method, offset, size, compressed });
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  if (cursor !== end || !names.has('[content_types].xml') || !names.has('word/document.xml')) return fail();
  return entries;
}

async function inflateEntry(bytes: Uint8Array, entry: DocxEntry): Promise<Uint8Array> {
  const packed = bytes.slice(entry.offset, entry.offset + entry.compressed);
  if (entry.method === 0) { if (packed.length !== entry.size) return fail(); return packed; }
  // Streaming limit checks actual decoded bytes, not just attacker-controlled ZIP sizes.
  const reader = new Blob([packed]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
  const chunks: Uint8Array[] = []; let length = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      length += value.length;
      if (length > entry.size) { await reader.cancel(); return fail(); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  if (length !== entry.size) return fail();
  const result = new Uint8Array(length); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

export function imagePixels(bytes: Uint8Array): number {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length >= 24 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a && view.getUint32(12) === 0x49484452) {
    return view.getUint32(16) * view.getUint32(20);
  }
  if (bytes.length > 4 && view.getUint16(0) === 0xffd8) {
    let cursor = 2;
    while (cursor + 4 <= bytes.length) {
      if (bytes[cursor++] !== 0xff) return fail();
      while (bytes[cursor] === 0xff) cursor++;
      const marker = bytes[cursor++];
      if (marker === 0xda || marker === 0xd9) break;
      const length = view.getUint16(cursor); if (length < 2 || cursor + length > bytes.length) return fail();
      if ([0xc0, 0xc1, 0xc2].includes(marker) && length >= 8) return view.getUint16(cursor + 3) * view.getUint16(cursor + 5);
      cursor += length;
    }
  }
  return fail();
}

export async function prepareSafeDocx(bytes: Uint8Array): Promise<Uint8Array> {
  const entries = inspectDocxZip(bytes), zip = new JSZip();
  const media = new Map<string, Uint8Array>();
  const relationships = new Map<string, Map<string, string>>();
  const imageReferences: Array<{ part: string; id: string }> = [];
  const resolveTarget = (part: string, target: string) => {
    const base = part === '_rels/.rels' ? '' : part.slice(0, part.lastIndexOf('/_rels/') + 1);
    const pieces: string[] = [];
    for (const segment of (base + target).split('/')) {
      if (segment === '..') { if (!pieces.length) return fail(); pieces.pop(); }
      else if (segment && segment !== '.') pieces.push(segment);
    }
    return pieces.join('/');
  };
  let nodes = 0, pixels = 0;
  for (const entry of entries.filter(item => /^word\/media\/[^/]+\.(png|jpe?g)$/i.test(item.name))) {
    const data = await inflateEntry(bytes, entry), count = imagePixels(data);
    if (!count || (pixels += count) > 16 * MiB) throw new Error('DOCX 内嵌图片超过 16M 像素预算，请切换 Word 预览。');
    media.set(entry.name, data);
  }
  for (const entry of entries) {
    const lower = entry.name.toLowerCase();
    if (/vbaproject|activex|embeddings|altchunk|customui|\.bin$/.test(lower)) throw new Error('此 DOCX 包含宏、嵌入对象或活动内容，已拒绝阅读。');
    // Embedded fonts are not loaded. All glyphs use the browser's local fonts.
    if (lower.startsWith('word/fonts/')) continue;
    const data = media.get(entry.name) ?? await inflateEntry(bytes, entry);
    if (/\.(xml|rels)$/.test(lower)) {
      const xml = new TextDecoder('utf-8', { fatal: true }).decode(data);
      if (/<!DOCTYPE|<!ENTITY/i.test(xml)) return fail();
      const document = new DOMParser().parseFromString(xml, 'application/xml');
      if (document.querySelector('parsererror')) return fail();
      const elements = [...document.getElementsByTagName('*')];
      const depths = new Map<Element, number>();
      if ((nodes += elements.length) > 40000) throw new Error('DOCX 内容超过 40,000 个排版节点，请切换分页预览。');
      for (const node of elements) {
        const depth = (node.parentElement ? depths.get(node.parentElement) ?? 0 : 0) + 1;
        if (depth > 128) throw new Error('DOCX XML 层级超过安全预算。');
        depths.set(node, depth);
        for (const attribute of [...node.attributes]) {
          // SDK accepts raw VML styles and interpolates selected OOXML values in
          // stylesheets. Reject CSS escapes/URLs/delimiters, never file text.
          const guid = /^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$/i.test(attribute.value);
          if (!guid && /url\s*\(|@import|expression\s*\(|[\\{}]|\/\*/i.test(attribute.value)) throw new Error('DOCX 含不支持的样式资源。');
        }
        if (/^(altChunk|object|OLEObject|control|externalData)$/i.test(node.localName)) throw new Error('DOCX 含不支持的活动内容。');
        if (['blip', 'imagedata'].includes(node.localName)) {
          const id = [...node.attributes].find(attribute => ['embed', 'id'].includes(attribute.localName))?.value;
          if (id) imageReferences.push({ part: entry.name, id });
        }
        if (node.localName === 'Relationship') {
          const target = node.getAttribute('Target') ?? '';
          if ((node.getAttribute('TargetMode') ?? '').toLowerCase() === 'external') {
            if ((node.getAttribute('Type') ?? '').endsWith('/hyperlink')) { node.remove(); continue; }
            throw new Error('DOCX 引用了外部内容，本机只读阅读不会联网加载。');
          }
          if (/^[a-z][a-z\d+.-]*:|^\/\/|[\\\x00-\x1f]/i.test(target)) return fail();
          const id = node.getAttribute('Id') ?? '';
          const rels = relationships.get(entry.name) ?? new Map<string, string>();
          if (!id || rels.has(id)) return fail();
          rels.set(id, resolveTarget(entry.name, target)); relationships.set(entry.name, rels);
          if ((node.getAttribute('Type') ?? '').endsWith('/image') && !media.has(resolveTarget(entry.name, target))) throw new Error('DOCX 图片关系必须指向已校验的 PNG/JPEG。');
        }
        if (node.localName === 'Override' && /macroenabled|oleobject|html/i.test(node.getAttribute('ContentType') ?? '')) return fail();
        if (['Default', 'Override'].includes(node.localName) && node.hasAttribute('ContentType')) {
          const mime = node.getAttribute('ContentType')!.trim().toLowerCase();
          node.setAttribute('ContentType', mime);
          if (mime.startsWith('image/') && !['image/png', 'image/jpeg'].includes(mime)) throw new Error('DOCX 图片仅支持已校验的 PNG/JPEG。');
          if (mime.startsWith('image/')) {
            const part = node.getAttribute('PartName')?.replace(/^\//, ''), extension = node.getAttribute('Extension');
            const matches = part ? [part] : entries.filter(item => item.name.endsWith(`.${extension}`)).map(item => item.name);
            if (matches.some(name => !media.has(name) && !name.startsWith('docProps/thumbnail.'))) return fail();
          }
        }
      }
      zip.file(entry.name, new XMLSerializer().serializeToString(document));
    } else if (/^word\/media\/[^/]+\.(png|jpe?g)$/i.test(entry.name)) {
      zip.file(entry.name, data);
    } else if (/^docprops\/thumbnail\./.test(lower)) { /* Not displayed. */ }
    else throw new Error('DOCX 含当前阅读插件不支持的资源，请切换 Word 预览。');
  }
  for (const { part, id } of imageReferences) {
    const slash = part.lastIndexOf('/');
    const relsPath = `${part.slice(0, slash + 1)}_rels/${part.slice(slash + 1)}.rels`;
    const target = relationships.get(relsPath)?.get(id);
    if (!target || !media.has(target)) throw new Error('DOCX 图片引用必须指向已校验的 PNG/JPEG。');
  }
  return zip.generateAsync({ type: 'uint8array', compression: 'STORE' });
}
