import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DATASET_CHAT_PLACEHOLDER,
  buildDatasetChatCapabilities,
} from '../src/utils/datasetCapabilitySelection.ts';

test('dataset chat advertises slash-triggered Skill selection', () => {
  assert.equal(DATASET_CHAT_PLACEHOLDER, '针对当前数据集提问，输入 / 使用技能...');
});

test('dataset chat capabilities preserve Skill and read-only dataset context', () => {
  assert.deepEqual(
    buildDatasetChatCapabilities('dataset-1', ['table-analysis']),
    {
      attachments: [],
      skills: ['table-analysis'],
      mcpServers: [],
      datasetIds: ['dataset-1'],
    },
  );
});
