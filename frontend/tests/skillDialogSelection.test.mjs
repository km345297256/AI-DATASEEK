import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { parse, compileScript } from '@vue/compiler-sfc';
import ts from 'typescript';

function compile(source) {
  return ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
}
const helper = { exports: {} };
new Function('exports', compile(readFileSync(new URL('../src/utils/skillCatalog.ts', import.meta.url), 'utf8')))(helper.exports);
const script = compileScript(parse(readFileSync(new URL('../src/components/SkillDialog.vue', import.meta.url), 'utf8')).descriptor, { id: 'selection-test' });

test('dialog filters by workflow and title while preserving exact release selections', async () => {
  const releases = [
    { name: 'lab:table@1.0.0', title: '表格检查', category: 'quality', format: 'data-skill-v1', triggers: [] },
    { name: 'lab:table@2.0.0', title: '表格检查', category: 'quality', format: 'data-skill-v1', triggers: [] },
    { name: 'auto', triggers: [] },
  ];
  const component = { exports: {} };
  new Function('require', 'module', 'exports', compile(script.content))(name => {
    if (name === 'vue') return vue;
    if (name === 'vue-i18n') return { useI18n: () => ({ t: key => key }) };
    if (name === '@/utils/skillCatalog') return helper.exports;
    if (name === '@/api/skill') return { listSkills: async () => releases, getSkillPreferences: async () => ['auto'] };
    return {};
  }, component, component.exports);
  const scope = vue.effectScope();
  const props = vue.reactive({ open: false, selectedSkills: ['lab:table@1.0.0'] });
  const state = scope.run(() => component.exports.default.setup(props, { expose() {}, emit(event, value) {
    if (event === 'update:selectedSkills') props.selectedSkills = value;
  } }));
  try {
    await state.loadSkills();
    assert.equal(state.visibleSkills.value.length, 2);
    state.query.value = '表格';
    state.category.value = 'quality';
    assert.equal(state.visibleSkills.value.length, 2);
    state.toggleSkill(releases[1].name);
    assert.deepEqual(props.selectedSkills, ['lab:table@1.0.0', 'lab:table@2.0.0']);
    state.category.value = 'acquisition';
    assert.equal(state.visibleSkills.value.length, 0);
    assert.equal(props.selectedSkills.length, 2);
    state.toggleSkill(releases[0].name);
    assert.deepEqual(props.selectedSkills, ['lab:table@2.0.0']);
  } finally { scope.stop(); }
});
