import assert from 'node:assert/strict';
import test from 'node:test';
import { recoveredProgramAttempts } from '../src/utils/programRecovery.ts';
import { buildToolTimeline } from '../src/utils/toolTimeline.ts';
import { mergeAnalysisToolEvent } from '../src/utils/analysisJob.ts';

const run = (id, code = 1, extra = {}) => ({
  tool_call_id: id, name: 'shell', function: 'program_run', timestamp: 1,
  args: { script_path: '/home/ubuntu/scripts/example.py' }, status: 'called',
  execution_status: code === 0 ? 'succeeded' : 'failed',
  program_attempt: { version: 1, identity: 'a'.repeat(64),
    returncode: code, state: code === 0 ? 'succeeded' : 'failed' },
  ...extra,
});

test('confirmed later success marks every earlier failed attempt without changing raw results', () => {
  const events = [run('first'), run('second'), run('fixed', 0), run('new-failure')];
  const before = JSON.stringify(events);
  assert.deepEqual([...recoveredProgramAttempts(events)], [['first', 'fixed'], ['second', 'fixed']]);
  const timeline = buildToolTimeline(events);
  assert.equal(timeline.length, 4);
  assert.equal(timeline[0].recoveredByCallId, 'fixed');
  assert.equal(timeline[0].panelTool.execution_status, 'failed');
  assert.equal(timeline[3].recoveredByCallId, undefined);
  assert.equal(JSON.stringify(events), before);
});

test('same basename, display path or raw success message cannot claim recovery', () => {
  const failed = run('failed');
  for (const successful of [
    run('other', 0, { program_attempt: { ...run('other', 0).program_attempt, identity: 'b'.repeat(64) } }),
    run('unattested', 0, { program_attempt: undefined, content: { success: true, returncode: 0 } }),
    run('plugin', 0, { name: 'plugin' }),
    run('not-program', 0, { function: 'shell_view' }),
  ]) assert.equal(recoveredProgramAttempts([failed, successful]).size, 0);
});

test('running, unknown, inconsistent and malformed receipts leave the failure unresolved', () => {
  const failed = run('failed');
  for (const changed of [
    { status: 'calling' }, { execution_status: null }, { execution_status: 'failed' },
    ...[null, { version: 1, identity: 'bad', returncode: 0, state: 'succeeded' },
      { version: 1, identity: 'a'.repeat(64), returncode: 1, state: 'succeeded' },
      { version: 1, identity: 'a'.repeat(64), returncode: '0', state: 'succeeded' },
      { version: 2, identity: 'a'.repeat(64), returncode: 0, state: 'succeeded' },
      { version: 1, identity: 'a'.repeat(64), returncode: null, state: 'running' },
    ].map(program_attempt => ({ program_attempt })),
  ]) assert.equal(recoveredProgramAttempts([failed, run('later', 0, changed)]).size, 0);
});

test('recovery never crosses steps/turns/sessions or forgives a later failure', () => {
  assert.equal(buildToolTimeline([run('failed')])[0].recoveredByCallId, undefined);
  assert.equal(buildToolTimeline([run('another-step-success', 0)])[0].recoveredByCallId, undefined);
  assert.equal(recoveredProgramAttempts([run('earlier-success', 0), run('failed')]).size, 0);
  assert.equal(recoveredProgramAttempts([run('failed', 1, { timestamp: 3 }), run('older-success', 0)]).size, 0);
  assert.equal(recoveredProgramAttempts([run('same'), run('same', 0)]).size, 0);
});

test('late lifecycle snapshots retain trusted attempt without inventing an extra operation', () => {
  const failed = run('failed');
  const fixed = run('fixed', 0);
  const later = { ...fixed, content: null, program_attempt: undefined };
  assert.deepEqual(mergeAnalysisToolEvent(fixed, later).program_attempt, fixed.program_attempt);
  const timeline = buildToolTimeline([failed, fixed, later]);
  assert.equal(timeline.length, 2);
  assert.equal(timeline[0].recoveredByCallId, 'fixed');
});
