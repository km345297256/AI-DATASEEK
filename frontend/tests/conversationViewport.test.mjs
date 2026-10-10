import assert from 'node:assert/strict';
import test from 'node:test';
import { ConversationViewport } from '../src/utils/conversationViewport.ts';

function world() {
  let top = 150, height = 1200;
  const content = { style: { paddingBottom: '' } };
  const reserved = () => Number.parseFloat(content.style.paddingBottom) || 0;
  const rows = Array.from({ length: 10 }, (_, index) => ({
    dataset: { messageKey: String(index) }, position: index * 100,
    getBoundingClientRect() { return { top: 100 + this.position - top, bottom: 200 + this.position - top }; },
  }));
  const viewport = new EventTarget();
  viewport.style = { overflowAnchor: 'auto' };
  Object.defineProperties(viewport, {
    scrollTop: { get: () => Math.min(top, height + reserved() - 200), set: value => { top = Math.max(0, Math.min(height + reserved() - 200, value)); } },
    scrollHeight: { get: () => height + reserved() }, clientHeight: { value: 200 },
  });
  viewport.querySelector = () => content;
  viewport.querySelectorAll = () => rows;
  viewport.getBoundingClientRect = () => ({ top: 100, bottom: 300 });
  viewport.contains = node => rows.includes(node);
  return { viewport, rows, content, resize(amount) { height += amount; }, grow(amount) { height += amount; for (const row of rows) row.position += amount; } };
}

test('paging retains the same message through prepend and delayed image/layout growth', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  assert.equal(state.capture(w.viewport), true);
  w.grow(300); state.layout(); assert.equal(w.viewport.scrollTop, 450);
  w.grow(120); state.layout(); assert.equal(w.viewport.scrollTop, 570);
  state.layout(); assert.equal(w.viewport.scrollTop, 570);
  state.dispose();
});

test('semantic row replacement retains position; disappearing row never selects unrelated content', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  state.capture(w.viewport);
  w.rows[1] = { ...w.rows[1] };
  w.grow(50); state.layout(); assert.equal(w.viewport.scrollTop, 200);
  w.rows.splice(1, 1); w.grow(50); state.layout(); assert.equal(w.viewport.scrollTop, 200);
  state.dispose();
});

for (const eventName of ['wheel', 'touchstart', 'pointerdown', 'keydown']) test(`${eventName} releases reader anchor without changing the transcript`, () => {
  const w = world(); const state = new ConversationViewport(() => false);
  state.capture(w.viewport);
  const event = new Event(eventName); if (eventName === 'keydown') Object.defineProperty(event, 'key', { value: 'PageDown' });
  w.viewport.dispatchEvent(event);
  w.grow(200); state.layout(); assert.equal(w.viewport.scrollTop, 150);
  state.dispose();
});

test('non-scrolling keys preserve the retained anchor', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  state.capture(w.viewport);
  const event = new Event('keydown'); Object.defineProperty(event, 'key', { value: 'Control' });
  w.viewport.dispatchEvent(event); w.grow(200); state.layout();
  assert.equal(w.viewport.scrollTop, 350);
  state.dispose();
});

test('paging compensation and follow writes are not reader movement', () => {
  const w = world(); let follow = false;
  const state = new ConversationViewport(() => follow);
  state.capture(w.viewport); w.grow(100); state.layout();
  assert.equal(state.readerMoved(w.viewport), false);
  w.viewport.scrollTop += 10;
  assert.equal(state.readerMoved(w.viewport), true);
  follow = true; state.layout();
  assert.equal(state.readerMoved(w.viewport), false);
  state.dispose();
});

test('late resize/animation callbacks cannot move the next session or revive disposed ownership', () => {
  const prior = { ResizeObserver: globalThis.ResizeObserver, requestAnimationFrame: globalThis.requestAnimationFrame, cancelAnimationFrame: globalThis.cancelAnimationFrame };
  const observers = [], frames = new Map(); let count = 0;
  globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() { this.disconnected = true; } };
  globalThis.requestAnimationFrame = callback => { frames.set(++count, callback); return count; };
  globalThis.cancelAnimationFrame = id => frames.delete(id);
  try {
    const first = world(), second = world(); let follow = false;
    const state = new ConversationViewport(() => follow);
    state.capture(first.viewport); first.grow(100); observers[0].callback();
    const staleFrame = [...frames.values()][0];
    state.bind(second.viewport);
    assert.equal(observers[0].disconnected, true);
    staleFrame(); observers[0].callback();
    assert.equal(first.viewport.scrollTop, 150); assert.equal(second.viewport.scrollTop, 150);
    follow = true; second.grow(100); observers[1].callback();
    for (const callback of frames.values()) callback(); frames.clear();
    assert.equal(second.viewport.scrollTop, second.viewport.scrollHeight - 200);
    state.dispose(); second.grow(100); observers[1].callback();
    assert.equal(frames.size, 0);
  } finally {
    for (const [key, value] of Object.entries(prior)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; }
  }
});

