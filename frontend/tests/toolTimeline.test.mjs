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

test('host-confirmed pre-dispatch rejection is not a program runtime failure or a success', () => {
  const blocked = write('prepare', { name: 'shell', function: 'program_run',
    execution_status: 'not_started', content: { success: false } });
  assert.equal(toolOperationState(blocked), '未执行');
  const timeline = buildToolTimeline([blocked, { ...blocked, execution_status: null, content: null }]);
  assert.equal(timeline.length, 1);
  assert.equal(toolOperationState(timeline[0].tool), '未执行');
  assert.equal(timeline[0].recoveredByCallId, undefined);
  // Untrusted tool output must not downgrade a runtime failure.
  assert.equal(toolOperationState(write('raw', { execution_status: 'failed',
    content: { success: false, side_effect_state: 'not_started' } })), '失败');
});

test('same-file request totals distinguish completed writes from not-started phase transitions', () => {
  const saved = write('saved');
  const redirected = write('redirected', { execution_status: 'not_started',
    content: { success: true, write_action: 'finalize_saved', side_effect_state: 'not_started' } });
  const failed = write('failed', { execution_status: 'failed', content: { success: false } });
  const events = [saved, redirected, failed,
    // Reconnection snapshots of the skipped proposal are not extra writes.
    { ...redirected, execution_status: null, content: null }];
  const items = buildToolTimeline(events);
  assert.equal(items.length, 1);
  assert.equal(items[0].count, 3);
  assert.equal(items[0].summary, '3 次修改请求 · 1 次完成 · 1 次未执行 · 1 次失败');
  assert.deepEqual(items[0].revisions.map(toolOperationState), ['已完成', '未执行', '失败']);
  assert.equal(items[0].revisions[1].content.write_action, 'finalize_saved');
});

test('ten report proposals with one skipped call are not labelled ten completed modifications', () => {
  const events = Array.from({ length: 10 }, (_, index) => write(`report-${index}`, {
    args: { file: '/home/ubuntu/output/synthetic-report.md', content: `version ${index}` },
    ...(index === 8 ? { execution_status: 'not_started', content: { success: false } } : {}),
  }));
  const [item] = buildToolTimeline(events);
  assert.equal(item.count, 10);
  assert.equal(item.summary, '10 次修改请求 · 9 次完成 · 1 次未执行');
  assert.equal(item.revisions.length, 10);
  assert.equal(toolOperationState(item.revisions[8]), '未执行');
});
