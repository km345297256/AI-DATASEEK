import assert from 'node:assert/strict';
import test from 'node:test';
import { readAnalysisOutcome, analysisOutcomeIssues, analysisOutcomeMissing, analysisOutcomeTitle } from '../src/utils/analysisOutcome.ts';

test('missing artifact is a controlled incomplete file notice without changing counts or continuation authority', () => {
  const outcome = readAnalysisOutcome({ status: 'partial', reason_code: 'artifact_validation_failed',
    missing: [{ kind: 'table', min_count: 2, label: '数据表' }], can_resume: false,
    issues: [{ artifact_name: '任意结果.csv', kind: 'table', reason_code: 'missing_artifact', blocking: true }] });
  assert.equal(analysisOutcomeTitle(outcome), '部分完成');
  assert.deepEqual(analysisOutcomeMissing(outcome), ['数据表 × 2']);
  assert.equal(analysisOutcomeIssues(outcome)[0].reason, '成果文件尚未生成。');
  assert.equal(outcome.can_resume, false);
  assert.doesNotMatch(analysisOutcomeIssues(outcome)[0].reason, /自动重跑|请重发|已完成/);
});

test('unsafe diagnostics are not reclassified as missing or rendered from arbitrary text', () => {
  const outcome = readAnalysisOutcome({ status: 'failed', reason_code: 'artifact_validation_failed', missing: [], can_resume: false,
    issues: [
      { artifact_name: 'safe.csv', kind: 'table', reason_code: 'unavailable_or_unsafe_path', blocking: true },
      { artifact_name: '/private/secret.csv', kind: 'table', reason_code: 'missing_artifact', blocking: true },
      { artifact_name: 'bad.csv', kind: 'table', reason_code: 'missing_artifact /private/secret', blocking: true },
    ] });
  assert.equal(outcome.issues.length, 1);
  assert.equal(analysisOutcomeIssues(outcome)[0].reason, '文件不可读取或不在允许的交付范围内。');
  assert.doesNotMatch(JSON.stringify(outcome), /private|secret/);
});
