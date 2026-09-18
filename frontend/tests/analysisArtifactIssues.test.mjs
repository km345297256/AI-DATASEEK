import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as presentation from '../src/utils/analysisOutcome.ts';
import { projectHistoryMessages } from '../src/utils/sessionHistory.ts';
import { useAnalysisProgress } from '../src/composables/useAnalysisProgress.ts';

const token = '1234567890abcdef1234567890abcdef';
const outcome = (extra = {}) => ({ status: 'succeeded', reason_code: 'completed', missing: [], can_resume: false, ...extra });
const issue = (extra = {}) => ({ artifact_name: '附加说明.json', kind: 'report', reason_code: 'invalid_json_syntax', blocking: false, ...extra });
const read = (extra = {}) => presentation.readAnalysisOutcome(outcome(extra));

// Render the real notice template: interpolation, success/warning treatment and
// resume visibility are verified, not a hand-written substitute component.
const componentSource = readFileSync(new URL('../src/components/AnalysisOutcomeNotice.vue', import.meta.url), 'utf8');
const script = compileScript(parse(componentSource).descriptor, { id: 'artifact-issues-test', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === '../utils/analysisOutcome') return presentation;
  assert.fail(`Unexpected notice dependency: ${name}`);
}, module, module.exports);
const renderNotice = (value, allowResume = false) => renderToString(vue.createSSRApp(module.exports.default, { outcome: value, allowResume }));

test('history without issue metadata remains compatible and is never rewritten', () => {
  const value = outcome();
  const snapshot = JSON.stringify(value);
  const parsed = presentation.readAnalysisOutcome(value);
  assert.deepEqual(parsed.issues, []);
  assert.equal(presentation.analysisOutcomeTitle(parsed), '已完成');
  assert.equal(JSON.stringify(value), snapshot);
  for (const issues of [null, false, 'legacy data', {}]) {
    assert.deepEqual(read({ issues }).issues, []);
  }
});

test('all supported issue reasons have controlled Chinese labels', () => {
  const reasons = Object.keys(presentation.ARTIFACT_ISSUE_REASON_LABELS);
  assert.equal(reasons.length, 34);
  for (const reason_code of reasons) {
    const parsed = read({ issues: [issue({ reason_code })] });
    assert.equal(parsed.issues[0].reason_code, reason_code);
    const [view] = presentation.analysisOutcomeIssues(parsed);
    assert.equal(view.reason, presentation.ARTIFACT_ISSUE_REASON_LABELS[reason_code]);
    assert.match(view.reason, /[\u4e00-\u9fff]/);
    assert.ok(!view.reason.includes(reason_code));
  }
});

test('file diagnostics support every deliverable type and arbitrary safe basenames', () => {
  const entries = [
    issue({ artifact_name: '图表-α (第 2 版).svg', kind: 'image', reason_code: 'invalid_content' }),
    issue({ artifact_name: '測定値 2026.csv', kind: 'table', reason_code: 'inconsistent_table_width' }),
    issue({ artifact_name: '补充说明任意名称.md', kind: 'report', reason_code: 'empty_file' }),
    issue({ artifact_name: 'simulate-量子.R', kind: 'code', reason_code: 'invalid_code_syntax' }),
    issue({ artifact_name: 'artifact_without_extension', kind: 'any', reason_code: 'delivery_failed' }),
  ];
  const parsed = read({ issues: entries });
  assert.deepEqual(parsed.issues, entries);
  assert.deepEqual(presentation.analysisOutcomeIssues(parsed).map((entry) => entry.kind_label), ['图表', '数据表', '报告', '代码', '结果文件']);
});

test('unsafe paths, controls and malformed issue fields are filtered without leaking original values', () => {
  const unsafeNames = ['/Users/private/answer.csv', 'C:\\private\\answer.csv', '../answer.csv', 'folder/answer.csv',
    '.', '..', ' .. ', '', ' ', 'secret\nname.csv', 'secret\u0000.csv', 'secret\u007f.csv', 'secret\u0085.csv',
    'secret\u202e.csv', 'secret\u2066.csv', '文'.repeat(161), '😀'.repeat(161)];
  const malformed = unsafeNames.map((artifact_name) => issue({ artifact_name }));
  malformed.push(null, [], 'invalid', Object.create(issue()), issue({ kind: '__proto__' }), issue({ kind: '/Users/private' }),
    issue({ reason_code: 'constructor' }), issue({ reason_code: '/Users/private/traceback' }),
    issue({ reason_code: '<img onerror="attack">' }), issue({ blocking: 'true' }), issue({ blocking: 1 }), issue({ artifact_name: 42 }));
  const value = read({ status: 'partial', reason_code: 'artifacts_missing', issues: malformed,
    missing: [{ kind: 'image', min_count: 2, label: 'ignored' }], can_resume: true, resume_from: token });
  assert.deepEqual(value.issues, []);
  assert.deepEqual(presentation.analysisOutcomeMissing(value), ['图表 × 2']);
  assert.equal(value.can_resume, true);
  assert.equal(value.resume_from, token);
});

