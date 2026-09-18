import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import * as presentation from '../src/utils/analysisPresentation.ts';
import { createMessageKey } from '../src/utils/sessionHistory.ts';
import { uploadAnalysisPrompt, toggleInputFile } from '../src/utils/analysisInputs.ts';
import { isLatestAssistantMessage } from '../src/utils/chatTimeline.ts';
import { isConsecutiveAssistant } from '../src/types/message.ts';

const file = (id, filename = 'result.png') => ({ file_id: id, filename, upload_date: '', file_url: `/files/${id}` });
const attachments = (role, files) => ({ type: 'attachments', content: { role, attachments: files, timestamp: 1 } });

test('input attachments precede their question without mutating execution order or message identities', () => {
  const messages = [
    { type: 'user', content: { content: 'Visualize these files', timestamp: 1 } },
    attachments('user', [file('input-one'), file('input-two')]),
    { type: 'assistant', content: { content: 'Analysis result', timestamp: 2 } },
    attachments('assistant', [file('result')]),
  ];
  const original = [...messages];
  const key = createMessageKey();
  const keys = messages.map(key);
  const entries = presentation.conversationEntries(messages);
  assert.deepEqual(entries.map(entry => entry.index), [1, 0, 2, 3]);
  assert.deepEqual(messages, original);
  for (const { message, index } of entries) {
    assert.equal(message, messages[index]);
    assert.equal(key(message), keys[index]);
  }
  assert.deepEqual(presentation.currentTurnDeliveries(messages).map(item => item.file_id), ['result']);
});

test('input grouping moves past an inserted task summary but never crosses a user turn', () => {
  const summary = { type: 'task-summary', content: { timestamp: 3 } };
  const messages = [
    { type: 'user', content: { content: 'first' } }, summary,
    attachments('user', [file('first-input')]), { type: 'step', content: { tools: [] } },
    { type: 'assistant', content: { content: 'first result' } }, attachments('assistant', [file('first-result')]),
    { type: 'user', content: { content: 'second' } },
    { type: 'task-summary', content: { timestamp: 5 } }, attachments('user', [file('second-input')]),
    { type: 'assistant', content: { content: 'second result' } },
  ];
  const entries = presentation.conversationEntries(messages);
  assert.deepEqual(entries.map(entry => entry.index), [2, 0, 1, 3, 4, 5, 8, 6, 7, 9]);
  assert.equal(presentation.precedingTaskSummary(messages, entries.find(entry => entry.message.type === 'step').index), summary);
});

test('text-only turns and standalone attachments keep their order', () => {
  const messages = [attachments('user', [file('standalone')]),
    { type: 'user', content: { content: 'no new attachment' } },
    { type: 'assistant', content: { content: 'answer' } }, attachments('assistant', [file('result')])];
  assert.deepEqual(presentation.conversationEntries(messages).map(entry => entry.index), [0, 1, 2, 3]);
  assert.deepEqual(presentation.conversationEntries([]), []);
});

test('gallery contains only published assistant images; scripts and input images are not results', () => {
  assert.deepEqual(presentation.deliveredImages(attachments('user', [file('input')])), []);
  const message = attachments('assistant', [file('plot'), file('code', 'plot.py'), file('table', 'values.csv')]);
  assert.deepEqual(presentation.deliveredImages(message).map(item => item.file_id), ['plot']);
  assert.equal(presentation.deliveredFiles(message).length, 3);
});

test('specialized preview suggestions use only current-turn verified attachments', () => {
  const messages = [attachments('assistant', [file('old', 'old.shp')]),
    { type: 'user', content: { content: 'new question' } }, attachments('user', [file('source')]),
    attachments('assistant', [file('new')]), attachments('assistant', [file('new')])];
  assert.deepEqual(presentation.currentTurnDeliveries(messages).map(item => item.file_id), ['new']);
});

test('task folding never reaches across a user turn', () => {
  const summary = { type: 'task-summary', content: { timestamp: 1 } };
  const messages = [summary, { type: 'step' }, { type: 'user' }, { type: 'step' }];
  assert.equal(presentation.precedingTaskSummary(messages, 1), summary);
  assert.equal(presentation.precedingTaskSummary(messages, 3), undefined);
});

