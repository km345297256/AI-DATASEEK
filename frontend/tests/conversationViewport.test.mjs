import assert from 'node:assert/strict';
import test from 'node:test';
import { ConversationViewport } from '../src/utils/conversationViewport.ts';

function world() {
  let top = 150, height = 1200;
  const content = {};
  const rows = Array.from({ length: 10 }, (_, index) => ({
    dataset: { messageKey: String(index) }, position: index * 100,
    getBoundingClientRect() { return { top: 100 + this.position - top, bottom: 200 + this.position - top }; },
  }));
  const viewport = new EventTarget();
  Object.defineProperties(viewport, {
    scrollTop: { get: () => top, set: value => { top = Math.max(0, Math.min(height - 200, value)); } },
    scrollHeight: { get: () => height }, clientHeight: { value: 200 },
  });
  viewport.querySelector = () => content;
  viewport.querySelectorAll = () => rows;
  viewport.getBoundingClientRect = () => ({ top: 100, bottom: 300 });
  viewport.contains = node => rows.includes(node);
  return { viewport, rows, grow(amount) { height += amount; for (const row of rows) row.position += amount; } };
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
