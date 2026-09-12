import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { usePreviewLoad } from '../src/composables/usePreviewLoad.ts';

const adapters = [
  ['MolstarPreview', 'scientific/MolstarPreview.vue'],
  ['JsRootPreview', 'scientific/JsRootPreview.vue'],
  ['NmriumPreview', 'scientific/NmriumPreview.vue'],
  ['VtkPreview', 'scientific/VtkPreview.vue'],
  ['DocxPreview', 'DocxPreview.vue'],
];
const clone = value => JSON.parse(JSON.stringify(value));
const flush = async () => {
  await vue.nextTick();
  for (let index = 0; index < 12; index++) await Promise.resolve();
};

// Run each actual production SFC setup with Vue's real watchers and disposal
// scope. Only the first I/O is held pending: no browser library is downloaded
// or instantiated, and the component's existing abort/error paths still run.
function mountAdapter(t, name, path) {
  const source = readFileSync(new URL(`../src/visualizations/extended/${path}`, import.meta.url), 'utf8');
  const parsed = parse(source, { filename: path });
  assert.deepEqual(parsed.errors, []);
  const script = compileScript(parsed.descriptor, { id: `watcher-stability-${name}` });
  const compiled = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    reportDiagnostics: true,
  });
  assert.deepEqual(compiled.diagnostics, []);

  const requests = [];
  const pendingRead = (file, plugin, signal) => {
    requests.push({ fileId: file.file_id, pluginId: plugin.id, signal });
    return new Promise((_resolve, reject) => {
      const abort = () => reject(new DOMException('Cancelled', 'AbortError'));
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, { once: true });
    });
  };
  const unexpected = () => assert.fail('Pending reads must not reach rendering or browser libraries');
  const dependencies = {
    vue,
    '../../../composables/usePreviewLoad': { usePreviewLoad },
    '../../composables/usePreviewLoad': { usePreviewLoad },
    '../runtime': {
      loadPluginBytes: pendingRead,
      requestPreview: (file, plugin, _options, signal) => pendingRead(file, plugin, signal),
    },
    './data': { assertMolecularText: unexpected, assertVtkInput: unexpected, safeText: unexpected, numericPairs: unexpected },
    './rootData': { buildRootNumericObject: unexpected },
    './browserLibraries': { loadBrowserLibrary: unexpected, loadNmrBrowserLibrary: unexpected },
    './docxBootstrap': { loadDocxBootstrap: async () => 'synthetic bootstrap' },
    './PreviewFrame.vue': { default: {} },
    './scientific/PreviewFrame.vue': { default: {} },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled.outputText)((id) => {
    assert.ok(id in dependencies, `Unexpected setup dependency ${id}`);
    return dependencies[id];
  }, module, module.exports);

  const props = vue.reactive({
    file: { file_id: 'file-a', filename: 'synthetic.dat', upload_date: '', metadata: { logical_path: 'synthetic.dat' } },
    plugin: { id: 'plugin-a', version: '1.0.0', enabled: true, reader: { id: 'synthetic-reader' } },
  });
  const scope = vue.effectScope();
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  t.after(() => scope.stop());
  return { props, state, requests, unmount: () => scope.stop() };
}

for (const [name, path] of adapters) {
  test(`${name}: equivalent file/plugin refreshes do not restart the current load`, async t => {
    const preview = mountAdapter(t, name, path);
    assert.equal(preview.requests.length, 1, 'the initial load remains immediate');
    const first = preview.requests[0];

    // Exercise independent polling updates and Vue-batched prop updates.
    for (let refresh = 0; refresh < 3; refresh++) {
      preview.props.plugin = clone(preview.props.plugin);
      await flush();
      preview.props.file = clone(preview.props.file);
      await flush();
      preview.props.file = clone(preview.props.file);
      preview.props.plugin = clone(preview.props.plugin);
      await flush();
    }
    assert.equal(preview.requests.length, 1);
    assert.equal(first.signal.aborted, false);
    assert.equal(preview.state.loading.value, true);
    assert.equal(preview.state.error.value, '');

    preview.unmount();
    await flush();
    assert.equal(first.signal.aborted, true, 'unmount still cancels the current read');
  });

  test(`${name}: changed identities reload, abort obsolete reads, and stop on unmount`, async t => {
    const preview = mountAdapter(t, name, path);
    preview.props.file = { ...clone(preview.props.file), file_id: 'file-b' };
    await flush();
    assert.equal(preview.requests.length, 2);
    assert.deepEqual(preview.requests.map(request => request.fileId), ['file-a', 'file-b']);
    assert.equal(preview.requests[0].signal.aborted, true);
    assert.equal(preview.requests[1].signal.aborted, false);

    preview.props.plugin = { ...clone(preview.props.plugin), id: 'plugin-b' };
    await flush();
    assert.equal(preview.requests.length, 3);
    assert.deepEqual(preview.requests.map(request => request.pluginId), ['plugin-a', 'plugin-a', 'plugin-b']);
    assert.equal(preview.requests[1].signal.aborted, true);
    assert.equal(preview.requests[2].signal.aborted, false);
    assert.equal(preview.state.loading.value, true, 'obsolete aborts do not end the new load');
    assert.equal(preview.state.error.value, '', 'obsolete aborts do not become visible failures');

    preview.unmount();
    await flush();
    assert.ok(preview.requests.every(request => request.signal.aborted));
    preview.props.file = { ...preview.props.file, file_id: 'file-c' };
    preview.props.plugin = { ...preview.props.plugin, id: 'plugin-c' };
    await flush();
    assert.equal(preview.requests.length, 3, 'disposed watchers must not start more reads');
  });
}
