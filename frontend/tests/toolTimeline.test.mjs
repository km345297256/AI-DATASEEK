import assert from 'node:assert/strict';
import test from 'node:test';
import { buildToolTimeline, toolOperationState } from '../src/utils/toolTimeline.ts';

const write = (id, extra = {}) => ({ tool_call_id: id, name: 'file', function: 'file_write',
  args: { file: '/home/ubuntu/output/example.py', content: id },
  status: 'called', timestamp: 1, content: { success: true }, ...extra });

test('groups same-file edits across reads and runs without hiding those operations', () => {
  const events = [write('one'), write('read', { function: 'file_read' }),
    write('run', { name: 'shell', function: 'program_run' }),
    write('two', { function: 'file_str_replace', content: { success: false } }),
    write('three', { args: { file: '/home/ubuntu/output/example.py', append: true } })];
  const before = JSON.stringify(events);
  const items = buildToolTimeline(events);
  assert.equal(items.length, 3);
  assert.equal(items[0].key, 'one');
  assert.equal(items[0].count, 3);
  assert.match(items[0].summary, /1 次失败/);
  assert.deepEqual(items[0].revisions.map((item) => item.tool_call_id), ['one', 'two', 'three']);
  assert.equal(items[1].tool.function, 'file_read');
  assert.equal(items[2].tool.function, 'program_run');
  assert.equal(JSON.stringify(events), before);
});

test('deduplicates status snapshots and never regresses a finished call', () => {
  const items = buildToolTimeline([write('one', { status: 'calling', content: null }),
    write('one'), write('one', { content: null }), write('one', { status: 'calling' })]);
  assert.equal(items.length, 1);
  assert.equal(items[0].count, 1);
  assert.equal(items[0].tool.status, 'called');
  assert.equal(items[0].tool.content.success, true);
});

test('different exact paths, approvals and presentation cards remain separate', () => {
  const items = buildToolTimeline([write('one'), write('other', { args: { file: '/another/example.py' } }),
    write('approval', { tool_approval: { status: 'pending' } }),
    write('presentation', { presentation: { kind: 'table' } })]);
  assert.equal(items.length, 4);
  assert.ok(items.every((item) => item.count === 1));
});

test('terminal status without an explicit result is not displayed as success', () => {
  assert.equal(toolOperationState(write('one', { content: null })), '已结束');
  assert.equal(toolOperationState(write('one', { content: { result: { success: false } } })), '失败');
  assert.equal(toolOperationState(write('one', { status: 'calling' })), '进行中');
  assert.equal(toolOperationState(write('one', { execution_status: 'failed', content: { content: 'old script' } })), '失败');
});
