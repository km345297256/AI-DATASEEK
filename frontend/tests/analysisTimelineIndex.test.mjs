import assert from 'node:assert/strict';
import test from 'node:test';
import { AnalysisTimelineIndex } from '../src/utils/analysisTimelineIndex.ts';
import { ConversationPresentationIndex, conversationEntries, precedingTaskSummary } from '../src/utils/analysisPresentation.ts';
import { findAnalysisTool } from '../src/utils/analysisJob.ts';
import { findCurrentTurnStep, findCurrentTurnRunningStep } from '../src/utils/chatTimeline.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

const user = value => ({ type: 'user', content: { content: String(value), timestamp: 1 } });
const summary = () => ({ type: 'task-summary', content: { timestamp: 1, duration_ms: 1, has_steps: true } });
const tool = id => ({ tool_call_id: id, name: 'analysis', function: 'execute', args: {}, status: 'calling', timestamp: 1 });
const step = (id, tools = []) => ({ type: 'step', content: { id, status: 'running', tools, timestamp: 1 } });
const assistant = value => ({ type: 'assistant', content: { content: String(value), timestamp: 1 } });
const attachments = role => ({ type: 'attachments', content: { role, attachments: [], timestamp: 1 } });
function assertPresentation(index, messages) {
  const entries = index.update(messages);
  assert.deepEqual(entries.map(({ message, index }) => ({ message, index })), conversationEntries(messages));
  for (const entry of entries) if (entry.message.type === 'step') assert.equal(index.summaryFor(entry.message), precedingTaskSummary(messages, entry.index));
  assert.equal(index.latestAssistant, messages.findLast(message => message.type === 'assistant'));
}

test('incremental presentation matches existing order, summaries and identities across append/completion/prepend', () => {
  const index = new ConversationPresentationIndex();
  let messages = [attachments('user')];
  for (let turn = 0; turn < 30; turn++) {
    const offset = messages.length;
    for (const message of [user(turn), attachments('user'), step('same'), step('other'), assistant(turn), attachments('assistant')]) {
      messages.push(message); assertPresentation(index, messages);
    }
    const stable = index.update(messages)[0];
    messages.splice(offset + 1, 0, summary());
    assertPresentation(index, messages);
    assert.equal(index.update(messages)[0], stable);
    // Duplicate terminal updates replace a summary without changing length.
    messages.splice(offset + 1, 1, summary()); assertPresentation(index, messages);
  }
  const tail = index.update(messages).at(-1);
  messages = [user('older'), summary(), step('old'), assistant('older'), ...messages];
  assertPresentation(index, messages);
  assert.equal(index.update(messages).at(-1), tail);
  assertPresentation(index, []);
});

for (const size of [1000, 10000, 50000]) test(`${size} historical rows: tool miss/current turn lookup and append touch only the suffix`, () => {
  let reads = 0;
  const source = Array.from({ length: size }, (_, index) => index % 2 ? assistant(index) : user(index));
  const messages = new Proxy(source, { get(target, key, receiver) {
    if (typeof key === 'string' && /^\d+$/.test(key)) reads++;
    return Reflect.get(target, key, receiver);
  } });
  const index = new AnalysisTimelineIndex();
  index.sync(messages); reads = 0;
  assert.equal(index.tool(messages, 'new'), undefined);
  assert.equal(index.step(messages, 'new'), undefined);
  assert.equal(index.runningStep(messages), undefined);
  assert.ok(reads < 10, `historical lookup read ${reads} rows`);
  const current = step('new', [tool('call')]);
  source.push(current); reads = 0;
  assert.equal(index.tool(messages, 'call'), current.content.tools[0]);
  assert.equal(index.step(messages, 'new'), current.content);
  assert.ok(reads < 10, `append read ${reads} rows`);
  index.clear();
  assert.equal(index.tool([], 'call'), undefined);
});

for (const size of [1000, 10000, 50000]) test(`${size} steps: summary preparation is linear and unchanged turns keep entry identity`, () => {
  let reads = 0;
  const firstSummary = summary();
  const source = [user('old'), firstSummary, ...Array.from({ length: size }, (_, i) => step(String(i))), assistant('answer')];
  const messages = new Proxy(source, { get(target, key, receiver) {
    if (typeof key === 'string' && /^\d+$/.test(key)) reads++;
    return Reflect.get(target, key, receiver);
  } });
  const index = new ConversationPresentationIndex();
  const entries = index.update(messages);
  for (const entry of entries) if (entry.message.type === 'step') assert.equal(index.summaryFor(entry.message), firstSummary);
  assert.ok(reads < size * 3, `summary projection read ${reads} rows`);
  reads = 0;
  source.push(user('new'), step('new'));
  const appended = index.update(messages);
  assert.equal(appended[2], entries[2]);
  assert.ok(reads < 15, `new turn re-read ${reads} historical rows`);
  reads = 0;
  source.splice(size + 4, 0, summary());
  index.update(messages);
  assert.ok(reads < 20, `active-turn completion re-read ${reads} historical rows`);
});

test('lookup preserves old reverse-message/first-tool semantics and current-turn step isolation', () => {
  const index = new AnalysisTimelineIndex();
  const messages = [user('old'), step('same', [tool('dup')]), user('new'), step('same', [tool('dup'), tool('dup')]), step('last')];
  for (const id of ['dup', 'missing']) assert.equal(index.tool(messages, id), findAnalysisTool(messages, id));
  for (const id of ['same', 'last', 'missing']) assert.equal(index.step(messages, id), findCurrentTurnStep(messages, id));
  assert.equal(index.runningStep(messages), findCurrentTurnRunningStep(messages));
  messages.at(-1).content.status = 'completed';
  assert.equal(index.runningStep(messages), findCurrentTurnRunningStep(messages));
});

test('late lifecycle merges into historical owner and subsequent live events use that same reactive card', async () => {
  let resolve;
  const page = new Promise(yes => { resolve = yes; });
  const session = useAnalysisSession({ api: { getSessionHistory: () => page } });
  session.sessionId.value = 'session'; session.historyBeforeSeq.value = 10;
  session.handleEvent({ event: 'message', data: { role: 'user', content: 'current', timestamp: 10, seq: 10 } });
  session.handleEvent({ event: 'tool', data: { ...tool('call'), status: 'calling', seq: 11 } });
  const loading = session.loadEarlierHistory();
  resolve({ events: [
    { event: 'message', data: { role: 'user', content: 'old', timestamp: 1, seq: 1 } },
    { event: 'tool', data: { ...tool('call'), seq: 2 } },
  ], has_more: false, next_before_seq: null });
  await loading;
  const owner = session.messages.value.find(message => message.type === 'tool').content;
  session.handleEvent({ event: 'tool', data: { ...tool('call'), status: 'called', content: 'finished', seq: 12 } });
  assert.equal(session.messages.value.filter(message => message.type === 'tool').length, 1);
  assert.equal(owner.content, 'finished');
  assert.equal(owner.status, 'called');
  session.dispose();
});
