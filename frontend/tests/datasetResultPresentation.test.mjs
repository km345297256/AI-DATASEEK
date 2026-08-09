import assert from 'node:assert/strict';
import test from 'node:test';

import { stripHiddenDatasetResultNotices } from '../src/utils/datasetResultPresentation.ts';

test('removes legacy dataset inventory notices from restored assistant messages', () => {
  const content = [
    '文件组织直接来自数据中心登记清单，无需调用模型判断。',
    '',
    '`示例数据集`',
    '```text',
    '└── values.csv (1 KiB)',
    '```',
    '',
    '方法与限制：仅展示登记清单中的相对路径，不读取文件内容，也不暴露宿主机真实路径。',
  ].join('\n');

  assert.equal(
    stripHiddenDatasetResultNotices(content),
    ['`示例数据集`', '```text', '└── values.csv (1 KiB)', '```'].join('\n'),
  );
});

test('removes the English legacy notices and preserves unrelated limitations', () => {
  const legacy = [
    'The file organization comes directly from the data-center inventory; no model decision was required.',
    '',
    'table.csv',
    '',
    'Method and limits: only registered relative paths are shown; file contents are not read and real host paths remain private.',
  ].join('\n');
  assert.equal(stripHiddenDatasetResultNotices(legacy), 'table.csv');

  const unrelated = '方法与限制：压缩包受文件数量和体积限制。';
  assert.equal(stripHiddenDatasetResultNotices(unrelated), unrelated);
});
