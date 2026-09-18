import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as presentation from '../src/utils/planPresentation.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import zh from '../src/locales/zh.ts';

const plan = (...statuses) => ({ timestamp: 1, steps: statuses.map((status, index) => ({
  timestamp: 1, id: `step-${index}`, description: `Step ${index}`, status,
})) });
const outcome = (status, extra = {}) => ({ status, reason_code: 'completed', missing: [], can_resume: false, ...extra });
const answer = (step_id, value) => ({ type: 'assistant', content: {
  content: 'This prose is not used as execution evidence', timestamp: 2,
  metadata: { step_id, analysis_outcome: value },
} });
const user = () => ({ type: 'user', content: { content: 'Another analysis', timestamp: 3 } });

test('all terminal does not mean all successful; progress counts only completed steps', () => {
  for (const [statuses, expected, count] of [
    [['completed'], 'completed', 1], [['failed'], 'failed', 0],
    [['completed', 'failed'], 'partial', 1], [['failed', 'failed'], 'failed', 0],
    [[], 'pending', 0],
  ]) {
    const result = presentation.presentPlan(plan(...statuses));
    assert.equal(result.status, expected);
    assert.equal(result.completed, count);
    assert.equal(result.total, statuses.length);
  }
});

test('running description wins over pending or past failed steps', () => {
  const result = presentation.presentPlan(plan('failed', 'pending', 'running'));
  assert.equal(result.status, 'running');
  assert.equal(result.activeDescription, 'Step 2');
  assert.equal(result.completed, 0);
  assert.equal(presentation.presentPlan(plan('pending')).status, 'pending');
});

test('structured partial outcome corrects the generic failed step without changing stored data', () => {
  const original = plan('failed');
  const messages = [answer('step-0', outcome('partial', { reason_code: 'answer_validation_unavailable' }))];
  const snapshot = JSON.stringify({ original, messages });
  const result = presentation.presentPlan(original, messages);
  assert.equal(result.status, 'partial');
  assert.equal(result.steps[0].displayStatus, 'partial');
  assert.equal(result.completed, 0);
  assert.equal(JSON.stringify({ original, messages }), snapshot);
});

test('only matching structured outcomes in the latest user turn affect the plan', () => {
  const succeeded = answer('step-0', outcome('succeeded'));
  for (const messages of [
    [succeeded, user()],
    [answer('unrelated-step', outcome('succeeded'))],
    [answer(undefined, outcome('succeeded'))],
    [{ ...succeeded, type: 'user' }],
    [{ type: 'assistant', content: { content: '任务已完成', timestamp: 1 } }],
    [answer('step-0', { status: 'succeeded' })],
  ]) assert.equal(presentation.presentPlan(plan('failed'), messages).status, 'failed');
  assert.equal(presentation.presentPlan(plan('failed'), [user(), succeeded]).status, 'completed');
});

test('later outcome for the same step wins and outcomes never bleed across step IDs', () => {
  const result = presentation.presentPlan(plan('failed', 'failed'), [
    answer('step-0', outcome('partial')),
    answer('step-0', outcome('succeeded')),
    answer('step-1', outcome('failed')),
  ]);
  assert.deepEqual(result.steps.map(step => step.displayStatus), ['completed', 'failed']);
  assert.equal(result.status, 'partial');
  assert.equal(result.completed, 1);
});

test('claimed success with missing or blocking artifacts cannot use the success icon or count', () => {
  for (const extra of [
    { missing: [{ kind: 'image', label: 'image', min_count: 1 }] },
    { issues: [{ artifact_name: 'plot.png', kind: 'image', reason_code: 'invalid_content', blocking: true }] },
  ]) {
    const result = presentation.presentPlan(plan('completed'), [answer('step-0', outcome('succeeded', extra))]);
    assert.equal(result.status, 'partial');
    assert.equal(result.completed, 0);
  }
});

