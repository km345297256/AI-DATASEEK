import assert from 'node:assert/strict';
import test from 'node:test';

import { apiClient } from '../src/api/client.ts';
import { getFileInfo } from '../src/api/file.ts';


test('getFileInfo requests the JSON info endpoint rather than the download stream', async () => {
  const originalAdapter = apiClient.defaults.adapter;
  let requestedUrl = '';
  apiClient.defaults.adapter = async (config) => {
    requestedUrl = config.url;
    return {
      config,
      data: {
        code: 0,
        msg: 'success',
        data: {
          file_id: 'minio:file-123',
          filename: 'chart.png',
          relative_path: 'reports/chart.png',
          upload_date: '2026-08-05T00:00:00Z',
        },
      },
      headers: {},
      request: {},
      status: 200,
      statusText: 'OK',
    };
  };

  try {
    const result = await getFileInfo('minio:file-123');

    assert.equal(requestedUrl, '/files/minio%3Afile-123/info');
    assert.equal(result?.file_id, 'minio:file-123');
    assert.equal(result?.relative_path, 'reports/chart.png');
  } finally {
    apiClient.defaults.adapter = originalAdapter;
  }
});
