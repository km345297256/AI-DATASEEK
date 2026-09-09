import assert from 'node:assert/strict';
import { test } from 'node:test';
import { documentGeometry, DOCUMENT_MAX_CANVAS_PIXELS, DOCUMENT_MAX_CANVAS_SIDE } from '../src/visualizations/extended/documentGeometry.ts';

const input = { pageWidth: 360, pageHeight: 240, zoom: 1, availableWidth: 1000, availableHeight: 650, devicePixelRatio: 1 };
test('100% PDF geometry separates 96 CSS dpi from DPR 1, 2 and 3 backing pixels', () => {
  for (const devicePixelRatio of [1, 2, 3]) {
    const geometry = documentGeometry({ ...input, devicePixelRatio });
    assert.equal(geometry.cssWidth, 480); assert.equal(geometry.cssHeight, 320);
    assert.equal(geometry.pixelWidth, 480 * devicePixelRatio); assert.equal(geometry.pixelHeight, 320 * devicePixelRatio);
    assert.equal(geometry.scaleX, devicePixelRatio); assert.equal(geometry.budgetLimited, false);
  }
});
test('50%–400% zoom changes PDF raster dimensions, not only the CSS size', () => {
  for (const zoom of [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4]) {
    const geometry = documentGeometry({ ...input, zoom, devicePixelRatio: 2 });
    assert.equal(geometry.cssWidth, 480 * zoom);
    if (!geometry.budgetLimited) assert.equal(geometry.pixelWidth, 960 * zoom);
    else assert.ok(geometry.pixelWidth > geometry.cssWidth);
  }
});
test('fit-width and fit-page account for both container dimensions', () => {
  assert.equal(documentGeometry({ ...input, zoom: 'fit-width', availableWidth: 600 }).cssWidth, 600);
  const page = documentGeometry({ ...input, zoom: 'fit-page', availableWidth: 600, availableHeight: 200 });
  assert.equal(page.cssHeight, 200); assert.equal(page.cssWidth, 300);
});
test('high DPI and huge pages cap allocation without silently changing requested zoom', () => {
  for (const [pageWidth, pageHeight] of [[360, 240], [2000, 400], [500, 5000]]) {
    const geometry = documentGeometry({ ...input, pageWidth, pageHeight, zoom: 4, devicePixelRatio: 3 });
    assert.equal(geometry.cssWidth, pageWidth * 96 / 72 * 4);
    assert.ok(geometry.pixelWidth * geometry.pixelHeight <= DOCUMENT_MAX_CANVAS_PIXELS);
    assert.ok(Math.max(geometry.pixelWidth, geometry.pixelHeight) <= DOCUMENT_MAX_CANVAS_SIDE);
    assert.equal(geometry.budgetLimited, true);
  }
});
test('invalid, nonfinite and unbounded geometry is rejected before canvas allocation', () => {
  for (const extra of [{ pageWidth: 0 }, { pageHeight: NaN }, { pageWidth: Infinity }, { availableWidth: 0 }, { zoom: 0.4 }, { zoom: 5 }, { pageWidth: 1e10 }]) {
    assert.throws(() => documentGeometry({ ...input, ...extra }));
  }
  assert.equal(documentGeometry({ ...input, devicePixelRatio: NaN }).scaleX, 1);
});
