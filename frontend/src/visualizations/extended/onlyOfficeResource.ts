import type { VisualizationResult } from '../runtime';

/** Independent origin is a security boundary, not a configurable iframe URL. */
export function parseOfficeResource(result: VisualizationResult, pageUrl: string, now = Date.now()) {
  const value = result.payload;
  if (result.kind !== 'resources' || value.provider !== 'onlyoffice' || typeof value.frame_url !== 'string'
    || typeof value.lease !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(value.lease)
    || typeof value.expires_at !== 'number' || !Number.isSafeInteger(value.expires_at)
    || value.expires_at * 1000 <= now || value.expires_at * 1000 > now + 901_000) {
    throw new Error('办公阅读授权不符合插件协议。');
  }
  const host = new URL(pageUrl), frame = new URL(value.frame_url);
  if (!['localhost', '127.0.0.1', '[::1]'].includes(host.hostname)
    || frame.hostname !== 'office.localhost' || frame.origin === host.origin
    || !['http:', 'https:'].includes(frame.protocol) || frame.protocol !== host.protocol || frame.port !== host.port
    || frame.username || frame.password || frame.search || frame.hash
    || frame.pathname !== `/office-viewer/frame/${value.lease}`) {
    throw new Error('办公阅读器必须使用同端口的独立本机来源。');
  }
  return { frameUrl: frame.href, origin: frame.origin, lease: value.lease, expiresAt: value.expires_at };
}
