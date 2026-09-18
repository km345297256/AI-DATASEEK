import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as outcomes from '../src/utils/analysisOutcome.ts';
import { projectHistoryMessages } from '../src/utils/sessionHistory.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

const file = { file_id: 'published-file', filename: 'summary.png', file_url: '/files/published-file', upload_date: '' };
const outcome = (extra = {}) => ({ status: 'failed', reason_code: 'answer_validation_unavailable', missing: [], can_resume: false, ...extra });
const assistant = (attachments, extra = {}) => ({ type: 'assistant', content: {
  timestamp: 1, content: 'An explanation', attachments,
  metadata: { analysis_outcome: outcome() }, ...extra,
} });

test('a pure explanation failure refers only to this explanation, not imagined files or previous results', () => {
  const value = outcome();
  assert.equal(outcomes.analysisOutcomeTitle(value), '未完成');
  const text = outcomes.analysisOutcomeReason(value);
  assert.match(text, /本次结果说明尚未完成证据核验/);
  assert.match(text, /暂不能作为已确认结论/);
  assert.doesNotMatch(text, /文件|已交付|历史|之前|已完成/);
});

test('a response with published attachments can mention only this response delivery without promoting partial to success', () => {
  const value = outcome({ status: 'partial' });
  assert.equal(outcomes.analysisOutcomeTitle(value), '部分完成');
  assert.equal(outcomes.analysisOutcomeIsComplete(value), false);
  const text = outcomes.analysisOutcomeReason(value, outcomes.hasDeliveredAnalysisFiles(assistant([file])));
  assert.match(text, /本次结果说明尚未完成证据核验/);
  assert.match(text, /本次已交付的文件仍可使用/);
  assert.doesNotMatch(text, /本次分析已完成/);
});

test('only actual assistant attachments establish delivery, not filenames in prose, input files, or malformed entries', () => {
  assert.equal(outcomes.hasDeliveredAnalysisFiles(assistant([file])), true);
  for (const message of [
    assistant(undefined, { content: '已交付 summary.png，可下载 /files/published-file' }),
    assistant([]), assistant([null, {}, { filename: 'not-published.png' }, { file_id: 'missing-name' }]),
    assistant([{ ...file, file_id: ' ' }]), assistant([{ ...file, filename: ' ' }]),
    assistant({ files: [file] }),
    { type: 'user', content: { attachments: [file] } },
    { type: 'attachments', content: { role: 'user', attachments: [file] } },
    { type: 'tool', content: { attachments: [file] } },
  ]) assert.equal(outcomes.hasDeliveredAnalysisFiles(message), false);
});

test('live and history projections do not lend previous-turn files to a new explanation failure', () => {
  const events = [
    { event: 'message', data: { role: 'user', content: 'Make a chart', timestamp: 1, seq: 1 } },
    { event: 'message', data: { role: 'assistant', content: 'Chart result', timestamp: 2, seq: 2, attachments: [file],
      metadata: { analysis_outcome: outcome({ status: 'succeeded', reason_code: 'completed' }) } } },
    { event: 'message', data: { role: 'user', content: 'Explain that chart in detail', timestamp: 3, seq: 3, attachments: [file] } },
    { event: 'message', data: { role: 'assistant', content: 'Explanation unavailable', timestamp: 4, seq: 4,
      metadata: { analysis_outcome: outcome() } } },
  ];
  const session = useAnalysisSession({ api: {} });
  for (const event of events) session.handleEvent(event);
  for (const messages of [projectHistoryMessages(events), session.messages.value]) {
    const replies = messages.filter(message => message.type === 'assistant');
    assert.equal(outcomes.hasDeliveredAnalysisFiles(replies[0]), true);
    assert.equal(outcomes.hasDeliveredAnalysisFiles(replies[1]), false);
    assert.equal(outcomes.analysisOutcomeTitle(replies[0].content.metadata.analysis_outcome), '已完成');
    assert.equal(outcomes.analysisOutcomeTitle(replies[1].content.metadata.analysis_outcome), '未完成');
  }
  session.dispose();
});

test('interrupted execution also avoids claiming preserved files without a published attachment', () => {
  const value = outcome({ reason_code: 'execution_interrupted' });
  assert.equal(outcomes.analysisOutcomeReason(value), '本次分析执行已中断。');
  assert.match(outcomes.analysisOutcomeReason(value, true), /本次已交付的文件仍可使用/);
});

const source = readFileSync(new URL('../src/components/AnalysisOutcomeNotice.vue', import.meta.url), 'utf8');
const script = compileScript(parse(source).descriptor, { id: 'outcome-delivery-scope', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const componentModule = { exports: {} };
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === '../utils/analysisOutcome') return outcomes;
  assert.fail(`Unexpected notice dependency: ${name}`);
}, componentModule, componentModule.exports);
const renderNotice = (value, hasDeliveredFiles) => renderToString(vue.createSSRApp(componentModule.exports.default, { outcome: value, hasDeliveredFiles }));

test('actual notice renders no file reassurance for pure explanations, and remains failed or partial', async () => {
  for (const status of ['failed', 'partial']) {
    const html = await renderNotice(outcome({ status }));
    assert.match(html, /本次结果说明尚未完成证据核验/);
    assert.match(html, status === 'failed' ? /未完成/ : /部分完成/);
    assert.doesNotMatch(html, /文件|已交付|本次分析已完成|text-\[#247357\]/);
  }
  const delivered = await renderNotice(outcome({ status: 'partial' }), true);
  assert.match(delivered, /本次已交付的文件仍可使用/);
  assert.match(delivered, /部分完成/);
});

test('auxiliary file diagnostics remain visible without inventing successful file delivery', async () => {
  const value = outcome({ status: 'succeeded', reason_code: 'completed', issues: [{
    artifact_name: 'optional.png', kind: 'image', reason_code: 'missing_artifact', blocking: false,
  }] });
  const empty = await renderNotice(value);
  assert.match(empty, /附加文件问题/);
  assert.match(empty, /成果文件尚未生成/);
  assert.match(empty, /以下文件未计入本次交付/);
  assert.doesNotMatch(empty, /本次已交付文件以附件为准/);
  assert.match(await renderNotice(value, true), /本次已交付文件以附件为准/);
});

test('ChatMessage supplies same-message published attachments rather than conversation-wide files', () => {
  const chat = readFileSync(new URL('../src/components/ChatMessage.vue', import.meta.url), 'utf8');
  assert.match(chat, /:has-delivered-files="hasDeliveredAnalysisFiles\(message\)"/);
});
