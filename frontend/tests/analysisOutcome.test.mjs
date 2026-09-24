import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import {
  readAnalysisOutcome, analysisOutcomeReason, analysisOutcomeMissing, analysisOutcomeTitle,
  resumableAnalysisOutcome, continuationAttempt,
} from '../src/utils/analysisOutcome.ts';
import { projectHistoryMessages } from '../src/utils/sessionHistory.ts';

const token = '0123456789abcdef0123456789abcdef';
const nextToken = 'abcdef0123456789abcdef0123456789';
const outcome = (extra = {}) => ({
  status: 'partial', reason_code: 'artifacts_missing',
  missing: [{ kind: 'image', min_count: 1, label: '图表' }],
  can_resume: true, resume_from: token, ...extra,
});
const assistant = (value = outcome()) => ({ type: 'assistant', content: { timestamp: 1, content: '部分完成', metadata: { analysis_outcome: value } } });
const user = () => ({ type: 'user', content: { timestamp: 1, content: '另一个目标' } });
const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('only explicit server continuation metadata enables resume', () => {
  assert.equal(readAnalysisOutcome(outcome()).can_resume, true);
  assert.equal(readAnalysisOutcome(outcome({ can_resume: false })).can_resume, false);
  assert.equal(readAnalysisOutcome(outcome({ status: 'succeeded' })).can_resume, false);
  assert.equal(resumableAnalysisOutcome([{ type: 'assistant', content: { content: '请点击继续未完成部分' } }], 0), undefined);
});

test('execution budget stops retain the concrete reason without promising a rerun', () => {
  for (const [reason_code, expected] of [
    ['analysis_budget_deadline_exceeded', '最长执行时间'],
    ['analysis_budget_store_unavailable', '无法可靠记录执行额度'],
    ['budget_no_progress_loop', '未产生新的有效进展'],
    ['invalid_execution_result', '未返回可验证的执行结果'],
    ['tool_protocol_error', '未能发起有效的工具调用'],
    ['report_validation_rejected', '部分正文未通过证据核验'],
    ['report_validation_unavailable', '正文尚未完成证据核验'],
    ['scientific_validation_rejected', '计算方法或数值一致性未通过核验'],
    ['scientific_validation_unavailable', '计算方法与数值一致性尚未完整核验'],
  ]) {
    const parsed = readAnalysisOutcome(outcome({ reason_code, can_resume: false }));
    const text = analysisOutcomeReason(parsed);
    assert.ok(text.includes(expected));
    assert.doesNotMatch(text, /请重发|自动重跑|已完成。/);
    assert.equal(parsed.can_resume, false);
  }
});

test('invalid or nonopaque checkpoint identifiers never enable resume', () => {
  for (const resume_from of [undefined, null, '', '/private/data/file', '../checkpoint', 'x'.repeat(32), token + 'a', token.slice(1)]) {
    const parsed = readAnalysisOutcome(outcome({ resume_from }));
    assert.equal(parsed.can_resume, false);
    assert.equal(parsed.resume_from, undefined);
  }
});

test('preparation failures explain that analysis has not started without implying source corruption or retry', () => {
  for (const code of ['dataset_unreadable', 'dataset_unsafe', 'dataset_changed', 'dataset_limit', 'dataset_preparation_failed',
    'input_preparation_failed', 'model_audit_unavailable', 'transport_timeout', 'transport_error', 'invalid_response',
    'invalid_safety', 'invalid_routing', 'invalid_decision', 'front_controller_unavailable']) {
    const parsed = readAnalysisOutcome(outcome({ status: 'failed', reason_code: code, can_resume: false, missing: [] }));
    assert.match(analysisOutcomeReason(parsed), /尚未开始/);
    assert.doesNotMatch(analysisOutcomeReason(parsed), /自动重|已完成|数据已损坏|源文件已改变/);
    assert.equal(resumableAnalysisOutcome([assistant(parsed)], 0), undefined);
  }
});

test('rejected evidence and missing requested sections remain failures, not an unavailable generic explanation', () => {
  for (const [code, fragment] of [['answer_validation_rejected', /引用或证据未通过检查/], ['answer_objectives_missing', /遗漏了您明确要求/]]) {
    const parsed = readAnalysisOutcome(outcome({ status: 'failed', reason_code: code, can_resume: false, missing: [] }));
    assert.match(analysisOutcomeReason(parsed), fragment);
    assert.equal(analysisOutcomeTitle(parsed), '未完成');
    assert.doesNotMatch(analysisOutcomeReason(parsed, true), /仍可使用|本次分析已完成/);
  }
});

test('malformed completion metadata is ignored without inventing availability', () => {
  for (const value of [null, 'partial', {}, outcome({ status: 'running' }), outcome({ can_resume: 'true' }),
    outcome({ missing: null }), outcome({ missing: [{ kind: 'image', label: '图', min_count: 0 }] }),
    outcome({ missing: [{ kind: 'image', label: '图', min_count: 1000 }] })]) {
    assert.equal(readAnalysisOutcome(value), undefined);
  }
});

