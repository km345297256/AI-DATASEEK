import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { ApiError } from '../src/utils/apiError.ts';
import {
  datasetSubmissionErrorMessage,
  isAbsoluteDatasetDirectory,
} from '../src/utils/datasetSubmission.ts';

test('dataset directory must be an absolute path without traversal segments', () => {
  assert.equal(isAbsoluteDatasetDirectory('/data/datasets/example'), true);
  assert.equal(isAbsoluteDatasetDirectory(' /Users/example/dataset '), true);
  assert.equal(isAbsoluteDatasetDirectory('relative/dataset'), false);
  assert.equal(isAbsoluteDatasetDirectory('/data/../private'), false);
});

test('dataset submission preserves a specific API message', () => {
  assert.equal(datasetSubmissionErrorMessage({
    code: 400,
    details: { code: 400, msg: 'Dataset source directory does not exist' },
  }), 'Dataset source directory does not exist');
});

test('dataset submission validation never returns the rejected absolute path', () => {
  const message = datasetSubmissionErrorMessage(new ApiError('Unprocessable Entity', {
    status: 422,
    code: 422,
    details: {
      detail: [
        {
          loc: ['body', 'storage_directory'],
          msg: 'Field required',
          input: '/private/dataset/path',
        },
      ],
    },
  }));

  assert.equal(message, 'Field required');
  assert.equal(message.includes('/private/dataset/path'), false);
});

test('dataset setup persistently registers metadata and domain, keeping paths out of browser storage', async () => {
  const source = await readFile(new URL('../src/pages/DatasetSetupPage.vue', import.meta.url), 'utf8');

  assert.match(
    source,
    /DEFAULT_STORAGE_DIRECTORY = '\/Users\/luchangfa\/Documents\/Codex\/Data'/,
  );
  assert.match(source, /storageDirectory = ref\(DEFAULT_STORAGE_DIRECTORY\)/);
  assert.match(
    source,
    /registerDataset\(\{\s*name:\s*name\.value\.trim\(\),\s*description:\s*description\.value\.trim\(\),\s*domain:\s*domain\.value,\s*storage_directory:\s*directory,\s*\}\)/,
  );
  assert.match(source, /router\.push\(`\/dataset\/seek\/\$\{encodeURIComponent\(result\.dataset_id\)\}`\)/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|searchParams/);
});