test('diagnostics are bounded, copied, and exact duplicates are removed', () => {
  const source = issue({ diagnostics: 'private parser details', file_path: '/Users/private/data', extra: 'not public' });
  const parsed = read({ issues: [source, source] });
  assert.deepEqual(parsed.issues, [issue()]);
  assert.notEqual(parsed.issues[0], source);
  source.artifact_name = 'changed.json';
  assert.equal(parsed.issues[0].artifact_name, '附加说明.json');
  const many = Array.from({ length: 1000 }, (_, index) => issue({ artifact_name: `${index}.json` }));
  assert.equal(read({ issues: many }).issues.length, 64);
  assert.equal(read({ issues: [issue({ artifact_name: '文'.repeat(160) }), issue({ artifact_name: '😀'.repeat(160) })] }).issues.length, 2);
});

test('successful required delivery stays completed while auxiliary failures render separately', async () => {
  const value = read({ issues: [issue(), issue({ artifact_name: 'optional.py', kind: 'code', reason_code: 'invalid_code_syntax' })] });
  assert.equal(presentation.analysisOutcomeIsComplete(value), true);
  assert.equal(presentation.analysisOutcomeTitle(value), '已完成');
  assert.equal(presentation.analysisOutcomeReason(value), '本次分析已完成。');
  const html = await renderNotice(value, true);
  assert.match(html, /已完成/);
  assert.match(html, /附加文件问题/);
  assert.match(html, /以下文件未计入本次交付/);
  assert.doesNotMatch(html, /不影响已完成的所需成果/);
  assert.match(html, /附加说明.json/);
  assert.match(html, /JSON 语法不合法/);
  assert.match(html, /optional.py/);
  assert.match(html, /代码文件存在语法错误/);
  assert.doesNotMatch(html, /待完成|继续未完成部分|影响所需成果/);
});

test('required failures retain missing quantities and the existing explicit resume contract', async () => {
  const value = read({ status: 'partial', reason_code: 'artifacts_missing',
    missing: [{ kind: 'image', min_count: 2, label: '图表' }], can_resume: true, resume_from: token,
    issues: [issue({ artifact_name: 'chart.svg', kind: 'image', blocking: true, reason_code: 'invalid_content' })] });
  const html = await renderNotice(value, true);
  assert.match(html, /部分完成/);
  assert.match(html, /待完成：图表 × 2/);
  assert.match(html, /文件交付问题/);
  assert.match(html, /chart.svg/);
  assert.match(html, /影响所需成果/);
  assert.match(html, /继续未完成部分/);
  assert.doesNotMatch(await renderNotice(value, false), /继续未完成部分/);
  assert.doesNotMatch(await renderNotice({ ...value, can_resume: false }, true), /继续未完成部分/);
});

test('contradictory succeeded metadata cannot hide a blocking file problem', async () => {
  const value = read({ issues: [issue({ blocking: true })], can_resume: true, resume_from: token });
  assert.equal(presentation.analysisOutcomeIsComplete(value), false);
  assert.equal(presentation.analysisOutcomeTitle(value), '部分完成');
  assert.equal(presentation.analysisOutcomeReason(value), '本次分析仍有待完成项。');
  assert.equal(value.can_resume, false);
  const html = await renderNotice(value, true);
  assert.doesNotMatch(html, /本次分析已完成|不影响已完成|继续未完成部分/);
});

test('real Vue rendering escapes filename markup and never renders unsafe diagnostics or links', async () => {
  const safeMarkupName = '<img onerror="attack">.png';
  const value = outcome({ issues: [issue({ artifact_name: safeMarkupName }),
    issue({ artifact_name: '/Users/private/secret.json' }), issue({ reason_code: 'private traceback' })] });
  const html = await renderNotice(value);
  assert.match(html, /&lt;img onerror=&quot;attack&quot;&gt;.png/);
  assert.doesNotMatch(html, /<img|<a |href=|\/Users\/private|private traceback/);
});

test('real analysis text is not replaced by status notices, in history or in the ChatMessage template', () => {
  const value = outcome({ issues: [issue()] });
  const events = [{ event: 'message', data: { seq: 1, timestamp: 1, role: 'assistant',
    content: '实际执行模型的分析结论与证据。', metadata: { analysis_outcome: value } } }];
  const messages = projectHistoryMessages(events);
  assert.equal(messages[0].content.content, '实际执行模型的分析结论与证据。');
  assert.deepEqual(messages[0].content.metadata.analysis_outcome.issues, [issue()]);
  const source = readFileSync(new URL('../src/components/ChatMessage.vue', import.meta.url), 'utf8');
  assert.match(source, /v-html="renderMarkdown\(visibleAssistantContent\)"/);
  assert.ok(source.indexOf('renderMarkdown(visibleAssistantContent)') < source.indexOf('<AnalysisOutcomeNotice'));
  assert.match(source, /const visibleAssistantContent = computed\(\(\) => stripHiddenDatasetResultNotices\(messageContent.value.content\)\)/);
});

test('file issues do not turn terminal outcomes into transient progress or reopen finished status', () => {
  const state = useAnalysisProgress();
  const progress = { event: 'message', data: { role: 'assistant', timestamp: 1, content: 'working', metadata: { analysis_progress: { stage: 'verifying_execution' } } } };
  state.updateAnalysisProgress(progress);
  const final = { event: 'message', data: { role: 'assistant', timestamp: 2, content: '真实结果', metadata: { analysis_outcome: outcome({ issues: [issue()] }) } } };
  state.updateAnalysisProgress(final);
  state.updateAnalysisProgress(progress);
  assert.equal(state.analysisProgress.value, '');
  assert.equal(projectHistoryMessages([progress, final]).length, 1);
});
