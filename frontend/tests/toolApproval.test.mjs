import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { effectiveToolApprovalStatus, isToolApprovalExpired, isToolApprovalTerminal, newerToolApproval } from '../src/utils/toolApproval.ts';
import { mergeAnalysisToolEvent } from '../src/utils/analysisJob.ts';

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const approval = (overrides = {}) => ({
  schema_version: 1, approval_id: 'a'.repeat(32), revision: 1, status: 'pending',
  tool_name: 'remote_query', effects: ['network'], permissions: ['remote:read'],
  arguments_preview: { query: 'weather', token: '[REDACTED]' }, credential_refs: [],
  created_at: '2026-09-05T00:00:00Z', expires_at: '2026-09-05T00:05:00Z',
  ...overrides,
});

test('approval revisions do not regress across SSE and polling', () => {
  const approved = approval({ revision: 2, status: 'approved' });
  assert.equal(newerToolApproval(approved, approval()), approved);
  assert.equal(newerToolApproval(approved, approval({ revision: 2 })), approved);
  assert.equal(newerToolApproval(approved, null), approved);
  assert.equal(newerToolApproval(null, approved), approved);
  assert.equal(newerToolApproval(null, undefined), null);
  assert.equal(newerToolApproval(approved, approval({ revision: 3, status: 'consumed' })).status, 'consumed');
});

test('terminal approval cannot be reopened even by a higher revision', () => {
  for (const status of ['consumed', 'rejected', 'cancelled', 'expired']) {
    const final = approval({ status, revision: 3 });
    assert.equal(isToolApprovalTerminal(final), true);
    assert.equal(newerToolApproval(final, approval({ revision: 4 })), final);
  }
  assert.equal(isToolApprovalTerminal('approved'), false);
  const nextCall = approval({ approval_id: 'b'.repeat(32) });
  assert.equal(newerToolApproval(approval({ status: 'consumed' }), nextCall), nextCall);
});

test('expiry ends pending grants without relabeling already consumed calls', () => {
  const before = Date.parse('2026-09-05T00:04:59Z');
  const deadline = Date.parse('2026-09-05T00:05:00Z');
  assert.equal(isToolApprovalExpired(approval(), before), false);
  assert.equal(isToolApprovalExpired(approval(), deadline), true);
  assert.equal(effectiveToolApprovalStatus(approval(), before), 'pending');
  assert.equal(effectiveToolApprovalStatus(approval(), deadline), 'expired');
  assert.equal(effectiveToolApprovalStatus(approval({ status: 'approved' }), deadline), 'expired');
  assert.equal(effectiveToolApprovalStatus(approval({ status: 'consumed' }), deadline), 'consumed');
  assert.equal(isToolApprovalExpired(approval({ expires_at: 'invalid' }), deadline), false);
});

test('job lifecycle events retain the exact-call approval and delivered result', () => {
  const current = {
    tool_call_id: 'call-1', status: 'called', content: { result: 'done' },
    tool_approval: approval({ status: 'consumed', revision: 3 }),
    analysis_job: { job_id: 'job-1', status: 'succeeded', revision: 4 },
  };
  const merged = mergeAnalysisToolEvent(current, {
    tool_call_id: 'call-1', status: 'calling', content: null,
    tool_approval: approval(), analysis_job: { job_id: 'job-1', status: 'running', revision: 2 },
  });
  assert.equal(merged.tool_approval, current.tool_approval);
  assert.equal(merged.analysis_job, current.analysis_job);
  assert.equal(merged.status, 'called');
  assert.equal(merged.content, current.content);
  assert.equal(mergeAnalysisToolEvent(current, { status: 'called' }).tool_approval, current.tool_approval);
});

test('approval API scopes decisions to the session, call and expected revision', () => {
  const api = source('../src/api/toolApproval.ts');
  assert.match(api, /encodeURIComponent\(sessionId\)/);
  assert.match(api, /encodeURIComponent\(approvalId\)/);
  assert.match(api, /expected_revision: expectedRevision/);
  assert.match(api, /'X-Tool-Approval-Action': 'decide'/);
  assert.doesNotMatch(api, /allow_always|auto_approve|localStorage|sessionStorage/);
});

test('approval card is read-only when shared and disposes polling requests', () => {
  const card = source('../src/components/ToolApprovalCard.vue');
  assert.match(card, /!props\.isShare/);
  assert.match(card, /if \(!canDecide\.value \|\| !props\.sessionId\) return/);
  assert.match(card, /onBeforeUnmount\(/);
  assert.match(card, /decisionRequest\?\.abort\(\)/);
  assert.match(card, /document\.removeEventListener\('visibilitychange'/);
  assert.match(source('../src/pages/SharePage.vue'), /:is-share="true"/);
});

test('visibility refresh preserves the epoch of an in-flight approval decision', () => {
  const card = source('../src/components/ToolApprovalCard.vue');
  const restart = card.slice(card.indexOf('function restartPolling()'), card.indexOf('async function decide('));
  assert.match(restart, /if \(deciding\.value\) return/);
  assert.ok(restart.indexOf('if (deciding.value) return') < restart.indexOf('generation += 1'));
});

test('credential UI keeps secrets out of browser persistence and uses declared slots only', () => {
  const panel = source('../src/components/ToolCredentialsPanel.vue');
  assert.match(panel, /type="password"/);
  assert.match(panel, /autocomplete="new-password"/);
  assert.match(panel, /tool\.execution\?\.credentials/);
  assert.match(panel, /secret\.value = ''/);
  assert.doesNotMatch(panel, /localStorage|sessionStorage|console\.(log|error|debug)/);
  assert.match(source('../src/api/credential.ts'), /'X-Credential-Action': 'create'/);
  assert.match(source('../src/api/credential.ts'), /'X-Credential-Action': 'revoke'/);
  assert.match(source('../src/pages/PluginsPage.vue'), /ToolCredentialsPanel/);
});
