import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { parse, compileScript } from '@vue/compiler-sfc';

const source = readFileSync(new URL('../src/utils/skillCatalog.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const module = { exports: {} };
new Function('exports', code)(module.exports);
const { filterSkillCatalog, skillCategory } = module.exports;
const base = { source: 'official', scope: 'global', description: '', triggers: [], installed: true };
const skills = [
  { ...base, name: 'table@1.0.0', title: '表格质量检查', format: 'data-skill-v1', category: 'quality', domains: ['tables'], version: '1.0.0', package_id: 'lab/table' },
  { ...base, name: 'table@2.0.0', format: 'data-skill-v1', category: 'quality', domains: ['tables'], version: '2.0.0', installed: false },
  { ...base, name: 'legacy', category: 'analysis', format: 'legacy', source: 'personal' },
];
const filters = { query: '', source: 'all', category: '', domain: '', installed: false };

test('catalog intersects workflow, domain, installation and source without merging versions', () => {
  assert.equal(filterSkillCatalog(skills, { ...filters, category: 'quality', domain: 'tables' }).length, 2);
  assert.deepEqual(filterSkillCatalog(skills, { ...filters, category: 'quality', domain: 'tables', installed: true }), [skills[0]]);
  assert.deepEqual(filterSkillCatalog(skills, { ...filters, category: 'quality', source: 'personal' }), []);
});

test('catalog searches title, package identity and exact release', () => {
  for (const query of ['表格质量', ' LAB/TABLE ', '1.0.0']) {
    assert.deepEqual(filterSkillCatalog(skills, { ...filters, query }), [skills[0]]);
  }
});

test('legacy and unknown categories stay unclassified rather than guessed', () => {
  assert.equal(skillCategory(skills[2]), 'unclassified');
  assert.equal(skillCategory({ ...skills[0], category: 'future' }), 'unclassified');
  assert.equal(skillCategory({ ...skills[0], category: 'toString' }), 'unclassified');
  assert.deepEqual(filterSkillCatalog(skills, { ...filters, category: 'unclassified' }), [skills[2]]);
});

test('release metadata renders an exact version and does not claim verification', async () => {
  const text = readFileSync(new URL('../src/components/SkillMetadata.vue', import.meta.url), 'utf8');
  const script = compileScript(parse(text).descriptor, { id: 'metadata-test', inlineTemplate: true });
  const compiled = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const component = { exports: {} };
  new Function('require', 'exports', compiled)(name => {
    if (name === 'vue') return vue;
    if (name === 'vue-i18n') return { useI18n: () => ({ t: key => key }) };
    if (name === '@/utils/skillCatalog') return module.exports;
    assert.fail(name);
  }, component.exports);
  const release = await renderToString(vue.createSSRApp(component.exports.default, { skill: skills[0] }));
  assert.match(release, /Version 1\.0\.0/);
  assert.match(release, /Skill not verified/);
  assert.match(release, /Data quality/);
  const legacy = await renderToString(vue.createSSRApp(component.exports.default, { skill: skills[2] }));
  assert.match(legacy, /Legacy skill/);
  assert.doesNotMatch(legacy, /Version|Skill not verified/);
});
