/** Real reader-produced, original synthetic profiles. Real Plotly, no business APIs. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-diffraction-data.json', import.meta.url), 'utf8'));
const rendered = page => page.waitForFunction(() => { const el = document.querySelector('.js-plotly-plot'); return !!el?.data?.length && !!el._fullLayout; });
export const domainExpansionDiffractionCases = ['xrdml', 'sas', 'explicit'].map(which => {
  const fixture = data[which]; let requests = [];
  return { name: `domain-expansion-diffraction-${which}`, component: 'DiffractionPreview.vue', filename: `synthetic.${which === 'sas' ? 'xml' : 'xrdml'}`, reader: 'diffraction',
    descriptor: { adapter: 'diffraction', view_kind: 'series', limits: { max_input_bytes: 16777216, max_output_bytes: 2097152 }, capabilities: { operations: ['preview'], input_mode: 'whole', shared: false } },
    file: { size: fixture.tree.metadata.source_bytes }, init: async () => { requests = []; },
    preview: request => { requests.push(request); if (request.kind === 'tree') { assert.deepEqual(request.options, {}); return fixture.tree; }
      assert.equal(request.kind, 'series'); assert.equal(request.version, '1'.repeat(64)); assert.deepEqual(request.options, { scan: 1 }); return fixture.series; },
    ready: page => page.getByRole('button', { name: '绘制所选扫描', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(requests.length, 1); assert.equal(await page.locator('.js-plotly-plot').count(), 0);
      await page.getByLabel('衍射扫描', { exact: true }).selectOption('1'); assert.equal(requests.length, 1);
      await page.getByRole('button', { name: '绘制所选扫描', exact: true }).click(); await rendered(page);
      const actual = await page.locator('.js-plotly-plot').evaluate(el => ({ x: el.data[0].x, y: el.data[0].y, dx: el.data[0].error_x?.array ?? null,
        dy: el.data[0].error_y?.array ?? null, xTitle: el._fullLayout.xaxis.title.text, yTitle: el._fullLayout.yaxis.title.text,
        yType: el._fullLayout.yaxis.type, curveLength: el.querySelector('.js-line')?.getTotalLength(), points: el.querySelectorAll('.point').length,
        errorsX: el.querySelectorAll('.xerror').length, errorsY: el.querySelectorAll('.yerror').length, bounds: el.getBoundingClientRect().toJSON() }));
      const expected = fixture.series.series[0]; assert.deepEqual(actual.x, expected.x); assert.deepEqual(actual.y, expected.y); assert.deepEqual(actual.dx, expected.x_error); assert.deepEqual(actual.dy, expected.y_error);
      assert.equal(actual.points, 4); assert.ok(actual.curveLength > 100); assert.ok(actual.bounds.width > 400 && actual.bounds.height >= 400); assert.equal(actual.yType, 'linear');
      if (which === 'sas') { assert.equal(actual.xTitle, 'Q (1/A)'); assert.ok(actual.errorsX >= 4 && actual.errorsY >= 4); assert.match(await page.getByTestId('diffraction-errors').textContent(), /标准差倍数未指定/); }
      else { assert.equal(actual.xTitle, '2Theta (deg)'); assert.equal(actual.dx, null); assert.equal(actual.dy, null); assert.match(actual.yTitle, /counts/); }
      assert.match(await page.getByRole('note').textContent(), /不按计数时间归一化/); assert.deepEqual(await page.locator('[role=alert]').allTextContents(), []);
      assert.equal(requests.length, 2); return { realPlotly: true, metadataOnlyInitialResponse: true, explicitVersionPinnedScan: true, rawUnconverted: actual };
    },
    beforeUnmount: page => page.evaluate(() => { window.__diffractionPlot = document.querySelector('.js-plotly-plot'); }),
    verifyCleanup: async page => { const value = await page.evaluate(() => ({ detached: !window.__diffractionPlot.isConnected, purged: !window.__diffractionPlot.data })); assert.deepEqual(value, { detached: true, purged: true }); return value; },
  };
});
domainExpansionDiffractionCases.push({ name: 'domain-expansion-diffraction-unsafe-unit', component: 'DiffractionPreview.vue', filename: 'synthetic.xml', reader: 'diffraction',
  descriptor: { adapter: 'diffraction', view_kind: 'series' }, file: { size: data.sas.tree.metadata.source_bytes },
  preview: () => { const value = structuredClone(data.sas.tree); value.choices.scans[0].x_unit = '<img src="https://example.invalid/track">'; return value; },
  expectedError: /衍射或散射响应未通过/, ready: page => page.locator('[role=alert]').waitFor(),
  verify: async page => { assert.equal(await page.locator('.js-plotly-plot').count(), 0); assert.equal(await page.locator('img').count(), 0); return { unsafeUnitRejectedBeforePlot: true }; },
});
