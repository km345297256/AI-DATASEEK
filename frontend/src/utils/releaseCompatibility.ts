/** Bump only for incompatible browser/API contracts, not for ordinary releases.
 * The server can accept several client protocols during a compatible rollout.
 */
export const CLIENT_PROTOCOL = 1;
const BUILD_ID = /^[a-f0-9]{64}$/;
const MAX_MANIFEST_BYTES = 4096;
const CHECK_INTERVAL_MS = 30_000;

export type ReleaseNotice = 'update' | 'incompatible' | 'asset_error';
export const RELEASE_MESSAGES: Record<ReleaseNotice, string> = {
  update: '系统已有新版本。当前任务可继续，请先保存输入，待操作完成后手动刷新页面。',
  incompatible: '服务协议已更新，当前页面可能无法继续使用部分功能。请先保存输入，再手动刷新页面；刷新不会重新提交分析任务。',
  asset_error: '页面资源加载失败，可能与网络或版本更新有关。请先保存输入，再手动刷新页面；系统不会自动刷新或重新提交任务。',
};

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const protocol = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;

export function parseRelease(value: unknown) {
  if (!record(value) || value.schema_version !== 1 || typeof value.build_id !== 'string'
    || !BUILD_ID.test(value.build_id) || !protocol(value.client_protocol)) return undefined;
  return { buildId: value.build_id, clientProtocol: value.client_protocol };
}

export function parseClientContract(value: unknown): number[] | undefined {
  if (!record(value) || value.schema_version !== 1 || !Array.isArray(value.supported_client_protocols)
    || !value.supported_client_protocols.length || value.supported_client_protocols.length > 16
    || !value.supported_client_protocols.every(protocol)) return undefined;
  return [...new Set(value.supported_client_protocols)].sort((a, b) => a - b);
}

/** A missing legacy endpoint, HTML error page or oversized response is unknown,
 * never evidence of an incompatible release. Error bodies are never displayed.
 */
async function readManifest(fetcher: typeof fetch, url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetcher(url, { signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error' });
  if (!response.ok || !response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    await response.body?.cancel();
    return undefined;
  }
  const size = response.headers.get('content-length');
  if (size && (!/^\d+$/.test(size) || Number(size) > MAX_MANIFEST_BYTES)) {
    await response.body?.cancel();
    return undefined;
  }
  const reader = response.body?.getReader();
  if (!reader) return undefined;
  try {
    let bytes = 0;
    const chunks: Uint8Array[] = [];
    while (true) {
      const part = await reader.read();
      if (signal.aborted) return undefined;
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > MAX_MANIFEST_BYTES) return undefined;
      chunks.push(part.value);
    }
    const buffer = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer));
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

/** Advisory only. Existing SSE/preview validators remain authoritative.
 * It never reloads, retries a mutation, cancels work or writes browser storage.
 */
export function createReleaseMonitor(options: {
  bootBuildId?: string | null;
  releaseUrl: string;
  contractUrl: string;
  notify: (notice: ReleaseNotice) => void;
  fetcher?: typeof fetch;
  now?: () => number;
}) {
  const fetcher = options.fetcher ?? fetch;
  const now = options.now ?? Date.now;
  const boot = options.bootBuildId && BUILD_ID.test(options.bootBuildId) ? options.bootBuildId : undefined;
  let disposed = false;
  let lastCheck = -Infinity;
  let pending: Promise<void> | undefined;
  let request: AbortController | undefined;
  let lastNotice: string | undefined;
  let assetError = false;
  let release: ReturnType<typeof parseRelease>;
  let supported: number[] | undefined;

  function announce() {
    if (disposed) return;
    const kind: ReleaseNotice | undefined = assetError ? 'asset_error'
      : (supported && !supported.includes(CLIENT_PROTOCOL)) ? 'incompatible'
      : boot && release && release.buildId !== boot ? 'update' : undefined;
    // The currently served frontend's protocol does not determine whether the
    // backend still supports this tab. Compatible old tabs keep working.
    const key = kind === 'asset_error' ? kind
      : kind && `${kind}:${release?.buildId ?? ''}:${supported?.join(',') ?? ''}`;
    if (key !== lastNotice) {
      lastNotice = key;
      if (kind) options.notify(kind);
    }
  }

  return {
    check(): Promise<void> {
      if (disposed) return Promise.resolve();
      if (pending) return pending;
      if (now() - lastCheck < CHECK_INTERVAL_MS) return Promise.resolve();
      lastCheck = now();
      const controller = new AbortController();
      request = controller;
      // Only the small diagnostic reads have a timeout, never analysis work.
      const timer = setTimeout(() => controller.abort(), 5000);
      pending = (async () => {
        const [asset, api] = await Promise.allSettled([
          readManifest(fetcher, options.releaseUrl, controller.signal),
          readManifest(fetcher, options.contractUrl, controller.signal),
        ]);
        if (disposed || controller.signal.aborted) return;
        if (asset.status === 'fulfilled') release = parseRelease(asset.value) ?? release;
        if (api.status === 'fulfilled') supported = parseClientContract(api.value) ?? supported;
        announce();
      })().finally(() => { clearTimeout(timer); pending = undefined; request = undefined; });
      return pending;
    },
    assetFailed() { assetError = true; announce(); },
    dispose() { disposed = true; request?.abort(); },
  };
}

export function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/.test(error.message);
}
