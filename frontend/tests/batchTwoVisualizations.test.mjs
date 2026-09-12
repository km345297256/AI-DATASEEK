import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as instrumentData from '../src/visualizations/extended/instrumentData.ts';
import * as archiveData from '../src/visualizations/extended/archiveMembersData.ts';
import { signalFixture, mcaFixture, archiveFixture, version, memberId } from './batchTwoFixtures.mjs';
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); await vue.nextTick(); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function mount(name, request, overrides = {}) {
  const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: name }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }, modules = { vue, '../../composables/usePreviewLoad': { usePreviewLoad }, './instrumentData': instrumentData,
    './archiveMembersData': archiveData, '../runtime': { requestVisualization: request }, './NumericSeriesPlot.vue': {}, ...overrides };
  new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-1', filename: 'data.edf' }, plugin: { id: 'viz-test', version: '1', enabled: true } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  return { state, props, start: flush, stop: () => scope.stop() };
}
test('signal response preserves mixed units/rates, exact time origins and read-vs-source budgets', () => {
  const result = signalFixture({ channels: [0, 1], start_seconds: 900000, duration_seconds: 1 });
  const parsed = instrumentData.parseSignalWindow(result.payload, result.metadata);
  assert.deepEqual(parsed.traces.map(row => [row.unit, row.x.length]), [['uV', 4], ['mV', 2]]);
  assert.equal(parsed.traces[0].x[0], 900000); assert.equal(parsed.readBytes, 1268); assert.ok(parsed.sourceBytes > 2e9);
});
for (const [name, mutate] of [
  ['nonfinite', result => { result.payload.series[0].y[0] = NaN; }], ['mismatched channel', result => { result.payload.series[0].channel = 1; }],
  ['resampled rate', result => { result.payload.series[0].sample_rate = 500; }], ['annotation choice', result => { result.payload.selected.channels = [2]; }],
  ['unsafe label', result => { result.payload.choices.channels[0].label = '<img>'; }], ['duplicate time', result => { result.payload.series[0].x[1] = 0; }],
  ['outside window', result => { result.payload.series[0].x[0] = -1; }], ['negative read', result => { result.metadata.read_bytes = -1; }],
  ['over budget', result => { result.metadata.read_bytes = 8388609; }], ['oversized source', result => { result.metadata.source_bytes = 8589934593; }],
  ['identity exposed', result => { result.metadata.identity_fields_hidden = false; }], ['coerced time', result => { result.payload.selected.start_seconds = '0'; }],
]) test(`signal frontend rejects ${name}`, () => { const result = signalFixture(); mutate(result); assert.throws(() => instrumentData.parseSignalWindow(result.payload, result.metadata)); });
test('MCA displays integer counts without losing safe precision and does not invent energy', () => {
  for (const calibrated of [true, false]) { const result = mcaFixture(calibrated), parsed = instrumentData.parseMcaSpectrum(result.payload, result.metadata);
    assert.equal(parsed.trace.y[3], Number.MAX_SAFE_INTEGER); assert.equal(parsed.calibrated, calibrated);
    assert.equal(parsed.trace.x[0], calibrated ? 1 : 0); assert.equal(parsed.xLabel, calibrated ? '能量 (keV)' : '通道索引'); }
});
for (const [name, mutate] of [
  ['unsafe integer counts', result => { result.payload.array.values[1] = 2 ** 53; }], ['fractional counts', result => { result.payload.array.values[1] = 0.5; }],
  ['negative counts', result => { result.payload.array.values[1] = -1; }], ['invented channel axis', result => { result.payload.array.values[0] = 100; }],
  ['fitted data', result => { result.metadata.fitting = true; }], ['wrong shape', result => { result.payload.array.shape = [8]; }],
  ['unconfirmed energy', result => { result.metadata.axis = 'energy'; }], ['coerced metadata', result => { result.metadata.channels = '4'; }],
]) test(`MCA frontend rejects ${name}`, () => { const result = mcaFixture(); mutate(result); assert.throws(() => instrumentData.parseMcaSpectrum(result.payload, result.metadata)); });
test('archive directory returns only scoped members and text remains inert', () => {
  const directory = archiveFixture(), parsed = archiveData.parseMemberPage(directory.payload, directory.metadata, null, 0);
  assert.equal(parsed.resources[0].memberId, memberId); assert.equal(parsed.resources[1].memberId, null);
  const text = archiveFixture({ member_id: memberId }); assert.match(archiveData.parseMemberPage(text.payload, text.metadata, memberId, 0).lines[0][1], /^<script>/);
});
for (const [name, mutate] of [
  ['wrong checksum', result => { result.metadata.checksum_verified = false; }], ['wrong member', result => { result.metadata.member_id = 'member-' + 'b'.repeat(64); }],
  ['write scope', result => { result.metadata.writes_source = true; }], ['extra URL', result => { result.metadata.url = 'http://remote'; }],
  ['large expanded member', result => { result.metadata.member_bytes = 262145; }], ['missing lines', result => { result.payload.table.rows.pop(); }],
  ['coerced format', result => { result.metadata.format = ['zip']; }], ['unbounded line', result => { result.payload.table.rows[0][1] = 'x'.repeat(1025); }],
]) test(`archive frontend rejects ${name}`, () => { const result = archiveFixture({ member_id: memberId }); mutate(result); assert.throws(() => archiveData.parseMemberPage(result.payload, result.metadata, memberId, 0)); });
test('signal controls make one version-pinned request only on explicit submit', async t => {
  const calls = [], view = mount('SignalWindowPreview', async (_file, _plugin, operation, options) => { calls.push(options); assert.equal(operation, 'preview'); return signalFixture(options); });
  t.after(view.stop); await view.start(); assert.deepEqual(calls, [{ kind: 'series' }]);
  view.state.channels.value = [0, 1]; view.state.start.value = 5; view.state.duration.value = 2; await flush(); assert.equal(calls.length, 1);
  await view.state.load(false); assert.deepEqual(calls[1], { kind: 'series', version, channels: [0, 1], start_seconds: 5, duration_seconds: 2 });
  assert.equal(view.state.data.value.traces.length, 2);
});
test('signal version mismatch does not display old or new data as the requested window', async t => {
  let calls = 0; const view = mount('SignalWindowPreview', async () => ({ ...signalFixture(), version: ++calls === 1 ? version : 'f'.repeat(64) }));
  t.after(view.stop); await view.start(); await view.state.load(false); assert.match(view.state.error.value, /变化/);
});
test('signal rejects a valid response for a different requested time window', async t => {
  const view = mount('SignalWindowPreview', async () => signalFixture()); t.after(view.stop); await view.start();
  view.state.start.value = 10; await view.state.load(false); assert.match(view.state.error.value, /不一致/);
});
test('archive uses the selected directory reference, pins every page and restores directory offset', async t => {
  const calls = [], view = mount('ArchiveMembersPreview', async (_file, _plugin, _op, options) => { calls.push(options); return archiveFixture(options); });
  t.after(view.stop); await view.start(); view.state.openMember('member-' + 'b'.repeat(64)); await flush(); assert.equal(calls.length, 1);
  view.state.openMember(memberId); await flush(); assert.deepEqual(calls[1], { kind: 'table', row_offset: 0, version, member_id: memberId });
  view.state.turn(200); await flush(); assert.equal(calls[2].row_offset, 200); assert.equal(calls[2].version, version); assert.equal(view.state.page.value.lines.length, 1);
  view.state.directory(); await flush(); assert.deepEqual(calls[3], { kind: 'table', row_offset: 0, version });
});
for (const [name, fixture, field] of [['SignalWindowPreview', signalFixture, 'data'], ['McaSpectrumPreview', mcaFixture, 'data'], ['ArchiveMembersPreview', archiveFixture, 'page']]) {
  test(`${name} polling clones do not reread; file and plugin version replacement do`, async t => {
    let calls = 0; const view = mount(name, async () => { calls++; return fixture(); }); t.after(view.stop); await view.start();
    for (let i = 0; i < 3; i++) { view.props.file = { ...view.props.file }; view.props.plugin = { ...view.props.plugin }; await flush(); }
    assert.equal(calls, 1); view.props.plugin.version = '2'; await flush(); assert.equal(calls, 2); view.props.file.file_id = 'file-2'; await flush(); assert.equal(calls, 3);
  });
  test(`${name} ignores late response after disable/unmount and aborts requests`, async () => {
    const wait = deferred(); let signal; const view = mount(name, (_f, _p, _o, _v, input) => { signal = input; return wait.promise; });
    view.stop(); assert.equal(signal.aborted, true); wait.resolve(fixture()); await flush(); assert.equal(view.state[field].value, undefined); assert.equal(view.state.error.value, '');
  });
}
test('new preview templates never inject markup, store paths or navigate to external resources', () => {
  for (const name of ['SignalWindowPreview', 'McaSpectrumPreview', 'ArchiveMembersPreview', 'NumericSeriesPlot']) {
    const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /v-html|innerHTML|localStorage|sessionStorage|window\.open|\beval\(/);
  }
});