test('outcome notices use controlled labels and never display arbitrary diagnostics or paths', () => {
  const unsafe = readAnalysisOutcome(outcome({ reason_code: '/private/key-secret', missing: [{ kind: '/private/data', min_count: 2, label: '/Users/private/report' }] }));
  assert.equal(analysisOutcomeReason(unsafe), '本次分析尚未完成，具体原因暂未确认。');
  assert.deepEqual(analysisOutcomeMissing(unsafe), ['成果 × 2']);
  assert.equal(analysisOutcomeReason(readAnalysisOutcome(outcome({ reason_code: 'delivery_failed' }))), '部分成果文件尚未成功交付。');
  for (const key of ['__proto__', 'constructor', 'toString']) {
    const inherited = readAnalysisOutcome(outcome({ reason_code: key, missing: [{ kind: key, min_count: 1, label: key }] }));
    assert.equal(typeof analysisOutcomeReason(inherited), 'string');
    assert.deepEqual(analysisOutcomeMissing(inherited), ['成果 × 1']);
  }
});

test('missing deliverable labels show format constraints without changing the typed outcome', () => {
  const raw = outcome({ missing: [
    { kind: 'report', min_count: 1, label: '报告', formats: ['md'] },
    { kind: 'table', min_count: 2, label: '数据表', formats: ['.CSV', 'csv', 'TSV'] },
    { kind: 'code', min_count: 1, label: '代码' },
  ] });
  const before = structuredClone(raw);
  const parsed = readAnalysisOutcome(raw);
  assert.deepEqual(analysisOutcomeMissing(parsed), ['报告（MD） × 1', '数据表（CSV / TSV） × 2', '代码 × 1']);
  assert.equal(analysisOutcomeTitle(parsed), '部分完成');
  assert.deepEqual(raw, before);
  assert.deepEqual(parsed.missing, raw.missing);
});

test('missing format labels reject arbitrary metadata and preserve the generic label', () => {
  for (const formats of [undefined, null, [], 'md', {}, [null], [1], ['unknownformat'],
    ['md', '/private/secret'], ['<script>'], ['md\nsecret'], [' md '], ['..md'], Array(9).fill('md')]) {
    const parsed = readAnalysisOutcome(outcome({ missing: [{ kind: 'report', min_count: 1, label: '报告', formats }] }));
    assert.deepEqual(analysisOutcomeMissing(parsed), ['报告 × 1']);
  }
});

test('only the latest assistant in the latest user turn can be continued', () => {
  const old = assistant();
  assert.equal(resumableAnalysisOutcome([old], 0).resume_from, token);
  assert.equal(resumableAnalysisOutcome([old, user()], 0), undefined);
  assert.equal(resumableAnalysisOutcome([old, assistant(outcome({ resume_from: nextToken }))], 0), undefined);
  assert.equal(resumableAnalysisOutcome([old, { type: 'task-summary', content: {} }, { type: 'attachments', content: {} }], 0).resume_from, token);
});

test('a safety rejection cannot be converted to a continuation button', () => {
  const message = assistant();
  message.content.metadata.safety_review = { decision: 'reject' };
  assert.equal(resumableAnalysisOutcome([message], 0), undefined);
});

test('the same logical continuation retries with the same ID, but a different checkpoint/session gets a new one', () => {
  let ids = 0;
  const createId = () => `id-${++ids}`;
  const original = continuationAttempt(null, 'session-a', token, createId);
  assert.equal(continuationAttempt(original, 'session-a', token, createId), original);
  assert.equal(ids, 1);
  assert.notEqual(continuationAttempt(original, 'session-a', nextToken, createId).clientMessageId, original.clientMessageId);
  assert.notEqual(continuationAttempt(original, 'session-b', token, createId).clientMessageId, original.clientMessageId);
  assert.throws(() => continuationAttempt(null, 'session-a', '/private/token', createId));
});

test('history projection preserves outcome metadata exactly like live message events', () => {
  const metadata = { analysis_outcome: outcome() };
  const messages = projectHistoryMessages([{ event: 'message', data: { seq: 1, timestamp: 1, role: 'assistant', content: '部分完成', metadata } }]);
  assert.deepEqual(messages[0].content.metadata, metadata);
  assert.equal(resumableAnalysisOutcome(messages, 0).resume_from, token);
});

function chatHandlers() {
  const requests = [];
  let ids = 0;
  const session = useAnalysisSession({ api: {
    createClientMessageId: () => `id-${++ids}`,
    chatWithSession: async (...request) => { requests.push(request); return () => {}; },
  } });
  session.sessionId.value = 'session-a';
  session.messages.value = [assistant()];
  return { ...session, requests };
}

