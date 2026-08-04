import assert from 'node:assert/strict';
import test from 'node:test';

import {
  failRunningSteps,
  findCurrentTurnRunningStep,
  findCurrentTurnStep,
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
