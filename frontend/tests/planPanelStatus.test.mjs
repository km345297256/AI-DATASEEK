import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';

const source = readFileSync(new URL('../src/components/PlanPanel.vue', import.meta.url), 'utf8');
const script = compileScript(parse(source).descriptor, { id: 'plan-status-test', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
let currentRun;
const taskProgress = {};
new Function('exports', ts.transpileModule(readFileSync(new URL('../src/utils/taskProgress.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(taskProgress);
const icon = vue.defineComponent({ render: () => vue.h('span') });
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === 'vue-i18n') return { useI18n: () => ({ t: (key) => key }) };
  if (name === 'lucide-vue-next') return { ChevronUp: icon, ChevronDown: icon, Clock: icon, CircleX: icon };
  if (name === './icons/StepSuccessIcon.vue') return { default: icon };
  if (name === './DataRunHistory.vue') return { default: icon };
  if (name === '@/utils/taskProgress') return taskProgress;
  if (name === '@/composables/useDataRuns') return { useDataRuns: () => ({ runs: vue.ref(currentRun ? [currentRun] : []), loading: vue.ref(false), error: vue.ref(''), nextBefore: vue.ref(null), load() {} }) };
  assert.fail(`Unexpected dependency: ${name}`);
}, module, module.exports);

const render = (statuses, run, isRunning = false) => {
  currentRun = run;
  return renderToString(vue.createSSRApp(module.exports.default, {
    sessionId: 'session', isRunning,
    plan: { steps: statuses.map((status, index) => ({ id: String(index), description: `步骤 ${index}`, status })) },
  }));
};

test('completed stages cannot override failed, incomplete or absent run records', async () => {
  assert.match(await render(['completed'], { status: 'failed', history_complete: true }), /任务失败/);
  assert.match(await render(['completed'], { status: 'succeeded', history_complete: false }), /任务记录不完整/);
  assert.doesNotMatch(await render(['completed'], undefined), /任务已完成/);
});

test('only a terminal successful run displays overall completion', async () => {
  const succeeded = { status: 'succeeded', history_complete: true };
  assert.match(await render(['completed'], succeeded), /任务已完成/);
  assert.match(await render(['completed'], succeeded, true), /任务进行中/);
  assert.doesNotMatch(await render(['completed'], succeeded, true), /任务已完成/);
});
