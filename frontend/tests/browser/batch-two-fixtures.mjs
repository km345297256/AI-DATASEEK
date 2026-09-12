/** Real compiled Vue + local Plotly; all HTTP is synthetic and intercepted. */
import assert from 'node:assert/strict';
import { signalFixture, mcaFixture, archiveFixture, memberId, version } from '../batchTwoFixtures.mjs';
function scenario(name, component, adapter, reader, filename, fixture) {
  const calls = [];
  return { name, component, reader, filename, descriptor: { adapter },
    api(request) {
      assert.equal(request.method(), 'POST'); assert.equal(new URL(request.url()).pathname, `/api/v1/files/synthetic-${name}/visualization`);
      const body = request.postDataJSON(); assert.equal(body.operation, 'preview'); assert.equal(body.plugin_id, `test-${name}`); calls.push(body);
      return { contentType: 'application/json', body: JSON.stringify({ code: 0, data: { contract_version: 2, plugin_id: `test-${name}`, ...fixture(body.options) } }) };
    }, calls };
}
const readyPlot = page => page.waitForFunction(() => document.querySelector('.js-plotly-plot')?._fullData?.length > 0);
const beforeUnmount = page => page.evaluate(() => { window.__heldPlot = document.querySelector('.js-plotly-plot'); });
async function verifyCleanup(page) {
  const result = await page.evaluate(() => ({ removed: !window.__heldPlot?.isConnected, purged: !window.__heldPlot?._fullData }));
  assert.equal(result.removed, true); assert.equal(result.purged, true); return result;
}
const signal = scenario('batch-two-signals', 'SignalWindowPreview.vue', 'signal-window', 'edf', 'record.edf', signalFixture);
signal.file = { size: 2500000768 }; signal.ready = readyPlot; signal.beforeUnmount = beforeUnmount; signal.verifyCleanup = verifyCleanup;
signal.verify = async page => {
  assert.equal(signal.calls.length, 1); assert.deepEqual(signal.calls[0].options, {});
  await page.locator('input[type=checkbox]').nth(1).check(); await page.getByRole('spinbutton', { name: '起点（秒）' }).fill('5');
  await page.getByRole('spinbutton', { name: '时长（秒）' }).fill('2');
  assert.equal(signal.calls.length, 1); await page.getByRole('button', { name: '读取时间窗' }).click();
  await page.waitForFunction(() => document.querySelector('.js-plotly-plot')?._fullData?.length === 2);
  assert.equal(signal.calls.length, 2); assert.equal(signal.calls[1].version, version);
  assert.deepEqual(signal.calls[1].options, { channels: [0, 1], start_seconds: 5, duration_seconds: 2 });
  const data = await page.locator('.js-plotly-plot').evaluate(element => element.data.map(trace => ({ n: trace.x.length, first: trace.x[0], xaxis: trace.xaxis, yaxis: trace.yaxis })));
  assert.deepEqual(data, [{ n: 8, first: 5, xaxis: 'x', yaxis: 'y' }, { n: 4, first: 5, xaxis: 'x2', yaxis: 'y2' }]);
  assert.deepEqual(await page.locator('.js-plotly-plot').evaluate(element => [element.layout.xaxis.range, element.layout.xaxis2.range]), [[5, 7], [5, 7]]);
  assert.equal(await page.locator('input[type=checkbox]').nth(2).isDisabled(), true);
  return { explicitWindowRequest: true, distinctAxes: true, ratesPreserved: [4, 2], versionPinned: true };
};
const mca = scenario('batch-two-mca', 'McaSpectrumPreview.vue', 'mca-spectrum', 'mca', 'spectrum.mca', () => mcaFixture(false));
mca.ready = readyPlot; mca.beforeUnmount = beforeUnmount; mca.verifyCleanup = verifyCleanup;
mca.verify = async page => {
  const data = await page.locator('.js-plotly-plot').evaluate(element => ({ x: element.data[0].x, y: element.data[0].y, axis: element.layout.xaxis.title.text }));
  assert.deepEqual(data.x, [0, 1, 2, 3]); assert.equal(data.y[3], Number.MAX_SAFE_INTEGER); assert.equal(data.axis, '通道索引');
  assert.equal(mca.calls.length, 1); return { exactCounts: true, axis: data.axis };
};
const archive = scenario('batch-two-members', 'ArchiveMembersPreview.vue', 'archive-members', 'archive-member', 'texts.zip', archiveFixture);
archive.ready = page => page.getByRole('button', { name: '查看文本' }).waitFor();
archive.verify = async page => {
  assert.equal(archive.calls.length, 1); await page.getByRole('button', { name: '查看文本' }).click();
  await page.getByRole('heading', { name: 'samples.txt' }).waitFor();
  assert.equal(archive.calls.length, 2); assert.equal(archive.calls[1].version, version); assert.equal(archive.calls[1].options.member_id, memberId);
  assert.equal(await page.evaluate(() => window.__archiveExecuted), undefined); assert.equal(await page.locator('#app script').count(), 0);
  assert.ok((await page.locator('#app').innerText()).includes('<script>window.__archiveExecuted=true</script>'));
  await page.getByRole('button', { name: '下一页' }).click(); await page.waitForFunction(() => document.querySelector('#app').textContent.includes('201–201 / 201'));
  assert.equal(archive.calls[2].version, version); assert.equal(archive.calls[2].options.row_offset, 200);
  await page.getByRole('button', { name: '返回归档目录' }).click(); await page.getByRole('button', { name: '查看文本' }).waitFor();
  assert.equal(archive.calls.length, 4); assert.equal(archive.calls[3].options.member_id, undefined);
  return { directoryDoesNotDecode: true, explicitMember: true, scriptInert: true, paginationPinned: true };
};
export const batchTwoCases = [signal, mca, archive];
