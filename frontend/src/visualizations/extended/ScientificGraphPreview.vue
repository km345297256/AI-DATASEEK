<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import cytoscape, { type Core, type Layouts } from 'cytoscape';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { graphElements, GRAPH_WARNINGS, parseScientificGraphData, type ScientificGraphData } from './scientificGraphData';
import { displayError } from './domains/lifecycle';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), container = ref<HTMLElement>(), busy = ref(false), error = ref('');
const data = ref<ScientificGraphData>(), layoutName = ref<'circle' | 'grid'>('circle'), query = ref(''), selected = ref('');
let graph: Core | undefined, layout: Layouts | undefined;
const selectedNode = computed(() => data.value?.nodes.find(n => n.id === selected.value));
const selectedEdge = computed(() => data.value?.edges.find(e => e.id === selected.value));
const matchCount = computed(() => {
  const q = query.value.trim().toLocaleLowerCase();
  return !q ? 0 : data.value?.nodes.filter(n => `${n.key} ${n.label} ${n.group ?? ''}`.toLocaleLowerCase().includes(q)).length ?? 0;
});
function highlight() {
  if (!graph || graph.destroyed()) return;
  graph.elements().removeClass('highlight');
  const q = query.value.trim().toLocaleLowerCase();
  if (!q) return;
  const ids = data.value?.nodes.filter(n => `${n.key} ${n.label} ${n.group ?? ''}`.toLocaleLowerCase().includes(q)).map(n => n.id) ?? [];
  for (const id of ids) graph.getElementById(id).addClass('highlight');
}
function applyLayout() {
  if (busy.value || !graph || graph.destroyed() || !props.plugin.enabled) return;
  if (!['circle', 'grid'].includes(layoutName.value)) return;
  busy.value = true;
  try {
    layout?.stop();
    // Fixed O(N+E) layouts only; no user layout options or force simulation.
    const options: cytoscape.CircleLayoutOptions | cytoscape.GridLayoutOptions = layoutName.value === 'circle'
      ? { name: 'circle', animate: false, fit: true, padding: 28, avoidOverlap: false }
      : { name: 'grid', cols: Math.ceil(Math.sqrt(data.value?.nodes.length ?? 1)), animate: false, fit: true, padding: 28, avoidOverlap: false };
    layout = graph.layout(options);
    layout.run();
  } catch (reason) { error.value = displayError(reason); }
  finally { busy.value = false; }
}
function fit() { if (!busy.value && props.plugin.enabled && graph && !graph.destroyed()) graph.fit(undefined, 28); }
function cancel() { loads.begin(); busy.value = false; data.value = undefined; selected.value = ''; error.value = '读取已取消 / Loading cancelled.'; }
async function load() {
  const fileIdentity = filePreviewIdentity(props.file), pluginIdentity = pluginPreviewIdentity(props.plugin);
  const task = loads.begin(); busy.value = true; error.value = ''; data.value = undefined; selected.value = ''; query.value = '';
  try {
    if (!props.plugin.enabled) return;
    const response = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'graph' }, task.signal);
    task.assertCurrent();
    if (filePreviewIdentity(props.file) !== fileIdentity || pluginPreviewIdentity(props.plugin) !== pluginIdentity) throw new Error('文件或插件已变化 / File or plugin changed.');
    if (response.kind !== 'graph') throw new Error('科学网络图响应类型不一致，请重新读取。');
    const next = parseScientificGraphData({ ...response.payload, contract_version: response.contract_version, type: props.plugin.reader, kind: response.kind,
      version: response.version, revision: response.revision, metadata: response.metadata, warnings: response.warnings, sampled: response.sampled }, props.file.size, props.file.filename);
    await nextTick(); task.assertCurrent();
    if (!container.value) throw new Error('图形容器不可用 / Graph viewport unavailable.');
    const instance = cytoscape({ container: container.value, elements: graphElements(next), layout: { name: 'preset' },
      minZoom: 0.05, maxZoom: 5, wheelSensitivity: 0.2, pixelRatio: 1, selectionType: 'single', boxSelectionEnabled: false,
      autoungrabify: true, style: [
        { selector: 'node', style: { 'background-color': '#23866b', width: 16, height: 16, label: next.nodes.length <= 150 ? 'data(label)' : '', 'font-size': 10, color: '#243b53', 'text-margin-y': -8, 'text-valign': 'top' } },
        { selector: 'edge', style: { width: 1, 'line-color': '#aab8c5', 'curve-style': 'bezier', 'target-arrow-shape': next.directed ? 'triangle' : 'none', 'target-arrow-color': '#aab8c5', 'arrow-scale': 0.7 } },
        { selector: ':selected', style: { 'background-color': '#e57819', 'line-color': '#e57819', 'target-arrow-color': '#e57819' } },
        { selector: '.highlight', style: { 'background-color': '#de9a24', label: 'data(label)', 'border-width': 2, 'border-color': '#8a5100' } },
      ] });
    graph = instance;
    let observer: ResizeObserver | undefined;
    task.onDispose(() => { observer?.disconnect(); layout?.stop(); layout = undefined; instance.removeAllListeners(); instance.destroy(); if (graph === instance) graph = undefined; });
    const onTap = (event: cytoscape.EventObject) => { if (task.isCurrent()) selected.value = event.target === instance ? '' : event.target.id(); };
    instance.on('tap', onTap);
    observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(() => { if (task.isCurrent() && !instance.destroyed()) instance.resize(); });
    observer?.observe(container.value);
    task.assertCurrent(); data.value = next; busy.value = false; applyLayout();
  } catch (reason) { if (task.isCurrent()) { loads.begin(); busy.value = false; error.value = displayError(reason); data.value = undefined; } }
  finally { if (task.isCurrent()) busy.value = false; }
}
watch(query, highlight);
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void load(); }, { flush: 'sync' });
onMounted(() => { void load(); });
</script>
<template><section class="scientific-graph-preview">
  <p class="notice">科学网络图 · 只读静态拓扑</p>
  <p v-if="busy" role="status">正在加载… <button type="button" @click="cancel">取消</button></p>
  <p v-if="error" role="alert">{{ error }}</p>
  <div class="toolbar">
    <label>布局 <select v-model="layoutName" aria-label="布局" :disabled="busy || !data || !plugin.enabled" @change="applyLayout"><option value="circle">圆形</option><option value="grid">网格</option></select></label>
    <button type="button" :disabled="busy || !data || !plugin.enabled" @click="fit">适应视口</button>
    <label>节点搜索 <input v-model="query" maxlength="128" :disabled="busy || !data || !plugin.enabled" placeholder="标识、标签或分组"/></label>
    <span v-if="query" role="status">{{ matchCount }} 个匹配</span>
    <button type="button" :disabled="busy || !plugin.enabled" @click="load">重新读取</button>
  </div>
  <p v-if="data" class="notice">{{ data.format }} · {{ data.nodes.length }} 节点 · {{ data.edges.length }} 边 · {{ data.directed ? '有向' : '无向' }} · {{ data.sourceBytes.toLocaleString() }} 字节 · 完整图，未采样</p>
  <div ref="container" class="graph-viewport" role="img" aria-label="有界科学网络拓扑视图 / Bounded scientific graph"></div>
  <p v-if="selectedNode" class="selection">节点：{{ selectedNode.key }} · {{ selectedNode.label }} <span v-if="selectedNode.group">· 分组：{{ selectedNode.group }}</span></p>
  <p v-else-if="selectedEdge" class="selection">边：{{ selectedEdge.key ?? selectedEdge.id }} · {{ selectedEdge.label || '—' }} · 权重：{{ selectedEdge.weight ?? '未指定' }}</p>
  <p v-else class="notice">单击节点或边查看详情；拖动画布平移、滚轮缩放。</p>
  <details class="notice"><summary>支持范围与限制</summary><p>规范化节点边 JSON、平面 GraphML 的 label/group/weight、GEXF 1.2/1.3 原生标签和权重。最多 1000 节点 / 3000 边 / 4 MiB。JSON 不是任意 Cytoscape 会话格式；超过 150 节点时仅显示搜索匹配的标签。</p>
    <p v-for="notice in GRAPH_WARNINGS" :key="notice">{{ notice }}</p></details>
</section></template>
<style scoped>.scientific-graph-preview{height:100%;display:flex;flex-direction:column;gap:8px;overflow:auto;padding:12px}.notice{font-size:12px;line-height:1.6;color:#667085}.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:12px}.toolbar label{display:flex;gap:6px;align-items:center}input,select,button{border:1px solid #c8d1da;border-radius:4px;padding:5px 8px;background:white}button:disabled,input:disabled,select:disabled{opacity:.5}.graph-viewport{min-height:320px;flex:1 0 320px;position:relative;background:#f8fafc;border:1px solid #dbe3ec;border-radius:6px}.selection{font-size:12px;overflow-wrap:anywhere}[role=alert]{color:#b42318}</style>
