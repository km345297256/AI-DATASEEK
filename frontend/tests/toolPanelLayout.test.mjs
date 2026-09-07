import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = (file) => readFileSync(new URL(file, import.meta.url), 'utf8');

test('mobile tool panel overrides the desktop half-width and anchors both edges', () => {
  const panel = source('../src/components/ToolPanel.vue');
  assert.match(panel, /max-sm:!w-full/);
  assert.match(panel, /max-sm:!left-0/);
  assert.match(panel, /max-sm:!right-0/);
  assert.match(panel, /parentSize\/2/); // Preserve the desktop split view.
});

test('job, approval and spill details scroll within a bounded accessible region', () => {
  const panel = source('../src/components/ToolPanelContent.vue');
  const details = panel.slice(panel.indexOf('data-testid="tool-execution-details"'), panel.indexOf('<component v-if="toolInfo"'));
  assert.match(details, /role="region"/);
  assert.match(details, /tabindex="0"/);
  assert.match(details, /max-h-\[60%\]/);
  assert.match(details, /overflow-y-auto/);
  assert.match(details, /AnalysisJobCard/);
  assert.match(details, /ToolApprovalCard/);
  assert.match(details, /safeToolContent\.spill/);
});

test('tool panel close action is keyboard accessible on the button, not just the icon', () => {
  const panel = source('../src/components/ToolPanelContent.vue');
  assert.match(panel, /<button\s+type="button"\s+:aria-label="\$t\('Close'\)"\s+@click="hide"/);
  assert.doesNotMatch(panel, /<Minimize2[^>]*@click/);
});
