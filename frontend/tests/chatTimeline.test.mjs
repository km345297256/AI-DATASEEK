import assert from 'node:assert/strict';
import test from 'node:test';

import {
  failRunningSteps,
  findCurrentTurnRunningStep,
  findCurrentTurnStep,
  insertTaskExecutionSummary,
} from '../src/utils/chatTimeline.ts';

const message = (type, content) => ({
  type,
  content: {
    timestamp: 1,
    ...content,
  },
});

test('reused step IDs stay scoped to the latest user turn', () => {
  const oldStep = { id: '2', description: 'old', status: 'running', tools: [], timestamp: 1 };
  const newStep = { id: '2', description: 'new', status: 'running', tools: [], timestamp: 2 };
  const messages = [
    message('user', { content: 'first request' }),
    message('step', oldStep),
    message('assistant', { content: 'Task error: Connection error.' }),
    message('user', { content: 'continue' }),
    message('assistant', { content: 'continuing' }),
    message('step', newStep),
  ];

  assert.equal(findCurrentTurnStep(messages, '2'), messages[5].content);
  assert.equal(findCurrentTurnRunningStep(messages), messages[5].content);
  assert.equal(findCurrentTurnStep(messages, '2').description, 'new');
});

test('starting a new turn can close stale running steps from older turns', () => {
  const oldStep = { id: '2', description: 'old', status: 'running', tools: [], timestamp: 1 };
  const messages = [
    message('user', { content: 'first request' }),
    message('step', oldStep),
    message('assistant', { content: 'Task error: Connection error.' }),
  ];

  const failed = failRunningSteps(messages, false);

  assert.equal(failed.length, 1);
  assert.equal(failed[0], messages[1].content);
  assert.equal(messages[1].content.status, 'failed');
});

test('an error only fails running steps in the current turn', () => {
  const oldStep = { id: '2', description: 'old', status: 'running', tools: [], timestamp: 1 };
  const newStep = { id: '2', description: 'new', status: 'running', tools: [], timestamp: 2 };
  const messages = [
    message('user', { content: 'first request' }),
    message('step', oldStep),
    message('user', { content: 'continue' }),
    message('step', newStep),
  ];

  const failed = failRunningSteps(messages);

  assert.equal(failed.length, 1);
  assert.equal(failed[0], messages[3].content);
  assert.equal(messages[1].content.status, 'running');
  assert.equal(messages[3].content.status, 'failed');
});

test('task summary stores only the rounded elapsed milliseconds', () => {
  const messages = [
    message('user', { content: 'analyze', timestamp: 10 }),
    message('assistant', { content: 'done', timestamp: 12 }),
  ];

  const summary = insertTaskExecutionSummary(messages, 12, 1234.6);

  assert.deepEqual(summary, { timestamp: 12, duration_ms: 1235 });
  assert.equal(messages.at(-1).type, 'task-summary');
  assert.deepEqual(Object.keys(messages.at(-1).content).sort(), ['duration_ms', 'timestamp']);
});

test('replayed task summary falls back to event timestamps in milliseconds', () => {
  const messages = [message('user', { content: 'analyze', timestamp: 10 })];

  const summary = insertTaskExecutionSummary(messages, 13);

  assert.equal(summary.duration_ms, 3000);
});
