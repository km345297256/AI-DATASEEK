import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// This exercises the actual component and local Plotly library using only
// synthetic preview responses. Width is the file panel width, not the viewport.
const columnNames = [
  'sample_id',
  ...Array.from({ length: 24 }, (_, index) => `measurement_${String(index + 1).padStart(2, '0')}_${'long_column_name_'.repeat(8)}`),
];
const rows = Array.from({ length: 6 }, (_, row) => [
  `sample-${row + 1}`,
  ...Array.from({ length: 24 }, (_, column) => (row + 1) * (column + 2)),
]);
const fixture = {
  contract_version: 2, type: 'tabular', reader: 'tabular', kind: 'table',
  media_type: 'application/json', metadata: {}, warnings: [], sampled: false,
  table: { columns: columnNames, rows, row_offset: 0, column_offset: 0, total_rows: rows.length, total_columns: columnNames.length },
};

async function plotReady(page) {
  await page.waitForFunction(() => {
    const plot = document.querySelector('.js-plotly-plot');
    return plot?._fullData?.length === 1 && !!plot.querySelector('.scatterlayer path');
  });
  await page.getByRole('status').waitFor({ state: 'hidden' });
}

async function checkLayout(page, width) {
  const layout = await page.evaluate(() => {
    const box = (element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right, bottom: rect.bottom };
    };
    const app = document.querySelector('#app');
    const section = document.querySelector('.plotly-controls').closest('section');
    const controls = document.querySelector('.plotly-controls');
    const chart = document.querySelector('.plotly-chart');
    const list = document.querySelector('.plotly-series-list');
    return {
      panel: box(app),
      section: { ...box(section), clientWidth: section.clientWidth, scrollWidth: section.scrollWidth },
      controls: { ...box(controls), clientHeight: controls.clientHeight, scrollHeight: controls.scrollHeight },
      chart: { ...box(chart), plotWidth: chart._fullLayout?._size?.w, plotHeight: chart._fullLayout?._size?.h },
      list: { ...box(list), clientHeight: list.clientHeight, scrollHeight: list.scrollHeight, clientWidth: list.clientWidth, scrollWidth: list.scrollWidth },
      settings: [...document.querySelectorAll('.plotly-table-settings label, .plotly-table-settings button')].map(box),
      selects: [...document.querySelectorAll('.plotly-table-settings select')].map(box),
      names: [...document.querySelectorAll('.plotly-series-name')].map((name) => ({ ...box(name), title: name.closest('label').title, text: name.textContent.trim(), clientWidth: name.clientWidth, scrollWidth: name.scrollWidth })),
    };
  });
  assert.equal(layout.panel.width, width);
  assert.ok(layout.section.scrollWidth <= layout.section.clientWidth + 1, 'Panel must not overflow horizontally');
  assert.ok(layout.controls.scrollHeight <= layout.controls.clientHeight + 1, 'Controls must retain their full height in a short panel');
  assert.ok(layout.chart.y >= layout.controls.bottom - 1, 'Chart must start after the controls');
  assert.ok(layout.chart.height >= 360, 'Chart retains its minimum usable height');
  assert.ok(layout.chart.plotWidth >= layout.chart.width * 0.5, 'Long legends must leave at least half of the chart width for data');
  assert.ok(layout.chart.plotHeight >= 120, 'Long legends must leave a usable plotting height');
  assert.ok(layout.chart.right <= layout.section.right + 1, 'Chart must fit the panel width');
  assert.ok(layout.list.height >= 64, 'Numeric column choices must remain visible');
  assert.ok(layout.list.height <= 240, 'Many numeric columns must use a bounded list');
  assert.ok(layout.list.scrollHeight > layout.list.clientHeight, 'Long column lists must scroll');
  assert.ok(layout.list.scrollWidth <= layout.list.clientWidth + 1, 'Long column names must not create horizontal scrolling');
  assert.equal(layout.names.length, 24);
  assert.ok(layout.names.every((name) => name.title === name.text && name.title.length > 100), 'Each abbreviated name exposes its full synthetic column title');
  assert.ok(layout.names.some((name) => name.scrollWidth > name.clientWidth), 'Long names should truncate within the panel');
  assert.equal(layout.selects.length, 2);
  assert.ok(layout.selects.every((select) => select.height >= 32 && select.width >= 60), 'Axis and chart controls must remain usable');
  for (const setting of layout.settings) {
    assert.ok(setting.x >= layout.controls.x - 1 && setting.right <= layout.controls.right + 1, 'Every setting must fit inside the controls');
    assert.ok(setting.bottom <= layout.controls.bottom + 1, 'Settings must not extend into the chart');
  }
  for (let left = 0; left < layout.settings.length; left++) {
    for (let right = left + 1; right < layout.settings.length; right++) {
      const a = layout.settings[left], b = layout.settings[right];
      const intersectionWidth = Math.min(a.right, b.right) - Math.max(a.x, b.x);
      const intersectionHeight = Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y);
      assert.ok(intersectionWidth <= 1 || intersectionHeight <= 1, 'Axis, chart mode and draw button must not overlap');
    }
  }
  return layout;
}

