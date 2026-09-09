import { getCurrentScope, onScopeDispose } from 'vue';

export interface PreviewLoad {
  signal: AbortSignal;
  isCurrent: () => boolean;
  assertCurrent: () => void;
  onDispose: (cleanup: () => void) => void;
}

/** One load owns its requests and resources until replacement or unmount. */
export function usePreviewLoad() {
  let disposed = false;
  let current: AbortController | undefined;
  let cleanups: Array<() => void> = [];

  const cancel = () => {
    current?.abort();
    current = undefined;
    const pending = cleanups;
    cleanups = [];
    pending.forEach((cleanup) => cleanup());
  };
  const dispose = () => { disposed = true; cancel(); };
  if (getCurrentScope()) onScopeDispose(dispose);

  const begin = (): PreviewLoad => {
    cancel();
    const controller = new AbortController();
    current = controller;
    if (disposed) controller.abort();
    const isCurrent = () => !disposed && current === controller && !controller.signal.aborted;
    return {
      signal: controller.signal,
      isCurrent,
      assertCurrent: () => {
        if (!isCurrent()) throw new DOMException('Preview load superseded', 'AbortError');
      },
      onDispose: (cleanup) => {
        if (isCurrent()) cleanups.push(cleanup);
        else cleanup();
      },
    };
  };
  return { begin, dispose };
}
