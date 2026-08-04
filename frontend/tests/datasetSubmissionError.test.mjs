import assert from 'node:assert/strict';
import test from 'node:test';

import { datasetSubmissionErrorMessage } from '../src/utils/datasetSubmissionError.ts';


test('dataset submission preserves a specific API client 400 message', () => {
  const message = datasetSubmissionErrorMessage({
    code: 400,
    message: 'Dataset source directory does not exist',
    details: {
      code: 400,
      msg: 'Dataset source directory does not exist',
      data: null,
    },
  });

  assert.equal(message, 'Dataset source directory does not exist');
});


test('dataset submission gives administrators-only guidance for API client 403', () => {
  const message = datasetSubmissionErrorMessage({
    code: 403,
    message: 'Only administrators can submit server directories for analysis',
  });

  assert.equal(message, '仅管理员可提交服务器目录进行分析');
});


test('dataset submission uses a safe fallback for an unknown error', () => {
  assert.equal(
    datasetSubmissionErrorMessage(null),
    '提交失败，请检查数据集信息和服务器目录后重试',
  );
});


test('dataset submission reads a backend message from an Axios response', () => {
  const message = datasetSubmissionErrorMessage({
    message: 'Request failed with status code 400',
    response: {
      status: 400,
      data: {
        code: 400,
        msg: 'Dataset directory contains more files than allowed',
        data: null,
      },
    },
  });

  assert.equal(message, 'Dataset directory contains more files than allowed');
});


test('dataset submission reads FastAPI validation detail from an Axios response', () => {
  const message = datasetSubmissionErrorMessage({
    message: 'Request failed with status code 422',
    response: {
      status: 422,
      data: {
        detail: [
          { loc: ['body', 'storage_directory'], msg: 'Field required' },
        ],
      },
    },
  });

  assert.equal(message, 'Field required');
});
