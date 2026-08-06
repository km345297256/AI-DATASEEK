import assert from 'node:assert/strict';
import test from 'node:test';

import { ApiError, normalizeApiError } from '../src/utils/apiError.ts';


test('normalizes an Axios-like FastAPI error to an Error with metadata', () => {
  const details = {
    detail: [
      { loc: ['body', 'storage_directory'], msg: 'Field required' },
    ],
  };
  const original = Object.assign(new Error('Request failed with status code 422'), {
    code: 'ERR_BAD_REQUEST',
    request: {},
    response: {
      status: 422,
      statusText: 'Unprocessable Entity',
      data: details,
    },
  });

  const normalized = normalizeApiError(original);

  assert.ok(normalized instanceof Error);
  assert.ok(normalized instanceof ApiError);
  assert.equal(normalized.message, 'Field required');
  assert.equal(normalized.status, 422);
  assert.equal(normalized.code, 422);
  assert.equal(normalized.details, details);
  assert.equal(normalized.cause, original);
});


test('preserves a backend business code separately from the HTTP status', () => {
  const details = { code: 1007, msg: 'Dataset cannot be submitted' };
  const normalized = normalizeApiError({
    response: {
      status: 400,
      statusText: 'Bad Request',
      data: details,
    },
  });

  assert.equal(normalized.status, 400);
  assert.equal(normalized.code, 1007);
  assert.equal(normalized.details, details);
  assert.equal(normalized.message, 'Dataset cannot be submitted');
});


test('normalizes a network failure and does not wrap ApiError twice', () => {
  const normalized = normalizeApiError({ message: 'Network Error', request: {} });

  assert.ok(normalized instanceof Error);
  assert.equal(normalized.code, 503);
  assert.equal(normalized.status, undefined);
  assert.equal(normalized.message, 'Network error, please check your connection');
  assert.equal(normalizeApiError(normalized), normalized);
});
