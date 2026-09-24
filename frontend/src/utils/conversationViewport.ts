/** Scroll ownership without changing DOM layout or mounting policy. */
export class ConversationViewport {
  private viewport?: HTMLElement;
  private content?: Element;
  private observer?: ResizeObserver;
  private anchor?: { node: HTMLElement; key: string; top: number };
  private frame?: number;
  private generation = 0;
  private observedTop?: number;

  private readonly following: () => boolean;
  constructor(following: () => boolean) { this.following = following; }

  bind(viewport: HTMLElement | null | undefined) {
    if (!viewport || typeof viewport.querySelector !== 'function') return;
    const content = viewport.querySelector('.analysis-conversation') ?? viewport;
    if (viewport === this.viewport && content === this.content) return;
    this.dispose();
    this.viewport = viewport; this.content = content;
    this.observedTop = viewport.scrollTop;
    for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) viewport.addEventListener(name, this.onIntent, { passive: true, capture: true });
    if (typeof ResizeObserver !== 'undefined') {
      const generation = this.generation;
      this.observer = new ResizeObserver(() => {
        if (generation !== this.generation) return;
        if (typeof requestAnimationFrame !== 'function') { this.layout(); return; }
        if (this.frame !== undefined) return;
        this.frame = requestAnimationFrame(() => {
          this.frame = undefined;
          if (generation === this.generation) this.layout();
        });
      });
      this.observer.observe(content);
      if (content !== viewport) this.observer.observe(viewport);
    }
  }

  /** Capture a stable rendered message before a historical page is prepended. */
  capture(viewport: HTMLElement | null | undefined): boolean {
    this.bind(viewport);
    this.anchor = undefined;
    if (!this.viewport) return false;
    const rows = this.viewport.querySelectorAll<HTMLElement>('[data-message-key]');
    const top = this.viewport.getBoundingClientRect().top;
    let low = 0, high = rows.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (rows[middle].getBoundingClientRect().bottom > top) high = middle;
      else low = middle + 1;
    }
    const node = rows[low];
    const key = node?.dataset.messageKey;
    if (!node || key === undefined) return false;
    this.anchor = { node, key, top: node.getBoundingClientRect().top - top };
    return true;
  }

  /** Subsequent image/layout growth honors the retained reading row, not just one tick. */
  layout() {
    const viewport = this.viewport;
    if (!viewport) return;
    if (this.following()) {
      this.anchor = undefined;
      viewport.scrollTop = viewport.scrollHeight;
      this.observedTop = viewport.scrollTop;
      return;
    }
    const anchor = this.anchor;
    if (!anchor) return;
    if (!viewport.contains(anchor.node)) {
      const node = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-key]'))
        .find(row => row.dataset.messageKey === anchor.key);
      if (!node) { this.anchor = undefined; return; }
      anchor.node = node;
    }
    const delta = anchor.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top - anchor.top;
    if (Math.abs(delta) > 0.5) {
      viewport.scrollTop += delta;
      this.observedTop = viewport.scrollTop;
    }
  }

  /** Programmatic writes and content shrink clamps are not reader intent. */
  readerMoved(viewport: HTMLElement | null | undefined): boolean {
    if (!viewport || viewport !== this.viewport || this.observedTop === undefined) return true;
    const floor = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const moved = Math.abs(viewport.scrollTop - Math.min(this.observedTop, floor)) > 0.5;
    this.observedTop = viewport.scrollTop;
    return moved;
  }

  private onIntent = (event: Event) => {
    if (event.type === 'keydown') {
      const key = (event as KeyboardEvent).key;
      if (!['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(key)) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest?.('input,textarea,[contenteditable="true"]')) return;
    }
    this.anchor = undefined;
  };

  dispose() {
    this.generation += 1;
    if (this.frame !== undefined && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.frame);
    this.frame = undefined;
    this.observer?.disconnect(); this.observer = undefined;
    for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) this.viewport?.removeEventListener(name, this.onIntent, true);
    this.viewport = undefined; this.content = undefined; this.anchor = undefined; this.observedTop = undefined;
  }
}