async function waitForTraces(page, count, type, mode) {
  await page.waitForFunction(({ count, type, mode }) => {
    const traces = document.querySelector('.js-plotly-plot')?._fullData;
    return traces?.length === count && traces.every((trace) => trace.type === type && (!mode || trace.mode === mode));
  }, { count, type, mode });
}

async function checkInteractions(page) {
  const checkboxes = page.locator('.plotly-series input[type="checkbox"]');
  assert.equal(await checkboxes.count(), 24);
  assert.equal(await checkboxes.nth(0).isChecked(), true);
  await checkboxes.nth(1).check();
  await checkboxes.nth(2).check();
  const settings = page.locator('.plotly-table-settings select');
  await settings.nth(0).selectOption('0');
  await page.getByRole('button', { name: '绘制', exact: true }).click();
  await waitForTraces(page, 3, 'scatter', 'lines');
  const traces = await page.evaluate(() => document.querySelector('.js-plotly-plot')._fullData.map((trace) => ({ x: [...trace.x], y: [...trace.y], name: trace.name, columnName: trace.meta?.columnName })));
  assert.deepEqual(traces.map((trace) => trace.x), Array.from({ length: 3 }, () => rows.map((row) => row[0])));
  assert.deepEqual(traces.map((trace) => trace.y), [1, 2, 3].map((column) => rows.map((row) => row[column])));
  assert.deepEqual(traces.map((trace) => trace.columnName), columnNames.slice(1, 4));
  assert.deepEqual(traces.map((trace) => trace.name), columnNames.slice(1, 4).map((name, index) => `#${index + 2} ${name.slice(0, 18)}…`));

  for (let index = 3; index < 8; index++) await checkboxes.nth(index).check();
  assert.equal(await page.locator('.plotly-series input[type="checkbox"]:checked').count(), 8);
  assert.equal(await page.locator('.plotly-series input[type="checkbox"]:disabled').count(), 16);
  assert.equal(await checkboxes.nth(0).isEnabled(), true, 'Selected columns must remain removable at the limit');
  await page.getByRole('button', { name: '绘制', exact: true }).click();
  await waitForTraces(page, 8, 'scatter', 'lines');
  await checkboxes.nth(7).uncheck();
  assert.equal(await checkboxes.nth(8).isEnabled(), true, 'Removing a selection releases the eight-column limit');
  await checkboxes.nth(8).check();

  await settings.nth(1).selectOption('markers');
  await page.getByRole('button', { name: '绘制', exact: true }).click();
  await waitForTraces(page, 8, 'scatter', 'markers');
  await settings.nth(1).selectOption('histogram');
  await page.getByRole('button', { name: '绘制', exact: true }).click();
  await waitForTraces(page, 8, 'histogram');
  const histogramValues = await page.evaluate(() => [...document.querySelector('.js-plotly-plot')._fullData[0].x]);
  assert.deepEqual(histogramValues, rows.map((row) => row[1]));
  await settings.nth(1).selectOption('lines');
  await page.getByRole('button', { name: '绘制', exact: true }).click();
  await waitForTraces(page, 8, 'scatter', 'lines');
  await page.evaluate(() => {
    const plot = document.querySelector('.js-plotly-plot');
    globalThis.Plotly.Fx.hover(plot, [{ curveNumber: 0, pointNumber: 2 }]);
  });
  await page.waitForFunction((name) => document.querySelector('.hoverlayer')?.textContent.includes(name), columnNames[1]);
  assert.ok((await page.locator('.hoverlayer').textContent()).includes(columnNames[1]), 'Hover shows the complete column name despite the abbreviated legend');
  await page.evaluate(() => globalThis.Plotly.Fx.unhover(document.querySelector('.js-plotly-plot')));
  return { selectedColumns: 8, numericColumns: 24, modes: ['lines', 'markers', 'histogram'], tracesVerified: true, fullColumnHoverVerified: true };
}

export const plotlyLayoutCases = [
  { width: 360, height: 500 },
  { width: 600, height: 700 },
  { width: 1000, height: 500 },
].map(({ width, height }) => ({
  name: `plotly-layout-${width}`, component: 'scientific/PlotlyPreview.vue', filename: 'synthetic-layout.csv', reader: 'tabular', kind: 'series',
  bytes: Buffer.from('sample_id,value\nsample-1,2\n', 'utf8'), preview: fixture,
  async setup(page) {
    await page.locator('#app').evaluate((app, { width, height }) => {
      app.style.width = `${width}px`; app.style.height = `${height}px`; app.style.margin = '0';
    }, { width, height });
  },
  ready: plotReady,
  async verify(page) {
    assert.deepEqual(await page.locator('[role="alert"]').allTextContents(), []);
    const initial = await checkLayout(page, width);
    const interaction = await checkInteractions(page);
    const after = await checkLayout(page, width);
    const chartScreenshot = join(await mkdtemp(join(tmpdir(), 'dataseek-plotly-layout-')), `plotly-layout-${width}-chart.png`);
    await page.locator('.plotly-chart').screenshot({ path: chartScreenshot });
    await page.locator('.plotly-series-list').evaluate((element) => { element.scrollTop = 0; });
    await page.locator('.plotly-controls').evaluate((element) => { element.closest('section').scrollTop = 0; });
    return { width, height, controlsHeight: initial.controls.height, chartHeight: after.chart.height, plotWidth: after.chart.plotWidth, plotHeight: after.chart.plotHeight, horizontalOverflow: after.section.scrollWidth - after.section.clientWidth, chartScreenshot, ...interaction };
  },
}));

