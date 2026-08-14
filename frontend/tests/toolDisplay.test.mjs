import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getToolDisplayDetail,
  resolveToolFunction,
  resolveToolName,
  safeToolContentForDisplay,
  sanitizeToolDisplayText,
} from '../src/utils/toolDisplay.ts';

const tool = (overrides = {}) => ({
  tool_call_id: 'call-1',
  name: 'shell',
  function: 'shell_run',
  args: {},
  status: 'calling',
  timestamp: 1,
  ...overrides,
});

test('shell_run exposes a useful command preview and expandable full command', () => {
  const command = `python3 - <<'PY'\n${'print("scientific analysis")\n'.repeat(12)}PY`;
  const detail = getToolDisplayDetail(tool({ args: { command } }));

  assert.match(detail.preview, /^python3 - <<'PY'/);
  assert.ok(detail.preview.length <= 180);
  assert.equal(detail.expandable, true);
  assert.equal(detail.full, command);
});

test('credentials and host paths are removed while sandbox paths stay concrete', () => {
  const command = 'API_KEY=top-secret python /data2/private/earth.nc --token abc123 --out /home/ubuntu/output/map.png';
  const safe = sanitizeToolDisplayText(command);

  assert.equal(safe.includes('top-secret'), false);
  assert.equal(safe.includes('abc123'), false);
  assert.equal(safe.includes('/data2/private/earth.nc'), false);
  assert.match(safe, /\[敏感参数已隐藏\]/);
  assert.match(safe, /\[受保护路径\]/);
  assert.match(safe, /\/home\/ubuntu\/output\/map\.png/);
});

test('prefixed environment and object credential keys are redacted', () => {
  const command = [
    'OPENAI_API_KEY=sk-openai',
    'AWS_SECRET_ACCESS_KEY=aws-secret',
    'MINIO_SECRET_KEY=minio-secret',
    'DATABASE_PASSWORD=db-password',
    'python /home/ubuntu/job.py',
  ].join(' ');
  const safeText = sanitizeToolDisplayText(command);
  const safeObject = safeToolContentForDisplay(tool({
    args: {
      OPENAI_API_KEY: 'sk-object',
      AWS_SECRET_ACCESS_KEY: 'aws-object',
      MINIO_SECRET_KEY: 'minio-object',
      DATABASE_PASSWORD: 'db-object',
    },
  }));

  for (const secret of [
    'sk-openai',
    'aws-secret',
    'minio-secret',
    'db-password',
  ]) {
    assert.equal(safeText.includes(secret), false);
  }
  for (const value of Object.values(safeObject.args)) {
    assert.equal(value, '[敏感参数已隐藏]');
  }
});

test('authorization bearer values are redacted from expanded commands', () => {
  const safe = sanitizeToolDisplayText(
    'curl -H "Authorization: Bearer sk-live-value" https://example.test',
  );

  assert.equal(safe.includes('sk-live-value'), false);
  assert.match(safe, /Authorization:\s+\[敏感参数已隐藏\]/);
});

test('URL credentials and sensitive query parameters are removed', () => {
  const safe = sanitizeToolDisplayText('curl https://alice:password@example.test/data?token=secret-value&format=json');

  assert.equal(safe.includes('password'), false);
  assert.equal(safe.includes('secret-value'), false);
  assert.match(safe, /https:\/\/\[敏感参数已隐藏\]@example\.test/);
  assert.match(safe, /token=\[敏感参数已隐藏\]/);
});

test('dataset_unpack shows source and destination without exposing host roots', () => {
  const detail = getToolDisplayDetail(tool({
    function: 'dataset_unpack',
    args: {
      archive_path: '/data/private/earth-observation.zip',
      output_dir: '/home/ubuntu/output/unpacked-1',
    },
  }));

  assert.equal(detail.full, '[受保护路径] → ~/output/unpacked-1');
});

test('shell_wait shows the session and bounded wait instead of a raw tool name', () => {
  const detail = getToolDisplayDetail(tool({
    function: 'shell_wait',
    args: { id: 'dataset-analysis', seconds: 30 },
  }));

  assert.equal(detail.full, 'dataset-analysis · 30 秒');
});

test('legacy stored events infer their function and toolkit', () => {
  const historic = tool({ name: 'dataset_unpack', function: '' });

  assert.equal(resolveToolFunction(historic), 'dataset_unpack');
  assert.equal(resolveToolName(historic), 'shell');
});

test('scientific plugin events resolve to the scientific toolkit', () => {
  const historic = tool({ name: 'scientific_visualize', function: '' });
  const current = tool({ name: 'plugin', function: 'geoscience_zonal_statistics' });

  assert.equal(resolveToolFunction(historic), 'scientific_visualize');
  assert.equal(resolveToolName(historic), 'scientific');
  assert.equal(resolveToolName(current), 'scientific');
});

test('display clone sanitizes shell args and console without mutating the event', () => {
  const original = tool({
    args: { command: 'TOKEN=secret python /root/job.py' },
    content: { console: [{ command: 'cat /root/private.txt', output: 'password=hunter2' }] },
  });
  const safe = safeToolContentForDisplay(original);

  assert.equal(safe.args.command.includes('secret'), false);
  assert.equal(safe.args.command.includes('/root/job.py'), false);
  assert.equal(safe.content.console[0].command.includes('/root/private.txt'), false);
  assert.equal(safe.content.console[0].output.includes('hunter2'), false);
  assert.equal(original.args.command, 'TOKEN=secret python /root/job.py');
});
