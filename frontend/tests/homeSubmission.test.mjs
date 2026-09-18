import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { ref } from 'vue';
import { uploadAnalysisPrompt } from '../src/utils/analysisInputs.ts';

const source = readFileSync(new URL('../src/pages/HomePage.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('HomePage', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const declaration = ast.statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(item => item.name.getText(ast) === 'handleSubmit'));
const compiled = ts.transpileModule(declaration.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;

for (const originalMessage of ['解释这些数据', '']) {
  test(`home ${originalMessage ? 'text-and-file' : 'file-only'} submission snapshots every field before session creation`, async () => {
    let resolve;
    const created = new Promise(done => { resolve = done; });
    const sessions = [], saved = [], navigations = [];
    const initialFile = { file_id: 'file-a', filename: 'a.csv', content_type: 'text/csv', size: 12, upload_date: '2026-09-16' };
    const scope = {
      message: ref(originalMessage), attachments: ref([{ ...initialFile }]), isSubmitting: ref(false),
      selectedSkills: ref(['skill-a']), selectedMcpServers: ref(['mcp-a']), selectedProfile: ref({ id: 'profile-a' }),
      uploadAnalysisPrompt,
      createSession: id => { sessions.push(id); return created; },
      savePendingChat: request => saved.push(request), router: { push: route => navigations.push(route) },
      showErrorToast: () => assert.fail('unexpected error'), t: value => value,
    };
    const submit = new Function('scope', `with (scope) { ${compiled}; return handleSubmit; }`)(scope);
    const pending = submit();
    assert.equal(scope.isSubmitting.value, true);
    scope.message.value = 'different objective';
    scope.attachments.value[0].file_id = 'changed-object';
    scope.attachments.value.splice(0, 1, { file_id: 'late-upload', filename: 'new.csv' });
    scope.selectedSkills.value.push('late-skill');
    scope.selectedMcpServers.value.push('late-mcp');
    scope.selectedProfile.value.id = 'profile-b';
    await submit(); // Double submission cannot create another session.
    assert.deepEqual(sessions, ['profile-a']);
    resolve({ session_id: 'new-session' }); await pending;
    assert.deepEqual(saved, [{ sessionId: 'new-session',
      message: uploadAnalysisPrompt(originalMessage, [initialFile]),
      files: [initialFile], skills: ['skill-a'], mcpServers: ['mcp-a'], agentProfileId: 'profile-a' }]);
    assert.deepEqual(navigations, [{ path: '/chat/new-session' }]);
  });
}
