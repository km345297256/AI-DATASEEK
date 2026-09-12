/** Original synthetic topology normalized by the real sandbox reader.
 * Uses actual Cytoscape canvases/core; no mocked SDK or live production API.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-graph-data.json', import.meta.url), 'utf8'));
let requests = [];
export const domainExpansionGraphCases = [
  { name: 'domain-expansion-graph', component: 'ScientificGraphPreview.vue', filename: 'synthetic-protein-network.json', reader: 'scientific-graph', descriptor: { adapter: 'scientific-graph' }, file: { size: data.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => { requests.push(request); assert.equal(request.kind, 'graph'); assert.deepEqual(request.options, {}); return data; },
    ready: async page => { await page.waitForFunction(() => document.querySelector('.graph-viewport')?._cyreg?.cy?.nodes().length === 3); await page.waitForTimeout(100); },
    verify: async page => {
      const initial = await page.locator('.graph-viewport').evaluate(container => {
        const cy = container._cyreg.cy;
        const painted = [...container.querySelectorAll('canvas')].map(canvas => {
          const context = canvas.getContext('2d'); if (!context) return 0;
          const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
          let count = 0; for (let i = 0; i < pixels.length; i += 4) if (pixels[i + 3] > 0 && pixels[i + 1] > pixels[i] + 20 && pixels[i + 1] > pixels[i + 2] + 10) count++;
          return count;
        });
        return { nodes: cy.nodes().length, edges: cy.edges().length, position: cy.getElementById('n0').position(), renderedPosition: cy.getElementById('n0').renderedPosition(), painted };
      });
      assert.equal(initial.nodes, 3); assert.equal(initial.edges, 2); assert.ok(initial.painted.some(value => value > 30), 'Actual canvas must contain green node pixels');
      const bounds = await page.locator('.graph-viewport').boundingBox(); assert.ok(bounds.width > 400 && bounds.height >= 300);
      await page.mouse.click(bounds.x + initial.renderedPosition.x, bounds.y + initial.renderedPosition.y);
      await page.getByText('节点：TP53', { exact: false }).waitFor();
      await page.getByLabel('节点搜索').fill('regulator');
      await page.waitForFunction(() => document.querySelector('.graph-viewport')._cyreg.cy.getElementById('n0').hasClass('highlight'));
      assert.equal(await page.locator('[role=status]').last().textContent(), '1 个匹配');
      await page.getByLabel('布局', { exact: true }).selectOption('grid');
      const grid = await page.locator('.graph-viewport').evaluate(container => container._cyreg.cy.getElementById('n0').position());
      assert.notDeepEqual(grid, initial.position); assert.equal(requests.length, 1, 'Layout and selection do not redownload the source');
      await page.getByRole('button', { name: '适应视口', exact: true }).click();
      return { actualCytoscape: true, exactNodeEdgeCounts: [3, 2], canvasNodePixels: initial.painted, circleAndGridDifferent: true, nodeHitTesting: true, fieldSearch: true, sameAuthorizedRead: requests.length, viewport: bounds };
    },
    beforeUnmount: page => page.evaluate(() => { window.__heldScientificGraph = document.querySelector('.graph-viewport')._cyreg.cy; }),
    verifyCleanup: async page => {
      const cleanup = await page.evaluate(() => ({ destroyed: window.__heldScientificGraph.destroyed(), canvasesDetached: [...window.__canvasReferences].every(canvas => !canvas.isConnected) }));
      assert.equal(cleanup.destroyed, true); assert.equal(cleanup.canvasesDetached, true); return cleanup;
    },
  },
  { name: 'domain-expansion-graph-unsafe', component: 'ScientificGraphPreview.vue', filename: 'synthetic-unsafe-network.json', reader: 'scientific-graph', descriptor: { adapter: 'scientific-graph' }, file: { size: data.metadata.source_bytes },
    preview: () => { const bad = structuredClone(data); bad.graph.nodes[0].label = '<img src="https://example.invalid/track">'; return bad; },
    expectedError: /科学网络图响应未通过/,
    ready: page => page.locator('[role=alert]').waitFor(),
    verify: async page => { assert.equal(await page.locator('canvas').count(), 0); assert.equal(await page.locator('img').count(), 0); return { rejectedBeforeSdk: true, noHtmlOrExternalResource: true }; },
  },
  { name: 'domain-expansion-graph-wrong-kind', component: 'ScientificGraphPreview.vue', filename: 'synthetic-wrong-kind.json', reader: 'scientific-graph', descriptor: { adapter: 'scientific-graph' }, file: { size: data.metadata.source_bytes },
    api: async request => {
      assert.equal(request.method(), 'POST'); assert.equal(request.postDataJSON().operation, 'preview');
      return { contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { contract_version: 2, plugin_id: 'test-domain-expansion-graph-wrong-kind', kind: 'table', version: '1'.repeat(64), revision: '2'.repeat(64), payload: { graph: data.graph, media_type: 'application/json', view_kind: 'graph' }, metadata: data.metadata, warnings: data.warnings, sampled: false } }) };
    },
    expectedError: /科学网络图响应类型不一致/,
    ready: page => page.locator('[role=alert]').waitFor(),
    verify: async page => { assert.equal(await page.locator('canvas').count(), 0); return { wrongPublicKindRejected: true, rejectedBeforeSdk: true }; },
  },
];
