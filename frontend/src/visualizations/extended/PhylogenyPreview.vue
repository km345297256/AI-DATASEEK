<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { displayError } from './domains/lifecycle';
import { layoutPhylogeny, parsePhylogenyData, PHYLOGENY_WARNINGS, type PhylogenyData } from './phylogenyData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), data = shallowRef<PhylogenyData>(), viewport = ref<HTMLElement>();
const busy = ref(false), error = ref(''), mode = ref<'topology' | 'length'>('topology'), query = ref(''), selected = ref<number>();
const collapsed = shallowRef<Set<number>>(new Set());
const layout = computed(() => data.value ? layoutPhylogeny(data.value, mode.value, collapsed.value) : undefined);
const matches = computed(() => {
  const q = query.value.trim().toLocaleLowerCase();
  return !q || !data.value ? [] : data.value.nodes.flatMap((node, index) => node.label?.toLocaleLowerCase().includes(q) ? [index] : []);
});
const selectedNode = computed(() => selected.value === undefined ? undefined : data.value?.nodes[selected.value]);
const short = (label: string) => [...label].length > 40 ? [...label].slice(0, 40).join('') + '…' : label;
async function load() {
  const request = scope.begin(); data.value = undefined; busy.value = false; error.value = ''; mode.value = 'topology'; query.value = ''; selected.value = undefined; collapsed.value = new Set();
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal);
    request.assertCurrent();
    const parsed = parsePhylogenyData(result, props.file.size, props.file.filename);
    request.onDispose(() => { if (data.value === parsed) data.value = undefined; });
    request.assertCurrent(); data.value = parsed;
  } catch (reason) { if (request.isCurrent()) error.value = displayError(reason); }
  finally { if (request.isCurrent()) busy.value = false; }
}
function cancel() { scope.begin(); data.value = undefined; busy.value = false; error.value = '读取已取消。'; }
function toggle() {
  if (!props.plugin.enabled || busy.value || selected.value === undefined || !data.value?.children[selected.value]?.length) return;
  const next = new Set(collapsed.value); next.has(selected.value) ? next.delete(selected.value) : next.add(selected.value); collapsed.value = next;
}
function expandAll() { collapsed.value = new Set(); }
async function findNext() {
  if (!data.value || !matches.value.length || !props.plugin.enabled || busy.value) return;
  const current = data.value, i = (matches.value.indexOf(selected.value ?? -1) + 1) % matches.value.length, index = matches.value[i]!;
  const next = new Set(collapsed.value);
  let parent = current.nodes[index]!.parent;
  while (parent !== null) { const n = Number(parent.slice(1)); next.delete(n); parent = current.nodes[n]!.parent; }
  collapsed.value = next; selected.value = index;
  await nextTick();
  if (data.value !== current || !props.plugin.enabled) return;
  viewport.value?.querySelector(`[data-node="n${index}"]`)?.scrollIntoView({ block: 'center', inline: 'nearest' });
}
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void load(); }, { immediate: true, flush: 'sync' });
</script>
<template><section class="phylogeny-preview">
  <div class="toolbar">
    <label>视图 <select v-model="mode" aria-label="系统树视图" :disabled="busy || !data || !plugin.enabled"><option value="topology">拓扑（不按枝长）</option><option value="length" :disabled="!data?.branchLengthMode">枝长比例</option></select></label>
    <label>标签搜索 <input v-model="query" aria-label="系统树标签搜索" maxlength="128" :disabled="busy || !data || !plugin.enabled" placeholder="物种或内部节点标签" /></label>
    <span v-if="query" role="status">{{ matches.length }} 个匹配</span>
    <button :disabled="busy || !matches.length || !plugin.enabled" @click="findNext">定位下一个</button>
    <button :disabled="busy || !data || !collapsed.size || !plugin.enabled" @click="expandAll">展开全部</button>
    <button :disabled="busy || !plugin.enabled" @click="load">重新读取</button>
  </div>
  <p v-if="busy" role="status">正在读取单棵系统树… <button @click="cancel">取消</button></p>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="data" class="notice" data-testid="phylogeny-stats">{{ data.nodes.length }} 节点 · {{ data.leaves }} 叶节点 · 最深 {{ data.maxDepth }} 层 · 完整单树，未采样</p>
  <p v-if="data && !data.branchLengthMode" class="notice" data-testid="phylogeny-length-notice">{{ data.missingLengths ? `有 ${data.missingLengths} 条非根枝长未指定，不补零；仅显示拓扑。` : '所有非根枝长均为零，仅显示拓扑。' }}</p>
  <p v-if="layout && mode === 'length'" class="notice" data-testid="phylogeny-scale">横坐标：从显示起点累计枝长，0–{{ layout.scale }}（原始单位未知；数值坐标近似，枝长原文保留）</p>
  <p v-else-if="data" class="notice">横坐标仅为拓扑层级，不表示进化距离或时间。滚动查看完整树；点击节点可查看枝长原文及折叠分支。</p>
  <div ref="viewport" class="phylogeny-viewport">
    <svg v-if="layout && data" :width="layout.width" :height="layout.height" role="img" aria-label="系统发育树" data-testid="phylogeny-svg">
      <title>单棵 Newick 系统树的只读展示</title>
      <path v-for="edge in layout.paths" :key="edge.id" :d="edge.d" fill="none" stroke="#577b75" stroke-width="1.5" data-testid="phylogeny-branch" />
      <g v-for="point in layout.points" :key="point.index" :data-node="data.nodes[point.index]!.id" :transform="`translate(${point.x},${point.y})`" class="phylogeny-node" role="button" tabindex="0" :aria-label="`节点 ${data.nodes[point.index]!.label ?? data.nodes[point.index]!.id}`" @click="selected = point.index" @keydown.enter.prevent="selected = point.index" @keydown.space.prevent="selected = point.index">
        <title>{{ data.nodes[point.index]!.label ?? '未命名节点' }} · 枝长原文：{{ data.nodes[point.index]!.length ?? '未指定' }}{{ point.index === 0 ? '（根入枝，不参与坐标）' : '' }}</title>
        <rect x="-8" y="-23" :width="Math.max(26, Math.min(40, [...(data.nodes[point.index]!.label ?? '')].length) * 8 + 22)" height="32" fill="transparent" />
        <circle r="5" :fill="selected === point.index ? '#c66b13' : matches.includes(point.index) ? '#e2a21a' : '#23866b'" stroke="white" />
        <path v-if="point.collapsed" d="M7,-6L18,0L7,6Z" fill="#577b75" />
        <text :x="point.collapsed ? 23 : 10" :y="point.terminal ? 4 : -9" fill="#243b53" font-size="12">{{ short(data.nodes[point.index]!.label ?? (point.terminal ? '未命名' : '')) }}{{ point.collapsed ? `（折叠 ${data.descendants[point.index]} 个后代）` : '' }}</text>
      </g>
    </svg>
  </div>
  <div v-if="selectedNode && data" class="selection" data-testid="phylogeny-selection">
    <p>节点：{{ selectedNode.label ?? '未命名' }} · {{ selectedNode.id }} · 枝长原文：{{ selectedNode.length ?? '未指定' }}{{ selected === 0 ? '（根入枝，不参与坐标）' : '' }}</p>
    <button v-if="data.children[selected!]!.length" @click="toggle">{{ collapsed.has(selected!) ? '展开所选分支' : '折叠所选分支' }}</button>
  </div>
  <details class="notice"><summary>格式与语义限制</summary><p v-for="notice in PHYLOGENY_WARNINGS" :key="notice">{{ notice }}</p><p>本版仅支持 nwk/newick/tree/tre 中的括号 Newick 单树，最多 1000 节点、64 层、4 MiB。支持空标签、多分叉和单引号标签；未引号下划线按 Newick 规则转为空格。负枝长、注释/NHX、NEXUS、phyloXML、多树及外部资源不在本版范围内。</p></details>
</section></template>
<style scoped>.phylogeny-preview{height:100%;display:flex;flex-direction:column;gap:8px;padding:12px;overflow:auto}.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px}.toolbar label{display:flex;align-items:center;gap:6px}input,select,button{padding:5px 8px;background:white;border:1px solid #c8d1da;border-radius:4px}button:disabled,input:disabled,select:disabled{opacity:.5}.notice{font-size:12px;line-height:1.6;color:#667085}.phylogeny-viewport{overflow:auto;flex:1 0 300px;min-height:300px;border:1px solid #dbe3ec;background:#f8fafc;border-radius:6px}.phylogeny-node{cursor:pointer;outline:none}.phylogeny-node:focus circle{stroke:#a96310;stroke-width:3}.selection{font-size:12px;overflow-wrap:anywhere}[role=alert]{color:#b42318}</style>
