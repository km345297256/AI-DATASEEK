import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  MAX_TOOL_PRESENTATION_DATA_BYTES,
  presentationColumns,
  presentationRows,
  resolveToolPresentation,
  safeToolResourceUrl,
  summarizeGeoPresentation,
} from '../src/utils/toolPresentation.ts';

const tool = (overrides = {}) => ({
  tool_call_id: 'call-1',
  name: 'plugin',
  function: 'station_summary',
  args: {},
  status: 'called',
  timestamp: 1,
  ...overrides,
});

const compactJsonBytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength;

test('auto and missing descriptors preserve the existing tool UI', () => {
  assert.equal(resolveToolPresentation(tool()), null);
  assert.equal(resolveToolPresentation(tool({ presentation: { kind: 'auto' } })), null);
});

test('generic card consumes declarative result and redacts secrets and host paths', () => {
  const resolved = resolveToolPresentation(tool({
    presentation: { kind: 'generic', title: 'Station result' },
    content: {
      result: {
        station: 'A',
        api_key: 'never-render-this',
        openaiApiKey: 'never-render-this-either',
        dbPassword: 'database-secret',
        apiSecret: 'provider-secret',
        source: '/Users/alice/private/stations.csv',
        note: 'token=secret-value',
      },
    },
  }));

  assert.ok(resolved);
  assert.equal(resolved.kind, 'generic');
  assert.deepEqual(resolved.data, {
    station: 'A',
    source: '[受保护路径]',
    note: 'token=[敏感参数已隐藏]',
  });
});

test('card data has a deterministic aggregate serialized-byte ceiling', () => {
  const longKey = (row, column) => {
    const prefix = `field-${String(row).padStart(3, '0')}-${String(column).padStart(3, '0')}-`;
    // Quotes make each key considerably larger after JSON escaping.
    return prefix + '"'.repeat(120 - prefix.length);
  };
  const hostileRows = Array.from({ length: 200 }, (_, row) => ({
    api_key: 'must-not-reach-card',
    source: '/Users/alice/private/data.nc token=must-not-reach-card',
    ...Object.fromEntries(Array.from(
      { length: 78 },
      (_, column) => [longKey(row, column), 'x'],
    )),
  }));
  const input = tool({
    presentation: { kind: 'table' },
    content: { result: hostileRows },
  });

  const first = resolveToolPresentation(input);
  const second = resolveToolPresentation(input);

  assert.ok(first);
  assert.ok(second);
  assert.deepEqual(first.data, second.data);
  const serialized = JSON.stringify(first.data);
  assert.ok(compactJsonBytes(first.data) <= MAX_TOOL_PRESENTATION_DATA_BYTES);
  assert.ok(compactJsonBytes(first.data) > 250_000);
  assert.equal(serialized.includes('api_key'), false);
  assert.equal(serialized.includes('must-not-reach-card'), false);
  assert.equal(serialized.includes('/Users/alice'), false);
});

test('card budget counts multibyte UTF-8 data rather than characters', () => {
  const resolved = resolveToolPresentation(tool({
    presentation: {
      kind: 'log',
      data: Array.from({ length: 20 }, () => '界'.repeat(20_000)),
    },
  }));

  assert.ok(resolved);
  assert.ok(compactJsonBytes(resolved.data) <= MAX_TOOL_PRESENTATION_DATA_BYTES);
  assert.ok(resolved.data.reduce((total, item) => total + item.length, 0) < 100_000);
});

test('card data keeps the item, node, and depth limits inside the byte ceiling', () => {
  const itemLimited = resolveToolPresentation(tool({
    presentation: { kind: 'generic', data: Array.from({ length: 250 }, (_, index) => index) },
  }));
  assert.ok(itemLimited);
  assert.equal(itemLimited.data.length, 200);

  const nodeLimited = resolveToolPresentation(tool({
    presentation: {
      kind: 'generic',
      data: Array.from({ length: 200 }, () => Object.fromEntries(
        Array.from({ length: 80 }, (_, column) => [`field_${column}`, column]),
      )),
    },
  }));
  assert.ok(nodeLimited);
  assert.ok(nodeLimited.data.length < 200);
  assert.ok(compactJsonBytes(nodeLimited.data) <= MAX_TOOL_PRESENTATION_DATA_BYTES);

  let nested = 'leaf';
  for (let depth = 0; depth < 12; depth += 1) nested = { next: nested };
  const depthLimited = resolveToolPresentation(tool({
    presentation: { kind: 'generic', data: nested },
  }));
  assert.ok(depthLimited);
  assert.match(JSON.stringify(depthLimited.data), /内容层级过深/);
});

