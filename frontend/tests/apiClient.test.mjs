import assert from 'node:assert/strict';
import test from 'node:test';

import { ApiError, apiClient } from '../src/api/client.ts';


function adapterResponse(config, data, status = 200, statusText = 'OK') {
  return {
    config,
    data,
    headers: {},
    request: {},
    status,
    statusText,
  };
}


test('business failures are rejected as ApiError instances', async () => {
  const failure = await apiClient.request({
    url: '/test-business-error',
    adapter: async (config) => adapterResponse(config, {
      code: 400,
      msg: 'Dataset source directory does not exist',
      data: null,
    }),
  }).catch((error) => error);

  assert.ok(failure instanceof Error);
  assert.ok(failure instanceof ApiError);
  assert.equal(failure.status, 200);
  assert.equal(failure.code, 400);
  assert.equal(failure.message, 'Dataset source directory does not exist');
  assert.equal(failure.details.code, 400);
});


test('Axios-like failures are rejected as ApiError instances with response metadata', async () => {
  const details = {
    detail: [{ loc: ['body', 'name'], msg: 'Field required' }],
  };
  const originalConsoleError = console.error;
  console.error = () => {};

  try {
    const failure = await apiClient.request({
      url: '/test-http-error',
      adapter: async (config) => Promise.reject({
        message: 'Request failed with status code 422',
        request: {},
        response: adapterResponse(config, details, 422, 'Unprocessable Entity'),
      }),
    }).catch((error) => error);

    assert.ok(failure instanceof Error);
    assert.ok(failure instanceof ApiError);
    assert.equal(failure.status, 422);
    assert.equal(failure.code, 422);
    assert.equal(failure.message, 'Field required');
    assert.equal(failure.details, details);
  } finally {
    console.error = originalConsoleError;
  }
});
