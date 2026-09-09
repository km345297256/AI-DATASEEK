import { onBeforeUnmount } from 'vue';
export function useDomainScope() {
  const controller = new AbortController();
  const cleanups: (() => void)[] = [];
  let closed = false;
  const cleanup = () => {
    if (closed) return;
    closed = true; controller.abort();
    for (const dispose of cleanups.reverse()) { try { dispose(); } catch { /* Continue releasing sibling resources. */ } }
    cleanups.length = 0;
  };
  onBeforeUnmount(cleanup);
  return {
    signal: controller.signal,
    check() { controller.signal.throwIfAborted(); },
    add(dispose: () => void) { if (closed) dispose(); else cleanups.push(dispose); },
    blob(blob: Blob) { const url = URL.createObjectURL(blob); if (closed) { URL.revokeObjectURL(url); controller.signal.throwIfAborted(); } cleanups.push(() => URL.revokeObjectURL(url)); return url; },
    cleanup,
  };
}
export function displayError(error: unknown): string {
  if (error instanceof Error && !/(https?:|\/Users\/|\/home\/|token|secret)/i.test(error.message)) return error.message.slice(0, 260);
  return '可视化加载失败：格式不支持、资源超限或当前设备不支持 WebGL。';
}
