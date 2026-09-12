import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';
import * as mediaData from '../src/visualizations/extended/mediaData.ts';
import * as structureData from '../src/visualizations/extended/structureData.ts';

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); await vue.nextTick(); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function mount(name, dependencies = {}, input = {}) {
  const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8');
  const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: name }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} }, mounted = [];
  const modules = { vue: { ...vue, onMounted: callback => mounted.push(callback) },
    '../../composables/usePreviewLoad': { usePreviewLoad }, './mediaData': mediaData, './structureData': structureData, ...dependencies };
  new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope(), props = vue.reactive({ file: { file_id: 'file-1', filename: 'local.mp3' },
    plugin: { id: 'viz-audio', adapter: 'audio-waveform', version: '1', enabled: true, limits: { max_input_bytes: 999999999, max_output_bytes: 1024 } }, ...input });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  return { state, props, start: async () => { for (const callback of mounted) await callback(); await flush(); }, stop: () => scope.stop() };
}
function wav({ channels = 1, bits = 16, code = 1, sampleRate = 8, samples = [-1, 0, 0.5, 0.99] } = {}) {
  const size = samples.length * bits / 8, buffer = new ArrayBuffer(44 + size + size % 2), view = new DataView(buffer);
  const tag = (position, value) => [...value].forEach((char, i) => view.setUint8(position + i, char.charCodeAt(0)));
  tag(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); tag(8, 'WAVE'); tag(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, code, true); view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * channels * bits / 8, true);
  view.setUint16(32, channels * bits / 8, true); view.setUint16(34, bits, true); tag(36, 'data'); view.setUint32(40, size, true);
  samples.forEach((value, i) => {
    const position = 44 + i * bits / 8;
    if (code === 3) view.setFloat32(position, value, true);
    else if (bits === 8) view.setUint8(position, Math.round(value * 128 + 128));
    else if (bits === 16) view.setInt16(position, Math.round(value * 32768), true);
    else if (bits === 32) view.setInt32(position, Math.round(value * 2147483648), true);
    else { const sample = Math.round(value * 8388608); view.setUint8(position, sample & 255); view.setUint8(position + 1, sample >>> 8 & 255); view.setUint8(position + 2, sample >>> 16 & 255); }
  });
  return buffer;
}
const node = (path, label, node_type = 'object', value) => ({ path, node_type, attributes: { label, ...(value !== undefined ? { value } : {}) } });
const tree = () => [node('/0', 'root'), node('/0/0', 'experiment'), node('/0/0/0', 'large integer', 'number', '9007199254740993'), node('/0/1', 'other', 'string', '<script>alert(1)</script>')];
const result = (kind, payload) => ({ kind, payload, metadata: { format: kind === 'tree' ? 'JSON' : 'ZIP' }, warnings: [], sampled: false, version: 'a'.repeat(64) });
const archive = (offset = 0) => ({ columns: ['成员', '类型'], rows: [['data.csv', 'file']], row_offset: offset, column_offset: 0, total_rows: 450, total_columns: 2 });