test('explicit cancellation/interruption is neutral stopped, not completed or failed', () => {
  for (const reason_code of ['user_cancelled', 'execution_interrupted']) {
    const result = presentation.presentPlan(plan('failed'), [answer('step-0', outcome('failed', { reason_code }))]);
    assert.equal(result.status, 'stopped');
    assert.equal(result.completed, 0);
  }
});

test('actual shared session event projection preserves partial results across done and starts a fresh next turn', () => {
  const session = useAnalysisSession({ api: {} });
  let seq = 0;
  const emit = (event, data) => session.handleEvent({ event, data: { ...data, timestamp: ++seq, seq } });
  emit('message', { role: 'user', content: 'Visualize these data' });
  emit('plan', plan('running'));
  emit('step', { ...plan('failed').steps[0] });
  emit('message', { role: 'assistant', content: 'Some verified files are available.', metadata: {
    step_id: 'step-0', analysis_outcome: outcome('partial', { reason_code: 'answer_validation_unavailable' }),
  } });
  emit('done', {});
  assert.equal(session.isLoading.value, false);
  const finished = presentation.presentPlan(session.plan.value, session.messages.value);
  assert.equal(finished.status, 'partial');
  assert.equal(finished.completed, 0);
  emit('message', { role: 'user', content: 'A separate analysis' });
  emit('plan', plan('failed'));
  assert.equal(presentation.presentPlan(session.plan.value, session.messages.value).status, 'failed');
  session.dispose();
});

const source = readFileSync(new URL('../src/components/PlanPanel.vue', import.meta.url), 'utf8');
const script = compileScript(parse(source).descriptor, { id: 'plan-panel-test', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const componentModule = { exports: {} };
const translate = (key, values = {}) => (zh[key] || key).replace(/\{(\w+)\}/g, (_match, name) => String(values[name]));
const icon = (name) => vue.defineComponent({ render: () => vue.h('i', { 'data-icon': name }) });
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === 'vue-i18n') return { useI18n: () => ({ t: translate }) };
  if (name === 'lucide-vue-next') return Object.fromEntries(['ChevronUp', 'ChevronDown', 'Clock', 'CircleX', 'CircleAlert', 'CirclePause'].map(name => [name, icon(name)]));
  if (name === './icons/StepSuccessIcon.vue') return { default: icon('Success') };
  if (name === '../utils/planPresentation') return presentation;
  assert.fail(`Unexpected dependency: ${name}`);
}, componentModule, componentModule.exports);

test('actual PlanPanel renders consistent status labels, icons and successful-step counts', async () => {
  for (const [statuses, messages, label, iconName, count] of [
    [['completed'], [], '任务已完成', 'Success', 1],
    [['failed'], [], '任务失败', 'CircleX', 0],
    [['completed', 'failed'], [], '任务部分完成', 'CircleAlert', 1],
    [['failed'], [answer('step-0', outcome('partial'))], '任务部分完成', 'CircleAlert', 0],
    [['failed'], [answer('step-0', outcome('failed', { reason_code: 'user_cancelled' }))], '任务已停止', 'CirclePause', 0],
    [['pending', 'running'], [], 'Step 1', 'Clock', 0],
  ]) {
    const html = await renderToString(vue.createSSRApp(componentModule.exports.default, { plan: plan(...statuses), messages }));
    assert.ok(html.includes(label), html);
    assert.ok(html.includes(`data-icon="${iconName}"`), html);
    assert.ok(html.includes(`已完成 ${count} / ${statuses.length}`), html);
    if (iconName !== 'Success') assert.ok(!html.includes('任务已完成'), html);
  }
});

test('interactive chat and read-only replay both pass current messages to the shared panel', () => {
  for (const path of ['../src/pages/ChatPage.vue', '../src/pages/SharePage.vue']) {
    assert.match(readFileSync(new URL(path, import.meta.url), 'utf8'), /<PlanPanel\b[^\n]*:messages="messages"/);
  }
});
