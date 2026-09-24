import { fetchEventSource } from '@microsoft/fetch-event-source';
import type { FetchEventSourceInit } from '@microsoft/fetch-event-source';

export interface SSECallbacks<T = any> {
  onOpen?: () => void;
  onMessage?: (event: { event: string; data: T }) => void;
  onClose?: () => void;
  /** Called only when recovery is exhausted, or the response is not retryable. */
  onError?: (error: Error) => void;
  onRetry?: (state: { attempt: number; maxAttempts: number; delayMs: number }) => void;
}

export interface SSEOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  headers?: Record<string, string>;
  /** Supply a fresh body on each attempt; never replay a serialized stale cursor. */
  getBody?: () => unknown;
  /** Enable only for read-only requests or callers with a stable idempotency key. */
  retryOnError?: boolean;
  /** Chat must receive a terminal event; an earlier EOF is a transport loss. */
  isComplete?: () => boolean;
}

export class SSEConnectionError extends Error {
  readonly kind: 'transport' | 'http' | 'protocol';
  readonly status?: number;

  constructor(kind: 'transport' | 'http' | 'protocol', message: string, status?: number) {
    super(message);
    this.name = 'SSEConnectionError';
    this.kind = kind;
    this.status = status;
  }
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    signal.addEventListener('abort', finish, { once: true });
  });
}

interface SSEDependencies {
  connect?: (url: string, options: FetchEventSourceInit) => Promise<void>;
  wait?: typeof waitForRetry;
}

/** One owner for retries and cancellation, including while waiting to reconnect. */
export function startSSEConnection<T>(
  url: string,
  options: SSEOptions,
  callbacks: SSECallbacks<T>,
  dependencies: SSEDependencies = {},
): { cancel: () => void; finished: Promise<void> } {
  const controller = new AbortController();
  const connect = dependencies.connect ?? fetchEventSource;
  const wait = dependencies.wait ?? waitForRetry;
  const method = options.method ?? 'GET';
  const retryAllowed = options.retryOnError ?? method === 'GET';
  const maxAttempts = 6;
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...options.headers };
  for (const name of Object.keys(headers)) {
    if (['authorization', 'x-api-key'].includes(name.toLowerCase())) {
      delete headers[name];
    }
  }

  const finished = (async () => {
    let retries = 0;
    while (!controller.signal.aborted) {
      const attemptController = new AbortController();
      const cancelAttempt = () => attemptController.abort();
      controller.signal.addEventListener('abort', cancelAttempt, { once: true });
      try {
        const body = options.getBody ? options.getBody() : options.body;
        await connect(url, {
          method,
          headers,
          body: body == null ? undefined : JSON.stringify(body),
          signal: attemptController.signal,
          openWhenHidden: true,
          async onopen(response) {
            if (controller.signal.aborted) return;
            if (!response.ok) {
              // HTTP/application failures need an explicit decision, not a POST replay.
              throw new SSEConnectionError('http', `连接请求失败（HTTP ${response.status}）`, response.status);
            }
            if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')) {
              throw new SSEConnectionError('protocol', '服务器返回了非事件流响应，请刷新页面后重试。');
            }
            try {
              callbacks.onOpen?.();
            } catch {
              throw new SSEConnectionError('protocol', '无法初始化事件流，请刷新页面。');
            }
          },
          onmessage(event) {
            if (controller.signal.aborted || options.isComplete?.()) return;
            if (!event.event?.trim()) return; // SSE comments / heartbeat frames.
            try {
              callbacks.onMessage?.({ event: event.event, data: JSON.parse(event.data) as T });
            } catch (error) {
              if (error instanceof SSEConnectionError) throw error;
              throw new SSEConnectionError('protocol', '事件流数据无法解析，请刷新页面；后台任务状态尚未确认。');
            }
            if (options.isComplete?.()) attemptController.abort();
          },
          onerror(error) {
            // Disable the library's implicit retries: each retry needs a new body.
            throw error;
          },
        });
        if (controller.signal.aborted) return;
        if (options.isComplete && !options.isComplete()) {
          throw new SSEConnectionError('transport', '任务事件流提前断开。');
        }
        callbacks.onClose?.();
        return;
      } catch (cause) {
        if (controller.signal.aborted) return;
        const isTransport = cause instanceof TypeError
          || (cause instanceof SSEConnectionError && cause.kind === 'transport')
          || (cause instanceof DOMException && ['NetworkError', 'AbortError'].includes(cause.name));
        if (!isTransport || !retryAllowed || retries >= maxAttempts) {
          callbacks.onError?.(isTransport
            ? new SSEConnectionError('transport', '连接暂时中断，请刷新页面恢复；后台任务可能仍在运行。')
            : cause instanceof Error ? cause : new SSEConnectionError('protocol', '事件流连接失败，请刷新页面。'));
          return;
        }
        retries += 1;
        const delayMs = Math.min(500 * 2 ** (retries - 1), 8000);
        callbacks.onRetry?.({ attempt: retries, maxAttempts, delayMs });
        await wait(delayMs, controller.signal);
      } finally {
        controller.signal.removeEventListener('abort', cancelAttempt);
        attemptController.abort();
      }
    }
  })();
  return { cancel: () => controller.abort(), finished };
}
