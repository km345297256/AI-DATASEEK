import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as vue from 'vue';

function dialog(t) {
  const source = readFileSync(new URL('../src/components/MCPDialog.vue', import.meta.url), 'utf8');
  const descriptor = parse(source).descriptor;
  const compiled = ts.transpileModule(compileScript(descriptor, { id: 'MCPDialog' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const currentUser = vue.ref({ id: 'owner', role: 'admin' });
  const dependencies = {
    vue,
    'lucide-vue-next': { Plus: {}, Pencil: {}, Trash2: {} },
    'vue-i18n': { useI18n: () => ({ t: value => value }) },
    '@/components/ui/dialog': {},
    '@/api/mcp': {},
    '@/utils/toast': {},
    '@/composables/useAuth': { useAuth: () => ({ currentUser }) },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(id => {
    assert.ok(id in dependencies, `Unexpected dependency: ${id}`);
    return dependencies[id];
  }, module, module.exports);
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(
    vue.reactive({ open: false, selectedServers: [] }), { expose() {}, emit() {} },
  ));
  t.after(() => scope.stop());
  return { state, currentUser, template: descriptor.template.content };
}

test('MCP connection edit controls require ownership, not installation or admin catalog access', t => {
  const { state, currentUser, template } = dialog(t);
  assert.match(template, /v-if="isOwned\(server\)"/);
  assert.equal(state.isOwned({ owner_user_id: 'owner' }), true);
  assert.equal(state.isOwned({ user_id: 'owner' }), true);
  assert.equal(state.isOwned({ owner_user_id: 'other', installed: true }), false);
  assert.equal(state.isOwned({ scope: 'global', installed: true }), false);
  currentUser.value = null;
  assert.equal(state.isOwned({}), false);
});

test('owner edit form keeps exact connection fields including credentials and environment', t => {
  const { state } = dialog(t);
  const server = { name: 'synthetic', transport: 'streamable-http', enabled: true,
    url: 'https://user:synthetic-secret@example.invalid/path?custom=opaque',
    headers: { Authorization: 'synthetic-header' }, env: { KEY: 'synthetic-env' } };
  state.startEdit(server);
  assert.equal(state.form.url, server.url);
  assert.deepEqual(state.form.headers, server.headers);
  assert.deepEqual(state.form.env, server.env);
  assert.equal(state.editingExisting.value, true);
});
