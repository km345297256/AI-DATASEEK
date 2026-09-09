import type { useDomainScope } from './lifecycle';

/** Only trusted bundled bootstrap code enters this frame; dataset bytes travel by structured clone. */
export function isolatedDomainFrame(
  target: HTMLElement,
  scope: ReturnType<typeof useDomainScope>,
  options: { title: string; head?: string; bootstrap: string; allowSdkEval?: boolean; onMessage: (type: string, payload: any) => void },
) {
  const channel = crypto.randomUUID().replace(/-/g, '');
  const origin = location.origin;
  const assetRoot = new URL(`${import.meta.env.BASE_URL}visualization-assets/`, origin).href;
  const frame = document.createElement('iframe');
  frame.title = options.title; frame.setAttribute('sandbox', 'allow-scripts allow-same-origin');
  frame.style.cssText = 'border:0;width:100%;height:100%;min-height:320px;display:block';
  // Caller head/bootstrap are source-controlled code, never dataset fields or server-returned HTML.
  const head = (options.head || '').split('__ASSET_ROOT__').join(assetRoot).split('__NONCE__').join(channel);
  // Cesium's pinned Knockout widgets / Emscripten bindings require Function construction.
  // Only that trusted SDK frame opts in; no data is interpolated into head/bootstrap or eval.
  const sdkEval = options.allowSdkEval ? " 'unsafe-eval' blob:" : '';
  const source = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${channel}' 'self' 'wasm-unsafe-eval'${sdkEval}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' data: blob:; worker-src 'self' blob:; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none';"><style>html,body,#viewer{height:100%;width:100%;margin:0;overflow:hidden;background:#102030}*{box-sizing:border-box}</style>${head}</head><body><div id="viewer"></div><script type="module" nonce="${channel}">
const channel=${JSON.stringify(channel)},parentOrigin=${JSON.stringify(origin)},assetRoot=${JSON.stringify(assetRoot)};
const notify=(type,payload={})=>parent.postMessage({channel,type,payload},parentOrigin);
const trusted=(event)=>event.source===parent&&event.origin===parentOrigin&&event.data?.channel===channel;
${options.bootstrap}
<\/script></body></html>`;
  // about:srcdoc makes Cesium classify relative worker IDs as foreign URLs.
  // Load our fixed empty HTTP document first, then write only the trusted template.
  // No dataset values, remote HTML or caller URLs are written into this document.
  frame.src = `${assetRoot}domains/host.html`;
  frame.addEventListener('load', () => {
    if (scope.signal.aborted) return;
    const document = frame.contentDocument;
    if (!document?.querySelector('meta[name="dataseek-visualization-host"][content="1"]')) { options.onMessage('error', {}); return; }
    document.open(); document.write(source); document.close();
  }, { once: true });
  const handler = (event: MessageEvent) => {
    if (scope.signal.aborted || event.source !== frame.contentWindow || event.origin !== origin || event.data?.channel !== channel) return;
    options.onMessage(event.data.type, event.data.payload);
  };
  window.addEventListener('message', handler);
  scope.add(() => { window.removeEventListener('message', handler); frame.remove(); });
  target.append(frame);
  return { frame, post: (type: string, payload: unknown) => { if (!scope.signal.aborted && frame.isConnected) frame.contentWindow?.postMessage({ channel, type, payload }, origin); } };
}