test('table helpers honor declared columns and cap rendered rows', () => {
  const resolved = resolveToolPresentation(tool({
    presentation: {
      kind: 'table',
      columns: [
        { key: 'value', label: 'Value', align: 'right' },
        { key: 'password', label: 'Password' },
      ],
    },
    content: { result: { summary: { preview: Array.from({ length: 150 }, (_, index) => ({ value: index })) } } },
  }));

  assert.ok(resolved);
  const rows = presentationRows(resolved.data);
  assert.equal(rows.length, 100);
  assert.deepEqual(presentationColumns(resolved, rows), [
    { key: 'value', label: 'Value', align: 'right' },
  ]);
});

test('resource URLs are restricted to same-origin signed file routes', () => {
  const origin = 'http://localhost:7001';
  const signature = 'a'.repeat(64);
  assert.equal(
    safeToolResourceUrl(`/api/v1/files/file-1?signature=${signature}&expires=123`, origin),
    `/api/v1/files/file-1?signature=${signature}&expires=123`,
  );
  assert.equal(safeToolResourceUrl('/api/v1/files/file-1', origin), '/api/v1/files/file-1');
  assert.equal(safeToolResourceUrl(`http://localhost:7001/api/v1/files/file-1?signature=${signature}&expires=123`, origin), undefined);
  assert.equal(safeToolResourceUrl('/api/v1/files/file-1?signature=abc&expires=123', origin), undefined);
  assert.equal(safeToolResourceUrl(`/api/v1/files/file-1?signature=${signature}`, origin), undefined);
  assert.equal(safeToolResourceUrl(`/api/v1/files/file-1?signature=${signature}&expires=123&token=secret`, origin), undefined);
  assert.equal(safeToolResourceUrl(`/api/v1/files/file-1?signature=${signature}&expires=123#preview`, origin), undefined);
  assert.equal(safeToolResourceUrl(`//localhost:7001/api/v1/files/file-1?signature=${signature}&expires=123`, origin), undefined);
  assert.equal(safeToolResourceUrl('https://tracker.example/image.png', origin), undefined);
  assert.equal(safeToolResourceUrl('javascript:alert(1)', origin), undefined);
  assert.equal(safeToolResourceUrl('data:image/svg+xml,<svg onload=alert(1)>', origin), undefined);
  assert.equal(safeToolResourceUrl('/api/v1/files/../auth/me', origin), undefined);
  assert.equal(safeToolResourceUrl('http://alice:secret@localhost:7001/api/v1/files/file-1', origin), undefined);
});

test('map summary accepts bounded GeoJSON without executing renderer code', () => {
  const summary = summarizeGeoPresentation({
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', geometry: { type: 'Point', coordinates: [116.4, 39.9] } },
      { type: 'Feature', geometry: { type: 'LineString', coordinates: [[120, 30], [121, 31]] } },
    ],
  });

  assert.equal(summary.featureCount, 2);
  assert.deepEqual(summary.geometryTypes.sort(), ['LineString', 'Point']);
  assert.deepEqual(summary.bounds, [116.4, 30, 121, 39.9]);
});

test('card template never renders extension-provided HTML', async () => {
  const source = await readFile(new URL('../src/components/DeclarativeToolCard.vue', import.meta.url), 'utf8');

  assert.equal(source.includes('v-html'), false);
  assert.equal(source.includes('eval('), false);
  assert.equal(source.includes('new Function'), false);
  assert.match(source, /kindIcons\[presentation\.value\.kind\]/);
});