test('media formats allow only declared local media, never playlists or URLs', () => {
  assert.equal(mediaData.mediaFormat('SAMPLE.MP4', 'video').mime, 'video/mp4');
  assert.equal(mediaData.mediaFormat('sample.wav', 'audio').maxBytes, 16 * 1024 * 1024);
  for (const filename of ['list.m3u8', 'source.mpd', 'page.html', 'image.svg', 'sample.avi']) assert.throws(() => mediaData.mediaFormat(filename, 'video'));
  assert.throws(() => mediaData.mediaFormat('audio.mp3', 'video'));
  assert.throws(() => mediaData.mediaFormat('video.mp4', 'audio'));
});
test('PCM waveform keeps channel separation, duration and non-decimated extrema', () => {
  const value = mediaData.pcmWaveform(wav({ channels: 2, samples: [-1, 0.5, 0, -0.25] }));
  assert.equal(value.channels, 2); assert.equal(value.frames, 2); assert.equal(value.duration, 0.25);
  assert.deepEqual(value.peaks.map(channel => channel.map(peak => peak.max)), [[-1, 0], [0.5, -0.25]]);
});
for (const bits of [8, 16, 24, 32]) test(`PCM ${bits}-bit waveform has signed normalized amplitude`, () => {
  const value = mediaData.pcmWaveform(wav({ bits, samples: [-1, 0, 0.5] }));
  assert.deepEqual(value.peaks[0], [{ min: -1, max: -1 }, { min: 0, max: 0 }, { min: 0.5, max: 0.5 }]);
});
test('float WAV is finite and retains amplitudes outside unit range without silent clipping', () => {
  assert.deepEqual(mediaData.pcmWaveform(wav({ code: 3, bits: 32, samples: [-2, 2] })).peaks[0], [{ min: -2, max: -2 }, { min: 2, max: 2 }]);
  for (const sample of [NaN, Infinity, -Infinity]) assert.throws(() => mediaData.pcmWaveform(wav({ code: 3, bits: 32, samples: [sample] })));
});
test('large sample count uses no more than 2048 min/max bins per channel', () => {
  const samples = Array(8192).fill(0); samples[100] = -1; samples[101] = 0.5;
  const wave = mediaData.pcmWaveform(wav({ sampleRate: 100, samples }));
  assert.equal(wave.peaks[0].length, 2048); assert.deepEqual(wave.peaks[0][25], { min: -1, max: 0.5 });
});
test('WAV rejects chunk overflow, unaligned frames, invalid rate, channels and compression', () => {
  for (const mutate of [view => view.setUint32(4, 9999999, true), view => view.setUint32(40, 9999999, true),
    view => view.setUint16(22, 16, true), view => view.setUint16(20, 17, true), view => view.setUint16(32, 999, true),
    view => view.setUint32(28, 0, true), view => view.setUint32(24, 999999999, true), view => view.setUint16(34, 64, true),
    view => view.setUint32(40, 7, true)]) {
    const buffer = wav(); mutate(new DataView(buffer)); assert.throws(() => mediaData.pcmWaveform(buffer));
  }
  assert.throws(() => mediaData.pcmWaveform(wav({ sampleRate: 1, samples: Array(121).fill(0) })));
  assert.throws(() => mediaData.pcmWaveform(new ArrayBuffer(mediaData.AUDIO_BYTES + 1)));
});
test('WAV refuses duplicate format/data chunks and malformed trailing bytes', () => {
  const original = wav();
  for (const extra of [new Uint8Array(original, 12, 24), new Uint8Array(original, 36), new Uint8Array([0])]) {
    const merged = new Uint8Array(original.byteLength + extra.length); merged.set(new Uint8Array(original)); merged.set(extra, original.byteLength);
    new DataView(merged.buffer).setUint32(4, merged.byteLength - 8, true); assert.throws(() => mediaData.pcmWaveform(merged.buffer));
  }
});
test('media clamps the request budget, does not autoplay and cleans media/blob resources', async t => {
  const created = [], revoked = [], originalCreate = URL.createObjectURL, originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = blob => { created.push(blob); return 'blob:test-1'; }; URL.revokeObjectURL = url => revoked.push(url);
  t.after(() => { URL.createObjectURL = originalCreate; URL.revokeObjectURL = originalRevoke; });
  let budget, pauses = 0, resets = 0;
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: async (_file, plugin) => { budget = plugin.limits.max_input_bytes; return new Uint8Array([1, 2, 3]).buffer; } } });
  view.state.audioElement.value = { pause() { pauses++; }, removeAttribute(name) { assert.equal(name, 'src'); }, load() { resets++; }, play() { assert.fail('Autoplay must not occur'); } };
  await view.start(); assert.equal(budget, mediaData.AUDIO_BYTES); assert.equal(view.state.source.value, 'blob:test-1');
  assert.match(view.state.waveNotice.value, /仅提供原生播放/); assert.equal(created[0].type, 'audio/mpeg');
  view.stop(); assert.equal(pauses, 1); assert.equal(resets, 1); assert.deepEqual(revoked, ['blob:test-1']); assert.equal(view.state.source.value, '');
});
test('media never creates a blob or waveform for a late stopped response', async () => {
  const pending = deferred(); let signal;
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: (_file, _plugin, input) => { signal = input; return pending.promise; } } });
  const start = view.start(); view.stop(); assert.equal(signal.aborted, true); pending.resolve(wav()); await start;
  assert.equal(view.state.source.value, ''); assert.equal(view.state.wave.value, undefined); assert.equal(view.state.error.value, '');
});
test('media cleanup owns real elements even after Vue clears its template refs', async () => {
  let pauses = 0, resets = 0, removed = 0;
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: async () => new Uint8Array([1]).buffer } });
  view.state.audioElement.value = { pause() { pauses++; }, removeAttribute() { removed++; }, load() { resets++; } };
  const canvas = { width: 1024, height: 300 }; view.state.canvas.value = canvas;
  await view.start(); canvas.width = 1024; canvas.height = 300;
  view.state.audioElement.value = undefined; view.state.canvas.value = undefined; view.stop();
  assert.equal(pauses, 1); assert.equal(resets, 1); assert.equal(removed, 1); assert.equal(canvas.width, 0); assert.equal(canvas.height, 0);
});
test('catalog identity-only polling does not repeat media, tree or archive reads', async () => {
  for (const name of ['MediaPreview', 'StructuredTreePreview', 'ArchivePreview']) {
    let calls = 0;
    const dependencies = name === 'MediaPreview' ? { './runtime': { loadPluginBytes: async () => { calls++; return new Uint8Array([1]).buffer; } } }
      : { '../runtime': { requestVisualization: async () => { calls++; return name === 'StructuredTreePreview' ? result('tree', { tree: tree() }) : result('table', { table: archive() }); } } };
    const view = mount(name, dependencies); await view.start(); assert.equal(calls, 1);
    for (let i = 0; i < 3; i++) { view.props.plugin = { ...view.props.plugin, limits: { ...view.props.plugin.limits } }; view.props.file = { ...view.props.file }; await flush(); }
    assert.equal(calls, 1, `${name} must not re-read after a catalog object clone`);
    view.props.plugin = { ...view.props.plugin, version: '2' }; await flush(); assert.equal(calls, 2); view.stop();
  }
});
test('media rejects over-budget bodies independently of runtime and reports unsupported codec', async () => {
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: async () => new ArrayBuffer(mediaData.AUDIO_BYTES + 1) } });
  await view.start(); assert.match(view.state.error.value, /预算/); assert.equal(view.state.source.value, ''); view.stop();
});
test('media rejects over-sized video geometry on metadata without starting playback', async t => {
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: async () => new Uint8Array([1]).buffer } }, {
    file: { file_id: 'file-1', filename: 'local.mp4' }, plugin: { id: 'viz-video', adapter: 'video-player', version: '1', enabled: true, limits: { max_input_bytes: mediaData.VIDEO_BYTES } },
  });
  t.after(view.stop); let paused = 0;
  view.state.videoElement.value = { duration: 1, videoWidth: 10000, videoHeight: 10000, pause() { paused++; }, removeAttribute() {}, load() {} };
  await view.start(); view.state.metadataLoaded(); assert.match(view.state.error.value, /分辨率/); assert.equal(paused, 1);
});
test('media file replacement releases old blobs and ignores superseded requests', async t => {
  const pending = deferred(), signals = [], requested = [];
  const view = mount('MediaPreview', { './runtime': { loadPluginBytes: (file, _plugin, signal) => { requested.push(file.file_id); signals.push(signal); return file.file_id === 'file-1' ? pending.promise : Promise.resolve(new Uint8Array([2]).buffer); } } });
  t.after(view.stop); const start = view.start(); view.props.file = { file_id: 'file-2', filename: 'next.mp3' }; await flush();
  assert.equal(signals[0].aborted, true); const newer = view.state.source.value; assert.match(newer, /^blob:/);
  pending.resolve(wav()); await start; assert.equal(view.state.source.value, newer); assert.deepEqual(requested, ['file-1', 'file-2']);
});
test('tree keeps original numeric lexemes and escaped text, never coerces to JS numbers', () => {
  const parsed = structureData.parseStructuredTree(tree());
  assert.equal(parsed[2].attributes.value, '9007199254740993'); assert.equal(parsed[3].attributes.value, '<script>alert(1)</script>');
  const value = tree(); value[0].attributes.url = 'https://not-a-source';
  assert.equal(structureData.parseStructuredTree(value)[0].attributes.url, undefined);
});
test('actual bounded reader JSON and ZIP payload shapes satisfy the frontend parsers', () => {
  const actualTree = [{ path: '/0', node_type: 'object', attributes: { label: 'root', children_count: 1 } },
    { path: '/0/0', node_type: 'array', attributes: { label: 'counts', children_count: 2 } },
    { path: '/0/0/0', node_type: 'number', attributes: { label: '0', children_count: 0, value: '9007199254740993', numeric_representation: 'source lexeme' } },
    { path: '/0/0/1', node_type: 'number', attributes: { label: '1', children_count: 0, value: '2.5', numeric_representation: 'source lexeme' } }];
  assert.equal(structureData.parseStructuredTree(actualTree)[2].attributes.value, '9007199254740993');
  const actualArchive = { columns: ['成员', '类型', '声明大小（字节）', '压缩大小（字节）', '嵌套压缩包'], rows: [['data.csv', 'file', 7, 7, false]], row_offset: 0, column_offset: 0, total_rows: 1, total_columns: 5 };
  assert.deepEqual(structureData.parseArchiveTable(actualArchive), actualArchive);
  assert.doesNotThrow(() => structureData.parseStructuredTree([node('/0', '😀'.repeat(512))]));
  assert.throws(() => structureData.parseStructuredTree([node('/0', '😀'.repeat(513))]));
});
test('tree search includes ancestors and expanded branches do not require further fetching', () => {
  const parsed = structureData.parseStructuredTree(tree());
  assert.deepEqual(structureData.visibleTree(parsed, new Set(['/0'])).map(item => item.path), ['/0', '/0/0', '/0/1']);
  assert.deepEqual(structureData.visibleTree(parsed, new Set(['/0', '/0/0'])).map(item => item.path), parsed.map(item => item.path));
  assert.deepEqual(structureData.visibleTree(parsed, new Set(), '9007199254740993').map(item => item.path), ['/0', '/0/0', '/0/0/0']);
  assert.equal(structureData.visibleTree(parsed, new Set(), 'no matches').length, 0);
});
test('tree rejects duplicate, path-like, orphan, unknown, overly deep or oversized nodes', () => {
  for (const invalid of [[], Array(257).fill(node('/0', 'x')), [node('/etc/passwd', 'x')], [node('/0', 'x'), node('/0', 'x')],
    [node('/0', 'x'), node('/0/2/1', 'orphan')], [node('/0', 'x', 'javascript')], [node('/0', 'x'.repeat(513))],
    [node('/0', 'x', 'number', 9007199254740992)], [node('/0', 'x', 'object', { execute: true })],
    Array.from({ length: 10 }, (_, i) => node('/0' + '/0'.repeat(i), 'deep'))]) assert.throws(() => structureData.parseStructuredTree(invalid));
});
test('tree component validates, searches locally and ignores late responses after disable', async () => {
  let calls = 0;
  const view = mount('StructuredTreePreview', { '../runtime': { requestVisualization: async () => { calls++; return result('tree', { tree: tree() }); } } });
  await view.start(); view.state.toggle('/0/0'); assert.equal(view.state.visible.value.length, 4);
  view.state.search.value = 'large integer'; await flush(); assert.equal(view.state.visible.value.length, 3); assert.equal(calls, 1); view.stop();
  const pending = deferred(); let signal;
  const stopped = mount('StructuredTreePreview', { '../runtime': { requestVisualization: (_file, _plugin, _operation, _options, input) => { signal = input; return pending.promise; } } });
  const start = stopped.start(); stopped.stop(); pending.resolve(result('tree', { tree: tree() })); await start;
  assert.equal(signal.aborted, true); assert.deepEqual(stopped.state.nodes.value, []);
});
test('archive rejects executable cells, invalid offset, oversized pages and dimensions', () => {
  assert.equal(structureData.parseArchiveTable(archive()).rows.length, 1);
  for (const invalid of [{ ...archive(), rows: [[{ href: 'https://bad' }, 'file']] }, { ...archive(), rows: [['x']] },
    { ...archive(), rows: Array(201).fill(['x', 'file']) }, { ...archive(), row_offset: 4096 }, { ...archive(), column_offset: 1 },
    { ...archive(), total_rows: 0 }, { ...archive(), total_columns: 3 }, { ...archive(), rows: [[Infinity, 'file']] },
    { ...archive(), rows: [['x'.repeat(513), 'file']] }]) assert.throws(() => structureData.parseArchiveTable(invalid));
});
test('archive paging carries the original version and searches only the loaded page', async t => {
  const options = [];
  const view = mount('ArchivePreview', { '../runtime': { requestVisualization: async (_file, _plugin, operation, input) => { assert.equal(operation, 'preview'); options.push(input); return result('table', { table: archive(input.row_offset) }); } } });
  t.after(view.stop); await view.start(); assert.deepEqual(options, [{ row_offset: 0 }]);
  view.state.search.value = 'absent'; await flush(); assert.equal(view.state.rows.value.length, 0); assert.equal(options.length, 1);
  view.state.rowOffset.value = 200; await flush(); assert.deepEqual(options[1], { row_offset: 200, version: 'a'.repeat(64) }); assert.equal(view.state.search.value, '');
  view.props.file = { file_id: 'file-2', filename: 'second.zip' }; await flush(); assert.deepEqual(options[2], { row_offset: 0 });
});
test('archive fails closed on inconsistent returned page and ignores a stopped page', async () => {
  const view = mount('ArchivePreview', { '../runtime': { requestVisualization: async () => result('table', { table: archive(200) }) } });
  await view.start(); assert.match(view.state.error.value, /偏移/); assert.equal(view.state.table.value, undefined); view.stop();
  const pending = deferred(); let signal;
  const stopped = mount('ArchivePreview', { '../runtime': { requestVisualization: (_file, _plugin, _operation, _options, input) => { signal = input; return pending.promise; } } });
  const start = stopped.start(); stopped.stop(); pending.resolve(result('table', { table: archive() })); await start;
  assert.equal(signal.aborted, true); assert.equal(stopped.state.table.value, undefined);
});
test('tree/archive/media templates have no HTML injection, navigation, extraction or autoplay', () => {
  for (const name of ['MediaPreview', 'StructuredTreePreview', 'ArchivePreview']) {
    const source = readFileSync(new URL(`../src/visualizations/extended/${name}.vue`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /v-html|innerHTML|window\.open|\.play\(|\bautoplay\b|localStorage|sessionStorage/);
  }
});
