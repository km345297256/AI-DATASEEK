import assert from 'node:assert/strict';
import test from 'node:test';
import { analysisJobElapsedSeconds, findAnalysisTool, isAnalysisJobTerminal, mergeAnalysisToolEvent, newerAnalysisJob } from '../src/utils/analysisJob.ts';

const job = (overrides = {}) => ({
  schema_version: 1, job_id: 'a'.repeat(32), revision: 1, status: 'queued',
  tool_name: 'shell_run', created_at: '2026-09-05T00:00:00Z', cancellable: true,
  ...overrides,
});
const tool = (overrides = {}) => ({
  tool_call_id: 'call-1', name: 'shell', function: 'shell_run', args: {},
  timestamp: 1, status: 'calling', analysis_job: job(), ...overrides,
});

test('late SSE and poll responses cannot regress job revision or terminal state', () => {
  const done = job({ revision: 4, status: 'succeeded' });
  assert.equal(newerAnalysisJob(done, job({ revision: 2, status: 'running' })), done);
  assert.equal(newerAnalysisJob(done, job({ revision: 5, status: 'running' })), done);
  assert.equal(newerAnalysisJob(done, null), done);
  assert.equal(newerAnalysisJob(job(), job({ revision: 2, status: 'running' })).status, 'running');
});

test('lifecycle events preserve a delivered result and cannot reopen a called tool', () => {
  const current = tool({ status: 'called', content: { result: 'report' }, analysis_job: job({ status: 'succeeded', revision: 4 }) });
  const merged = mergeAnalysisToolEvent(current, tool({ content: null }));
  assert.equal(merged.status, 'called');
  assert.deepEqual(merged.content, { result: 'report' });
  assert.equal(merged.analysis_job.status, 'succeeded');
});

test('late updates locate their original tool in earlier steps', () => {
  const first = tool();
  const messages = [
    { type: 'step', content: { tools: [first] } },
    { type: 'tool', content: tool({ tool_call_id: 'call-2' }) },
  ];
  assert.equal(findAnalysisTool(messages, 'call-1'), first);
  assert.equal(findAnalysisTool(messages, 'absent'), undefined);
});

test('elapsed time freezes at completion and interrupted is terminal', () => {
  const done = job({ finished_at: '2026-09-05T00:00:12Z', status: 'interrupted' });
  assert.equal(analysisJobElapsedSeconds(done, Date.now()), 12);
  assert.equal(isAnalysisJobTerminal(done), true);
  assert.equal(isAnalysisJobTerminal(job({ status: 'cancelling' })), false);
  assert.equal(analysisJobElapsedSeconds(job({ created_at: 'invalid' })), 0);
});
