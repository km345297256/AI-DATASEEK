/** Reading ownership with temporary fold room, without changing message mounting policy. */
export class ConversationViewport {
  private viewport?: HTMLElement;
  private content?: Element;
  private observer?: ResizeObserver;
  private anchor?: { node: HTMLElement; key: string; top: number };
  private frame?: number;
  private generation = 0;
  private observedTop?: number;
  private foldAnchor = false;
  private foldRevision = 0;
  private reserved = 0;
  private padding?: { element: HTMLElement; original: string; base: number };
  private nativeAnchor?: string;

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
    this.releaseFold();
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

  /** Called synchronously before Vue removes/adds step rows; settle after its DOM commit. */
  beginFold(viewport: HTMLElement | null | undefined, key: string | number): (() => void) | undefined {
    this.bind(viewport);
    if (!this.viewport) return;
    const node = Array.from(this.viewport.querySelectorAll<HTMLElement>('[data-message-key]'))
      .find(row => row.dataset.messageKey === String(key));
    if (!node) return;
    this.anchor = { node, key: String(key), top: node.getBoundingClientRect().top - this.viewport.getBoundingClientRect().top };
    this.foldAnchor = true;
    const revision = ++this.foldRevision, generation = this.generation;
    // Disable native anchoring only while we own the fold's reading position.
    if (this.nativeAnchor === undefined) this.nativeAnchor = this.viewport.style.overflowAnchor;
    this.viewport.style.overflowAnchor = 'none';
    // Protect against the browser's synchronous shrink clamp before the next tick.
    // A visible heading needs at most one viewport of temporary room below it.
    this.setReserved(this.reserved + this.viewport.clientHeight + Math.max(0, -this.anchor.top));
    return () => {
      if (generation === this.generation && revision === this.foldRevision && this.foldAnchor) this.layout();
    };
  }

  /** Explicit new input/return-to-bottom relinquishes all old reading ownership. */
  followTail() {
    this.releaseFold();
    this.anchor = undefined;
    this.setReserved(0);
    if (this.viewport) {
      this.viewport.scrollTop = this.viewport.scrollHeight;
      this.observedTop = this.viewport.scrollTop;
    }
  }

  private setReserved(height: number) {
    if (height > 0 && !this.padding) {
      const element = this.content as HTMLElement | undefined;
      if (!element?.style) return;
      this.padding = { element, original: element.style.paddingBottom,
        base: Number.parseFloat(getComputedStyle(element).paddingBottom) || 0 };
    }
    if (!this.padding) return;
    this.reserved = Math.max(0, Math.ceil(height));
    this.padding.element.style.paddingBottom = this.reserved ? `${this.padding.base + this.reserved}px` : this.padding.original;
    if (!this.reserved) this.padding = undefined;
  }

  private reclaimReserved(top = this.viewport?.scrollTop ?? 0) {
    const viewport = this.viewport;
    if (!viewport || !this.reserved) return;
    const naturalFloor = viewport.scrollHeight - this.reserved - viewport.clientHeight;
    this.setReserved(top <= 0 ? 0 : Math.min(this.reserved, Math.max(0, top - naturalFloor)));
  }

  private releaseFold() {
    this.foldRevision += 1;
    if (this.foldAnchor) this.anchor = undefined;
    this.foldAnchor = false;
    if (this.viewport && this.nativeAnchor !== undefined) this.viewport.style.overflowAnchor = this.nativeAnchor;
    this.nativeAnchor = undefined;
    // A reader gesture may reclaim only space that cannot clamp their current position.
    this.reclaimReserved();
  }

  /** Subsequent image/layout growth honors the retained reading row, not just one tick. */
  layout() {
    const viewport = this.viewport;
    if (!viewport) return;
    if (this.following() && !this.foldAnchor) {
      this.followTail();
      return;
    }
    const anchor = this.anchor;
    if (!anchor) { this.reclaimReserved(); return; }
    if (!viewport.contains(anchor.node)) {
      const node = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-key]'))
        .find(row => row.dataset.messageKey === anchor.key);
      if (!node) { this.anchor = undefined; this.releaseFold(); return; }
      anchor.node = node;
    }
    const delta = anchor.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top - anchor.top;
    if (this.foldAnchor) {
      const target = Math.max(0, viewport.scrollTop + delta);
      const naturalFloor = viewport.scrollHeight - this.reserved - viewport.clientHeight;
      this.setReserved(target <= 0 ? 0 : Math.max(0, target - naturalFloor));
      viewport.scrollTop = target;
      this.observedTop = viewport.scrollTop;
      return;
    }
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
    if (moved) this.releaseFold();
    return moved;
  }

  private onIntent = (event: Event) => {
    if (event.type === 'keydown') {
      const key = (event as KeyboardEvent).key;
      if (!['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(key)) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest?.('input,textarea,[contenteditable="true"]')) return;
    }
    // A second toggle's pointerdown must not drop its existing reservation before click.
    if (event.type === 'pointerdown' && this.foldAnchor) return;
    this.releaseFold();
    this.anchor = undefined;
  };

  dispose() {
    this.generation += 1;
    this.releaseFold();
    this.setReserved(0);
    if (this.frame !== undefined && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.frame);
    this.frame = undefined;
    this.observer?.disconnect(); this.observer = undefined;
    for (const name of ['wheel', 'touchstart', 'pointerdown', 'keydown']) this.viewport?.removeEventListener(name, this.onIntent, true);
    this.viewport = undefined; this.content = undefined; this.anchor = undefined; this.observedTop = undefined;
  }
}