const arrayVariable = `synthetic_array_${'long_variable_name_'.repeat(7)}`;
plotlyLayoutCases.push({
  name: 'plotly-layout-array-360', component: 'scientific/PlotlyPreview.vue', filename: 'synthetic-layout.npy', reader: 'tabular', kind: 'series',
  bytes: Buffer.from('synthetic-array-reader-fixture', 'utf8'),
  preview(request) {
    const heatmap = request.kind === 'heatmap';
    const offset = request.options.indices.reduce((sum, index) => sum + index, 0);
    return {
      contract_version: 2, type: 'tabular', reader: 'tabular', kind: request.kind,
      media_type: 'application/json', warnings: [], sampled: false,
      metadata: { source_shape: [2, 3, 4, 5, 6, 12], strides: [1, 1] },
      choices: { variables: [arrayVariable] }, selected: { variable: arrayVariable },
      array: { shape: heatmap ? [6, 12] : [12], values: Array.from({ length: heatmap ? 72 : 12 }, (_, index) => index + 1 + offset) },
    };
  },
  async setup(page) {
    await page.locator('#app').evaluate((app) => { app.style.width = '360px'; app.style.height = '500px'; app.style.margin = '0'; });
  },
  ready: plotReady,
  async verify(page) {
    assert.deepEqual(await page.locator('[role="alert"]').allTextContents(), []);
    const checkArrayLayout = async () => {
      const layout = await page.evaluate(() => {
        const form = document.querySelector('.plotly-array-settings'), section = form.closest('section'), chart = document.querySelector('.plotly-chart');
        const rect = (element) => { const box = element.getBoundingClientRect(); return { x: box.x, right: box.right, y: box.y, bottom: box.bottom, height: box.height }; };
        return { form: { ...rect(form), scrollHeight: form.scrollHeight, clientHeight: form.clientHeight }, section: { scrollWidth: section.scrollWidth, clientWidth: section.clientWidth }, chart: rect(chart), inputs: [...form.querySelectorAll('select, input, button')].map(rect) };
      });
      assert.ok(layout.section.scrollWidth <= layout.section.clientWidth + 1, 'Long array variables must fit in the narrow panel');
      assert.ok(layout.form.scrollHeight <= layout.form.clientHeight + 1, 'Slice controls must retain their full height');
      assert.ok(layout.chart.y >= layout.form.bottom - 1, 'Array chart must not overlap slice controls');
      assert.ok(layout.chart.height >= 360);
      assert.ok(layout.inputs.every((input) => input.height >= 32 && input.x >= layout.form.x - 1 && input.right <= layout.form.right + 1 && input.bottom <= layout.form.bottom + 1), 'All array controls must remain visible and bounded');
      return { controlsHeight: layout.form.bottom - layout.form.y, chartHeight: layout.chart.height };
    };
    await checkArrayLayout();
    const sliceInputs = page.locator('.plotly-array-settings input[type="number"]');
    assert.equal(await sliceInputs.count(), 5);
    await sliceInputs.nth(0).fill('1');
    await page.getByRole('button', { name: '读取变量／切片', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.js-plotly-plot')?._fullData?.[0]?.y?.[0] === 2);
    assert.deepEqual(await page.evaluate(() => [...document.querySelector('.js-plotly-plot')._fullData[0].y]), Array.from({ length: 12 }, (_, index) => index + 2));
    await page.locator('.plotly-array-settings select').nth(1).selectOption('heatmap');
    assert.equal(await sliceInputs.count(), 4);
    await page.getByRole('button', { name: '读取变量／切片', exact: true }).click();
    await waitForTraces(page, 1, 'heatmap');
    await page.getByRole('status').waitFor({ state: 'hidden' });
    const heatmap = await page.evaluate(() => document.querySelector('.js-plotly-plot')._fullData[0].z);
    assert.equal(heatmap.length, 6); assert.equal(heatmap[0].length, 12); assert.equal(heatmap[0][0], 1);
    const layout = await checkArrayLayout();
    await page.locator('.plotly-controls').evaluate((element) => { element.closest('section').scrollTop = 0; });
    return { width: 360, height: 500, dimensions: 6, sliceValuesVerified: true, heatmapVerified: true, ...layout };
  },
});
