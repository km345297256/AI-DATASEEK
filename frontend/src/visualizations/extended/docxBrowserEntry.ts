import { renderAsync, defaultOptions, type HElement } from 'docx-preview';
import { prepareSafeDocx } from './docxSafety';

// Runs only in a sandboxed opaque origin. No app/API/storage access or resource fetch.
const channel = document.querySelector<HTMLMetaElement>('meta[name="channel"]')!.content;
const parentOrigin = document.querySelector<HTMLMetaElement>('meta[name="parent-origin"]')!.content;
const content = document.getElementById('content')!;
const send = (type: string, extra: Record<string, unknown> = {}) => parent.postMessage({ channel, type, ...extra }, parentOrigin);
let started = false;
const imageUrls = new Set<string>(), createUrl = URL.createObjectURL.bind(URL);
URL.createObjectURL = blob => { const url = createUrl(blob); imageUrls.add(url); return url; };
const releaseImages = () => { for (const url of imageUrls) URL.revokeObjectURL(url); imageUrls.clear(); };
window.addEventListener('pagehide', releaseImages, { once: true });
document.addEventListener('click', event => { if ((event.target as Element)?.closest?.('a')) event.preventDefault(); }, true);
document.addEventListener('submit', event => event.preventDefault(), true);
document.addEventListener('drop', event => event.preventDefault(), true);
document.addEventListener('dragover', event => event.preventDefault(), true);
window.addEventListener('message', async event => {
  if (event.source !== parent || event.origin !== parentOrigin || event.data?.channel !== channel) return;
  if (event.data.type === 'zoom' && typeof event.data.zoom === 'number' && event.data.zoom >= 0.5 && event.data.zoom <= 3) {
    content.style.setProperty('zoom', String(event.data.zoom)); return;
  }
  if (started || event.data.type !== 'render' || !(event.data.bytes instanceof ArrayBuffer)) return;
  started = true;
  try {
    const validated = await prepareSafeDocx(new Uint8Array(event.data.bytes));
    let renderedNodes = 0, renderedCharacters = 0;
    const boundedNode = (node: HElement | Node | string): Node => {
      if (node instanceof Node) return node;
      if (++renderedNodes > 40000) throw new Error('DOCX 排版展开超过 40,000 个节点，请切换分页预览。');
      if (typeof node === 'string' && (renderedCharacters += node.length) > 8 * 1024 * 1024) throw new Error('DOCX 排版文本超过安全预算，请切换分页预览。');
      if (typeof node !== 'string' && node.children && !node.tagName.startsWith('#')) {
        return defaultOptions.h({ ...node, children: node.children.map(boundedNode) });
      }
      return defaultOptions.h(node);
    };
    await renderAsync(validated, content, content, {
      className: 'docx', inWrapper: true, breakPages: true, ignoreLastRenderedPageBreak: false,
      ignoreFonts: true, useBase64URL: false, renderAltChunks: false, renderComments: false,
      renderChanges: false, experimental: false, debug: false,
      // Headers/footers can multiply small XML; guard before every allocation.
      h: boundedNode,
    });
    for (const link of content.querySelectorAll('a')) { link.removeAttribute('href'); link.removeAttribute('target'); }
    if (content.querySelectorAll('*').length > 80000) throw new Error('DOCX 排版结果超过安全预算。');
    send('rendered', { pages: content.querySelectorAll('section.docx').length });
  } catch (reason) {
    releaseImages();
    content.replaceChildren();
    send('error', { message: reason instanceof Error ? reason.message.slice(0, 300) : 'DOCX 阅读失败。' });
  }
});
send('ready');
