import { readBoundedBinary } from '../boundedBinary';

/** Only this source-controlled bundle may execute in the opaque reading frame. */
export async function loadDocxBootstrap(signal: AbortSignal): Promise<string> {
  // Fetch in the app origin: opaque iframes cannot request loopback assets under
  // Local Network Access. Do not weaken their origin or CSP to bypass this.
  const response = await fetch('/visualization-assets/docx/docx.js', {
    signal, credentials: 'omit', redirect: 'error',
  });
  if (!response.ok || response.redirected || !/^(?:application|text)\/(?:java|ecma)script(?:\s*;|$)/i.test(response.headers.get('content-type') ?? '')) {
    await response.body?.cancel();
    throw new Error('本地 DOCX 阅读组件不可用，请切换 Word 分页预览。');
  }
  const bytes = await readBoundedBinary(response, 1024 * 1024, signal);
  // HTML script raw text must not terminate early, even when a bundled string
  // contains a mixed-case closing tag. User file contents never enter srcdoc.
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes).replace(/<\/script/gi, '<\\/script');
}
