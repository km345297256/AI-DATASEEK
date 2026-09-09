import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { compileScript, parse } from '@vue/compiler-sfc';
const source = readFileSync(new URL('../src/visualizations/extended/onlyOfficeResource.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { parseOfficeResource } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const lease = 'a'.repeat(43), now = 1_000_000;
const response = (frame_url = `http://office.localhost:7001/office-viewer/frame/${lease}`, expires_at = 1900) => ({
  kind: 'resources', payload: { provider: 'onlyoffice', lease, frame_url, expires_at },
});
test('office resources only allow a short lease at the dedicated local origin on the application port', () => {
  const result = parseOfficeResource(response(), 'http://localhost:7001/datasets', now);
  assert.equal(result.origin, 'http://office.localhost:7001'); assert.equal(result.lease, lease);
  for (const url of [`http://localhost:7001/office-viewer/frame/${lease}`, `https://example.org/office-viewer/frame/${lease}`,
    `http://office.localhost:7002/office-viewer/frame/${lease}`, 'http://office.localhost:7001/api/v1/files',
    `http://office.localhost:7001/office-viewer/frame/${lease}?x=1`, `http://office.localhost:7001/office-viewer/frame/${lease}#x`,
    `http://user@office.localhost:7001/office-viewer/frame/${lease}`, `http://office.localhost.:7001/office-viewer/frame/${lease}`]) {
    assert.throws(() => parseOfficeResource(response(url), 'http://localhost:7001/', now));
  }
  for (const expires of [999, 1902, '1900', NaN]) assert.throws(() => parseOfficeResource(response(undefined, expires), 'http://localhost:7001/', now));
});
test('office adapter uses unified prepare and destroys its bounded cross-origin iframe on disposal', () => {
  const source = readFileSync(new URL('../src/visualizations/extended/OnlyOfficePreview.vue', import.meta.url), 'utf8');
  const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
  assert.ok(compileScript(descriptor, { id: 'office-viewer', inlineTemplate: true }).content);
  assert.match(source, /requestVisualization\(props.file, props.plugin, 'prepare'/);
  assert.match(source, /sandbox="allow-scripts allow-same-origin"/);
  assert.doesNotMatch(source, /allow-(?:forms|popups|top-navigation|downloads)|DocsAPI|localStorage|sessionStorage|callbackUrl/);
  assert.match(source, /event.origin !== resource.origin/); assert.match(source, /event.source !== frame.value\?\.contentWindow/);
  assert.match(source, /load.onDispose/); assert.match(source, /void revoke\(resource.lease\)/);
});