test('shared continuation blocks double-click and retries ambiguous failure with the same identity', async () => {
  const harness = chatHandlers();
  const pending = harness.resumeAnalysis(0);
  await harness.resumeAnalysis(0);
  await pending;
  assert.equal(harness.requests.length, 1);
  assert.equal(harness.requests[0][1], '');
  assert.deepEqual(harness.requests[0].slice(4, 8), [[], [], [], null]);
  assert.equal(harness.requests[0][11], token);
  assert.equal(harness.requests[0][12], undefined);
  harness.requests[0][8].onError(new Error('ambiguous'));
  await harness.resumeAnalysis(0);
  assert.equal(harness.requests.length, 2);
  assert.equal(harness.requests[0][10], harness.requests[1][10]);
});

test('shared continuation rejects disposed, restoring, running and connected views', async () => {
  for (const kind of ['disposed', 'restoring', 'running', 'connected']) {
    const harness = chatHandlers();
    if (kind === 'disposed') harness.dispose();
    if (kind === 'restoring') harness.isRestoringHistory.value = true;
    if (kind === 'running') harness.isLoading.value = true;
    if (kind === 'connected') { await harness.resumeAnalysis(0); harness.isLoading.value = false; }
    const count = harness.requests.length;
    await harness.resumeAnalysis(0);
    assert.equal(harness.requests.length, count);
  }
});

test('shared views render status but cannot emit a continuation from the UI', () => {
  const component = source('../src/components/ChatMessage.vue');
  assert.match(component, /:allow-resume="allowAnalysisResume && !isShare && !safetyReview"/);
  assert.match(component, /readAnalysisOutcome\(messageContent\.value\.metadata\?\.analysis_outcome\)/);
  const shared = source('../src/pages/SharePage.vue');
  assert.match(shared, /:is-share="true"/);
  assert.doesNotMatch(shared, /resumeAnalysis|allow-analysis-resume/);
});

test('both entries use the checkpoint-only shared continuation handler', () => {
  for (const page of ['ChatPage', 'DatasetSeekPage']) {
    assert.match(source(`../src/pages/${page}.vue`), /canResumeAnalysis, resumeAnalysis/);
  }
  const controller = source('../src/composables/useAnalysisSession.ts');
  const handler = controller.slice(controller.indexOf('async function resumeAnalysis('), controller.indexOf('function reset()'));
  assert.match(handler, /connect\(revision, undefined, attempt\)/);
  assert.doesNotMatch(handler, /createSession\(|messages\.value\.push/);
});

test('API treats continuation as a new idempotent command without current UI capability overrides', () => {
  const api = source('../src/api/agent.ts');
  assert.match(api, /effectiveClientMessageId = message \|\| resumeFrom/);
  assert.match(api, /\.\.\.\(resumeFrom \? \{ resume_from: resumeFrom \} : \{/);
  assert.match(api, /client_message_id: effectiveClientMessageId/);
  assert.doesNotMatch(source('../src/utils/analysisOutcome.ts'), /localStorage|sessionStorage|URLSearchParams/);
});

test('unknown failure notice does not invent preserved files or promise automatic reexecution', () => {
  const parsed = readAnalysisOutcome(outcome({ status: 'failed', reason_code: 'unrecognized_future_code', missing: [], can_resume: false }));
  assert.equal(analysisOutcomeTitle(parsed), '未完成');
  assert.equal(analysisOutcomeReason(parsed), '本次分析尚未完成，具体原因暂未确认。');
  assert.doesNotMatch(analysisOutcomeReason(parsed), /已生成|已保留|自动重|重新执行/);
});

test('only confirmed successful outcomes display completed and contradictory missing items remain visible', () => {
  const complete = readAnalysisOutcome(outcome({ status: 'succeeded', reason_code: 'completed', missing: [], can_resume: false }));
  assert.equal(analysisOutcomeTitle(complete), '已完成');
  assert.equal(analysisOutcomeReason(complete), '本次分析已完成。');
  const contradictory = readAnalysisOutcome(outcome({ status: 'succeeded', reason_code: 'completed' }));
  assert.equal(analysisOutcomeTitle(contradictory), '部分完成');
  assert.equal(analysisOutcomeReason(contradictory), '本次分析仍有待完成项。');
  assert.deepEqual(analysisOutcomeMissing(contradictory), ['图表 × 1']);
  assert.equal(contradictory.can_resume, false);
});

test('in-progress assistant messages stay nonterminal and never display successful outcome or old resume action', () => {
  const progress = {
    type: 'assistant',
    content: { timestamp: 2, content: '正在核验执行状态…', metadata: { analysis_progress: { stage: 'verifying_execution' } } },
  };
  assert.equal(readAnalysisOutcome(progress.content.metadata.analysis_outcome), undefined);
  assert.equal(readAnalysisOutcome(outcome({ status: 'running' })), undefined);
  assert.equal(resumableAnalysisOutcome([assistant(), progress], 0), undefined);
  assert.equal(resumableAnalysisOutcome([assistant(), progress], 1), undefined);
  const projected = projectHistoryMessages([{ event: 'message', data: { role: 'assistant', ...progress.content } }]);
  assert.deepEqual(projected, []);
});
