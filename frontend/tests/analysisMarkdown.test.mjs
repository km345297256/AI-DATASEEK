import assert from 'node:assert/strict';
import test from 'node:test';
import { marked } from 'marked';
import { createAnalysisMarkdown } from '../src/utils/analysisMarkdown.ts';

test('actual analysis relationships and ranges retain their scientific text', () => {
  const parser = createAnalysisMarkdown();
  const text = 'TOC~TN 原 r=+0.972，LOO +0.969~+0.982；δ13C~沉积速率，年代 1780.1~2019.1。';
  const html = parser.parse(text);
  assert.doesNotMatch(html, /<del>/);
  assert.ok(html.includes(text));
});

test('double deletion markup and other Markdown retain their meaning', () => {
  const html = createAnalysisMarkdown().parse('~~撤回结论~~；TOC~TN **仍为正相关**；[原件](https://example.org/a~b)\n\n`x~y~z`\n\n```python\nx = ~mask\n```');
  assert.match(html, /<del>撤回结论<\/del>/);
  assert.match(html, /TOC~TN <strong>仍为正相关<\/strong>/);
  assert.match(html, /href="https:\/\/example\.org\/a~b"/);
  assert.match(html, /<code>x~y~z<\/code>/);
  assert.match(html, /x = ~mask/);
  assert.equal((html.match(/<del>/g) || []).length, 1);
});

test('analysis parsing leaves global marked behavior and custom renderers unchanged', () => {
  const parser = createAnalysisMarkdown();
  assert.match(marked.parse('~removed~'), /<del>/);
  assert.doesNotMatch(parser.parse('~removed~'), /<del>/);
  const renderer = new marked.Renderer();
  renderer.code = ({text}) => `<pre data-custom="true">${text}</pre>`;
  assert.match(parser.parse('```text\n~literal~\n```', {renderer}), /data-custom="true">~literal~/);
});