function foldingTest(name, body) {
  test(name, () => {
    const original = globalThis.getComputedStyle;
    globalThis.getComputedStyle = element => element.style;
    try { body(); } finally {
      if (original === undefined) delete globalThis.getComputedStyle;
      else globalThis.getComputedStyle = original;
    }
  });
}

foldingTest('expanding retains the clicked heading despite follow and delayed content above it', () => {
  const w = world(); const state = new ConversationViewport(() => true);
  const settle = state.beginFold(w.viewport, 2);
  w.resize(700); settle();
  assert.equal(w.viewport.scrollTop, 150);
  assert.equal(w.content.style.paddingBottom, '');
  w.grow(120); state.layout();
  assert.equal(w.viewport.scrollTop, 270);
  assert.equal(state.readerMoved(w.viewport), false);
  state.followTail();
  assert.equal(w.viewport.scrollTop, w.viewport.scrollHeight - 200);
  assert.equal(w.viewport.style.overflowAnchor, 'auto');
  state.dispose();
});

foldingTest('collapse retains only needed bottom room, repeated pointer toggle and new input do not revive it', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  w.viewport.scrollTop = 900;
  const settle = state.beginFold(w.viewport, 9);
  w.resize(-200); settle();
  assert.equal(w.viewport.scrollTop, 900);
  assert.equal(w.content.style.paddingBottom, '100px');
  w.viewport.dispatchEvent(new Event('pointerdown'));
  assert.equal(w.viewport.scrollTop, 900);
  assert.equal(w.content.style.paddingBottom, '100px');
  const reopen = state.beginFold(w.viewport, 9);
  w.resize(200); reopen();
  assert.equal(w.viewport.scrollTop, 900);
  assert.equal(w.content.style.paddingBottom, '');
  const stale = state.beginFold(w.viewport, 9);
  state.followTail(); stale();
  assert.equal(w.viewport.scrollTop, 1000);
  assert.equal(w.content.style.paddingBottom, '');
  state.dispose();
});

foldingTest('reader movement reclaims only safe space and cannot resurrect the old heading', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  w.viewport.scrollTop = 900;
  const settle = state.beginFold(w.viewport, 9); w.resize(-200); settle();
  w.viewport.dispatchEvent(new Event('wheel'));
  assert.equal(w.viewport.scrollTop, 900, 'intent alone must not clamp the reader');
  w.viewport.scrollTop = 850; assert.equal(state.readerMoved(w.viewport), true);
  assert.equal(w.content.style.paddingBottom, '50px');
  w.viewport.scrollTop = 700; assert.equal(state.readerMoved(w.viewport), true);
  assert.equal(w.content.style.paddingBottom, '');
  w.grow(100); state.layout(); assert.equal(w.viewport.scrollTop, 700);
  state.dispose();
});

foldingTest('session replacement cancels pending folds and restores owned inline styles', () => {
  const first = world(), second = world(); const state = new ConversationViewport(() => false);
  first.content.style.paddingBottom = '12px';
  const settle = state.beginFold(first.viewport, 2);
  state.bind(second.viewport); settle();
  assert.equal(first.content.style.paddingBottom, '12px');
  assert.equal(first.viewport.style.overflowAnchor, 'auto');
  assert.equal(second.viewport.scrollTop, 150);
  assert.equal(second.content.style.paddingBottom, '');
  state.dispose();
});

foldingTest('fold reservation preserves existing padding through settle and release', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  w.content.style.paddingBottom = '12px';
  w.viewport.scrollTop = 900;
  const settle = state.beginFold(w.viewport, 9); w.resize(-200); settle();
  assert.equal(w.viewport.scrollTop, 900);
  assert.equal(w.content.style.paddingBottom, '100px', '88px reservation plus the original 12px');
  state.followTail();
  assert.equal(w.content.style.paddingBottom, '12px');
  assert.equal(w.viewport.scrollTop, 812);
  state.dispose();
});

foldingTest('two rapid toggles give only the latest settle callback reading ownership', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  const stale = state.beginFold(w.viewport, 2);
  const current = state.beginFold(w.viewport, 3);
  w.grow(100);
  stale(); assert.equal(w.viewport.scrollTop, 150);
  current(); assert.equal(w.viewport.scrollTop, 250);
  assert.equal(w.content.style.paddingBottom, '');
  stale(); assert.equal(w.viewport.scrollTop, 250);
  state.dispose();
});

foldingTest('collapse below one viewport still reserves enough room for a positive reading offset', () => {
  const w = world(); const state = new ConversationViewport(() => false);
  w.viewport.scrollTop = 80;
  const settle = state.beginFold(w.viewport, 1); w.resize(-1050); settle();
  assert.equal(w.viewport.scrollTop, 80);
  assert.equal(w.content.style.paddingBottom, '130px', '50px viewport deficit plus the 80px reading offset');
  w.viewport.scrollTop = 0; state.readerMoved(w.viewport);
  assert.equal(w.content.style.paddingBottom, '', 'At the top no retained space is needed');
  state.dispose();
});
