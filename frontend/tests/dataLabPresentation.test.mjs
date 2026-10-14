import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
function load(file) {
  const code = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  new Function('exports', code)(exports);
  return exports;
}
const { skillLabel } = load('../src/utils/skillLabel.ts');
const { taskRunLabel } = load('../src/utils/taskProgress.ts');

test('readable label preserves exact selected release even if another package version has the same name', () => {
  const digest = 'a'.repeat(64);
  const selector = `dataseek:table@1.0.0#sha256=${digest}`;
  const catalog = [{ name: 'dataseek:table@1.0.0', title: '表格基础质检', version: '1.0.0', package_sha256: digest }];
  assert.equal(skillLabel(selector, catalog), '表格基础质检 · v1.0.0');
  assert.equal(skillLabel(selector, []), 'dataseek:table · v1.0.0');
  assert.equal(skillLabel(selector, [{ ...catalog[0], package_sha256: 'b'.repeat(64), title: '另一份包' }]), 'dataseek:table · v1.0.0');
  assert.equal(selector, `dataseek:table@1.0.0#sha256=${digest}`);
  assert.equal(skillLabel('legacy', []), 'legacy');
});

test('task summary uses run outcome and never a completed stage to infer overall success', () => {
  assert.equal(taskRunLabel({ status: 'succeeded', history_complete: true }, false, ''), '最近执行：任务已完成');
  assert.equal(taskRunLabel({ status: 'succeeded', history_complete: true }, true, ''), '任务进行中');
  assert.equal(taskRunLabel({ status: 'succeeded', history_complete: false }, false, ''), '任务记录不完整');
  assert.equal(taskRunLabel({ status: 'failed', history_complete: true }, false, ''), '最近执行：任务失败');
  assert.equal(taskRunLabel(undefined, false, ''), '任务记录');
  assert.equal(taskRunLabel({ status: 'succeeded', history_complete: true }, false, 'offline'), '任务状态暂不可用');
});
