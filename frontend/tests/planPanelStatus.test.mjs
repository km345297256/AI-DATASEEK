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
const icon = vue.defineComponent({ render: () => vue.h('span') });
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === 'vue-i18n') return { useI18n: () => ({ t: (key) => key }) };
  if (name === 'lucide-vue-next') return { ChevronUp: icon, ChevronDown: icon, Clock: icon, CircleX: icon };
  if (name === './icons/StepSuccessIcon.vue') return { default: icon };
  assert.fail(`Unexpected dependency: ${name}`);
}, module, module.exports);

const render = (statuses) => renderToString(vue.createSSRApp(module.exports.default, {
  plan: { steps: statuses.map((status, index) => ({ id: String(index), description: `步骤 ${index}`, status })) },
}));

test('failed or empty plans never display task completed', async () => {
  for (const statuses of [[], ['failed'], ['completed', 'failed']]) {
    const html = await render(statuses);
    assert.doesNotMatch(html, /Task Completed/);
    assert.match(html, statuses.length ? /部分步骤未完成/ : /任务状态待确认/);
  }
});

test('pending work stays visible after an earlier step succeeds', async () => {
  const html = await render(['completed', 'running']);
  assert.match(html, /步骤 1/);
  assert.doesNotMatch(html, /Task Completed/);
  assert.match(await render(['completed', 'completed']), /Task Completed/);
});