test('file-only uploads have a visible request while normal requests stay unchanged', () => {
  assert.match(uploadAnalysisPrompt('', [file('x')]), /概览.*内容.*结构/);
  assert.equal(uploadAnalysisPrompt('  compare these  ', [file('x')]), 'compare these');
  assert.equal(uploadAnalysisPrompt(' ', []), '');
});

test('same-named inputs are selected by identity, explicitly clearing remains empty', () => {
  const files = [file('a', 'same.csv'), file('b', 'same.csv')];
  assert.deepEqual(toggleInputFile(['a'], 'b', files), ['a', 'b']);
  assert.deepEqual(toggleInputFile(['a'], 'a', files), []);
  assert.deepEqual(toggleInputFile(['a'], 'foreign', files), ['a']);
});

const source = readFileSync(new URL('../src/components/AnalysisConversation.vue', import.meta.url), 'utf8');
const script = compileScript(parse(source).descriptor, { id: 'analysis-conversation-test', inlineTemplate: true });
const compiled = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const componentModule = { exports: {} };
const icon = vue.defineComponent({ render: () => vue.h('i') });
const chatMessage = vue.defineComponent({ props: ['message'], render() { return vue.h('article', { 'data-role': this.message.content?.role }, this.message.type); } });
new Function('require', 'module', 'exports', compiled)((name) => {
  if (name === 'vue') return vue;
  if (name === 'lucide-vue-next') return { ChevronRight: icon, Image: icon };
  if (name === './ChatMessage.vue') return { default: chatMessage };
  if (name === '../api/client') return { API_CONFIG: { host: 'http://localhost:7001' } };
  if (name === '../api/file') return { prepareShapefilePreview: () => { throw Error('No preview read during rendering'); } };
  if (name === '../types/message') return { isConsecutiveAssistant };
  if (name === '../utils/chatTimeline') return { isLatestAssistantMessage };
  if (name === '../utils/analysisPresentation') return presentation;
  if (name === '../composables/useFilePanel') return { useFilePanel: () => ({ showFilePanel() {}, beginFilePreview() {} }) };
  if (name === '../utils/toast') return { showErrorToast() {} };
  assert.fail(`Unexpected dependency: ${name}`);
}, componentModule, componentModule.exports);

test('actual common conversation renders images and followups for both entry points', async () => {
  for (const allowProducts of [false, true]) {
    const html = await renderToString(vue.createSSRApp(componentModule.exports.default, {
      messages: [attachments('user', [file('input')]), attachments('assistant', [file('plot')])],
      messageKey: createMessageKey(), sessionId: 'session', isLoading: false, allowProducts,
      canResumeAnalysis: () => false, completionAdvice: { recommendations: ['Explain the result'] },
    }));
    assert.match(html, /可视化成果/);
    assert.match(html, /src="http:\/\/localhost:7001\/files\/plot"/);
    assert.doesNotMatch(html, /src="[^"]*\/files\/input"/);
    assert.match(html, /Explain the result/);
  }
});

test('actual conversation places files above text and thought summary while actions retain source indices', async () => {
  const messages = [
    { type: 'user', content: { content: 'Visualize the dataset', timestamp: 1 } },
    { type: 'task-summary', content: { timestamp: 2 } }, attachments('user', [file('input')]),
    { type: 'assistant', content: { content: 'The result', timestamp: 3 } },
  ];
  const resumeIndices = [];
  const html = await renderToString(vue.createSSRApp(componentModule.exports.default, {
    messages, messageKey: createMessageKey(), sessionId: 'session', isLoading: false,
    canResumeAnalysis: index => { resumeIndices.push(index); return false; },
  }));
  assert.deepEqual([...html.matchAll(/<article\b[^>]*>([^<]+)<\/article>/g)].map(match => match[1]), ['attachments', 'user', 'task-summary', 'assistant']);
  assert.deepEqual(resumeIndices, [2, 0, 1, 3]);
  assert.deepEqual(messages.map(message => message.type), ['user', 'task-summary', 'attachments', 'assistant']);
});

test('both page shells delegate timeline, gallery and recommendations to the common component', () => {
  for (const page of ['ChatPage', 'DatasetSeekPage']) {
    const text = readFileSync(new URL(`../src/pages/${page}.vue`, import.meta.url), 'utf8');
    assert.match(text, /<AnalysisConversation/);
    assert.doesNotMatch(text.split('<script')[0], /<ChatMessage|v-for="\(message, index\) in messages"/);
  }
});
