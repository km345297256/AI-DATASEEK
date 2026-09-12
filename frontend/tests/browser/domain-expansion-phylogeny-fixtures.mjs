/** Original synthetic Newick normalized by the real isolated-reader code.
 * The real Vue/SVG renderer is exercised; no network or production API use.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const data = JSON.parse(readFileSync(new URL('./domain-expansion-phylogeny-data.json', import.meta.url), 'utf8'));
let requests = [];
const base = { component: 'PhylogenyPreview.vue', filename: 'synthetic-tree.nwk', reader: 'phylogeny', descriptor: { adapter: 'phylogeny' } };
export const domainExpansionPhylogenyCases = [
  { ...base, name: 'domain-expansion-phylogeny', file: { size: data.complete.metadata.source_bytes },
    init: async () => { requests = []; },
    preview: request => { requests.push(request); assert.equal(request.kind, 'tree'); assert.deepEqual(request.options, {}); return data.complete; },
    ready: page => page.locator('[data-testid=phylogeny-svg]').waitFor(),
    verify: async page => {
      const svg = page.locator('[data-testid=phylogeny-svg]');
      const initial = await svg.evaluate(el => ({ nodes: el.querySelectorAll('[data-node]').length, edges: el.querySelectorAll('path[data-testid=phylogeny-branch]').length,
        lengths: [...el.querySelectorAll('path[data-testid=phylogeny-branch]')].map(p => p.getTotalLength()), view: el.getBoundingClientRect().toJSON(), human: el.querySelector('[data-node=n2]').getAttribute('transform') }));
      assert.equal(initial.nodes, 5); assert.equal(initial.edges, 4); assert.ok(initial.lengths.every(n => n > 25)); assert.ok(initial.view.width >= 600 && initial.view.height >= 100);
      assert.equal(initial.human, 'translate(632,32)');
      await page.getByLabel('系统树视图', { exact: true }).selectOption('length');
      await page.getByTestId('phylogeny-scale').waitFor(); assert.match(await page.getByTestId('phylogeny-scale').textContent(), /0–0.8/);
      assert.equal(await page.locator('[data-node=n2]').getAttribute('transform'), 'translate(332,32)');
      await page.getByRole('button', { name: '节点 95', exact: true }).click();
      assert.match(await page.getByTestId('phylogeny-selection').textContent(), /枝长原文：0.3/);
      await page.getByRole('button', { name: '折叠所选分支', exact: true }).click();
      assert.equal(await page.locator('[data-node]').count(), 3); assert.equal(await page.locator('[data-node=n2]').count(), 0);
      await page.getByLabel('系统树标签搜索').fill('human');
      await page.getByRole('button', { name: '定位下一个', exact: true }).click();
      assert.equal(await page.locator('[data-node]').count(), 5); assert.match(await page.getByTestId('phylogeny-selection').textContent(), /节点：Human/);
      const point = await page.locator('[data-node=n2]').boundingBox(); assert.ok(point && point.width > 20 && point.height > 5);
      assert.equal(requests.length, 1, 'Mode/search/collapse reuse the same authorized tree');
      return { realSvg: true, actualVisiblePathLengths: initial.lengths, originalInternalLabel: '95', branchScale: 0.8, exactSelectedLength: '0.3', collapsedCount: 3, searchedReexpandedCount: 5, sameAuthorizedRead: requests.length, initialGeometry: initial.view };
    },
    beforeUnmount: page => page.evaluate(() => { window.__phylogenySvg = document.querySelector('[data-testid=phylogeny-svg]'); }),
    verifyCleanup: async page => { const detached = await page.evaluate(() => !window.__phylogenySvg.isConnected && !document.querySelector('[data-testid=phylogeny-svg]')); assert.equal(detached, true); return { svgDetached: detached, noSdkWorkersOrBlobs: true }; },
  },
  { ...base, name: 'domain-expansion-phylogeny-missing', file: { size: data.missing.metadata.source_bytes },
    preview: () => data.missing, ready: page => page.locator('[data-testid=phylogeny-svg]').waitFor(),
    verify: async page => { assert.match(await page.getByTestId('phylogeny-length-notice').textContent(), /1 条非根枝长未指定/); const option = await page.locator('option[value=length]').evaluate(el => ({ disabled: el.disabled, html: el.outerHTML })); assert.equal(option.disabled, true, option.html);
      await page.getByRole('button', { name: '节点 Human', exact: true }).click(); assert.match(await page.getByTestId('phylogeny-selection').textContent(), /枝长原文：未指定/);
      return { topologyVisible: true, missingLengthNotZero: true, branchModeDisabled: true }; },
  },
  { ...base, name: 'domain-expansion-phylogeny-unsafe', file: { size: data.complete.metadata.source_bytes },
    preview: () => { const value = structuredClone(data.complete); value.phylogeny.nodes[1].label = '<img src="https://example.invalid/track">'; return value; },
    expectedError: /系统树响应未通过/, ready: page => page.locator('[role=alert]').waitFor(),
    verify: async page => { assert.equal(await page.locator('svg').count(), 0); assert.equal(await page.locator('img').count(), 0); return { unsafeRejectedBeforeSvg: true, noHtmlResource: true }; },
  },
  { ...base, name: 'domain-expansion-phylogeny-wrong-kind', file: { size: data.complete.metadata.source_bytes },
    api: async request => { assert.equal(request.method(), 'POST'); return { contentType: 'application/json', body: JSON.stringify({ code: 0, msg: 'ok', data: { contract_version: 2, plugin_id: 'test-domain-expansion-phylogeny-wrong-kind', kind: 'graph', version: '1'.repeat(64), revision: '2'.repeat(64), payload: { phylogeny: data.complete.phylogeny, media_type: 'application/json', view_kind: 'tree' }, metadata: data.complete.metadata, warnings: data.complete.warnings, sampled: false } }) }; },
    expectedError: /系统树响应未通过/, ready: page => page.locator('[role=alert]').waitFor(),
    verify: async page => { assert.equal(await page.locator('svg').count(), 0); return { wrongPublicKindRejected: true }; },
  },
];
