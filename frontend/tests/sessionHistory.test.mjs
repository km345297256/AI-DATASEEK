import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { acceptAgentEvent, createAgentEventCursor } from '../src/utils/agentEventCursor.ts';
import { createMessageKey, prependHistoricalMessages, projectHistoryMessages } from '../src/utils/sessionHistory.ts';

const event = (seq, type, data) => ({ event: type, data: { seq, event_id: `${seq}-0`, version: 1, timestamp: seq, ...data } });

function turn(start, question, id) {
  return [
    event(start, 'message', { role: 'user', content: question, attachments: [{ file_id: `input-${id}` }] }),
    event(start + 1, 'step', { id: 'reused-step', status: 'running', description: 'Analyze' }),
    event(start + 2, 'tool', { tool_call_id: id, name: 'shell', function: 'shell_run', args: {}, status: 'calling' }),
    event(start + 3, 'tool', { tool_call_id: id, name: 'shell', function: 'shell_run', args: {}, status: 'called', content: { output: 'result' } }),
    event(start + 4, 'message', { role: 'assistant', content: 'Complete', attachments: [{ file_id: `result-${id}` }] }),
    event(start + 5, 'done', {}),
  ];
}

test('whole-turn paged replay matches eager history including tools, attachments and elapsed summaries', () => {
  const first = turn(1, 'First', 'tool-a');
  const second = turn(10, 'Second', 'tool-b');
  const third = turn(20, 'Third', 'tool-c');
  const eager = projectHistoryMessages([...first, ...second, ...third]);
  const latest = projectHistoryMessages(third);
  const latestObject = latest.find((message) => message.type === 'step');
  const merged = prependHistoricalMessages(projectHistoryMessages([...first, ...second]), latest);
  assert.deepEqual(merged, eager);
  assert.equal(merged.findLast((message) => message.type === 'step'), latestObject);
  assert.deepEqual(merged.filter((message) => message.type === 'step').map((message) => message.content.status), ['completed', 'completed', 'completed']);
});

test('late history completion preserves live events, tools, polling updates and the SSE cursor', async () => {
  let resolvePage;
  const page = new Promise((resolve) => { resolvePage = resolve; });
  const cursor = createAgentEventCursor();
  const latestEvents = turn(20, 'Current', 'live-tool');
  const current = projectHistoryMessages(latestEvents.slice(0, 3));
  latestEvents.slice(0, 3).forEach((item) => acceptAgentEvent(cursor, item));
  const running = current.find((message) => message.type === 'step');
  const tool = running.content.tools[0];
  const toolArray = running.content.tools;
  const key = createMessageKey();
  const runningKey = key(running);
  // A poll and an SSE result arrive while the older history request is pending.
  tool.analysis_job = { job_id: 'job', revision: 8, status: 'succeeded' };
  tool.content = { output: 'fresh result' };
  current.push({ type: 'assistant', content: { timestamp: 25, content: 'Fresh answer' } });
  acceptAgentEvent(cursor, event(25, 'message', { role: 'assistant', content: 'Fresh answer' }));
  resolvePage(turn(1, 'Older', 'old-tool'));
  const merged = prependHistoricalMessages(projectHistoryMessages(await page), current);
  assert.equal(merged.at(-1), current.at(-1));
  assert.equal(merged.findLast((message) => message.type === 'step'), running);
  assert.equal(running.content.tools, toolArray);
  assert.equal(running.content.tools[0], tool);
  assert.equal(key(running), runningKey);
  assert.equal(tool.analysis_job.revision, 8);
  assert.equal(tool.content.output, 'fresh result');
  assert.equal(cursor.lastSeq, 25);
});

test('late lifecycle-only events reconnect to the original older tool without duplicated cards', () => {
  const oldEvents = turn(1, 'Original', 'job-tool');
  const older = projectHistoryMessages(oldEvents);
  const recent = projectHistoryMessages([
    event(10, 'message', { role: 'user', content: 'Next' }),
    event(11, 'tool', { tool_call_id: 'job-tool', name: 'shell', function: 'shell_run', args: {}, status: 'called', analysis_job: { job_id: 'j1', revision: 4, status: 'succeeded' } }),
  ]);
  const merged = prependHistoricalMessages(older, recent);
  const tools = merged.flatMap((message) => message.type === 'step' ? message.content.tools : message.type === 'tool' ? [message.content] : []);
  assert.equal(tools.length, 1);
  assert.equal(tools[0].content.output, 'result');
  assert.equal(tools[0].analysis_job.revision, 4);
  assert.equal(merged.at(-1).content.content, 'Next');
});

test('history projection keeps legacy deduplication and dataset-only placeholder filtering', () => {
  const legacy = { event: 'message', data: { event_id: 'legacy-1', timestamp: 1, role: 'assistant', content: '正在分析指定数据集' } };
  assert.equal(projectHistoryMessages([legacy, legacy]).length, 1);
  assert.equal(projectHistoryMessages([legacy, legacy], true).length, 0);
});

test('older errors do not fail or otherwise mutate the current running turn', () => {
  const old = [
    event(1, 'message', { role: 'user', content: 'Old' }),
    event(2, 'step', { id: 'same', status: 'running', description: 'Old step' }),
    event(3, 'error', { error: 'Old failure' }),
  ];
  const current = projectHistoryMessages([
    event(10, 'message', { role: 'user', content: 'Current' }),
    event(11, 'step', { id: 'same', status: 'running', description: 'Current step' }),
  ]);
  const merged = prependHistoricalMessages(projectHistoryMessages(old), current);
  assert.deepEqual(merged.filter((message) => message.type === 'step').map((message) => message.content.status), ['failed', 'running']);
});

test('page integrations use abortable five-turn history while shared views keep the legacy endpoint', async () => {
  const agent = await readFile(new URL('../src/api/agent.ts', import.meta.url), 'utf8');
  assert.match(agent, /params: \{ turns: 5, before_seq: beforeSeq \}, signal/);
  for (const name of ['ChatPage', 'DatasetSeekPage']) {
    const source = await readFile(new URL(`../src/pages/${name}.vue`, import.meta.url), 'utf8');
    assert.match(source, /useAnalysisSession\(/);
    assert.match(source, /analysisSession\.restore\(/);
    assert.match(source, /:message-key="messageKey"/);
    assert.doesNotMatch(source, /watch\(messages,[\s\S]*?\{ deep: true \}/);
  }
  const controller = await readFile(new URL('../src/composables/useAnalysisSession.ts', import.meta.url), 'utf8');
  assert.match(controller, /getSessionHistory\(/);
  assert.match(controller, /historyRequest\?\.abort\(\)/);
  assert.match(controller, /request\.signal\.aborted/);
  assert.match(controller, /prependHistoricalMessages\(projectHistoryMessages\(page\.events/);
  const shared = await readFile(new URL('../src/pages/SharePage.vue', import.meta.url), 'utf8');
  assert.match(shared, /getSharedSession\(/);
  assert.doesNotMatch(shared, /getSessionHistory\(/);
});
