<template>
  <section class="flex min-h-0 flex-1 flex-col">
    <div class="flex flex-wrap items-center gap-3 border-b p-3 text-sm">
      <label>搜索预览节点 <input v-model="search" type="search" maxlength="128" class="rounded border px-2 py-1" /></label>
      <button :disabled="busy || !nodes.length" @click="expanded = new Set(nodes.map(node => node.path))">展开全部</button>
      <button :disabled="busy || !nodes.length" @click="expanded = new Set(['/0'])">收起分支</button>
    </div>
    <p class="px-4 py-2 text-xs text-gray-500">{{ format || '结构化数据' }} · 只读结构树，最多 256 节点、8 层；不执行代码、标签指令或外部引用。大整数按原始文本显示。</p>
    <p v-if="busy" role="status" class="p-4">正在读取结构…</p>
    <p v-if="error" role="alert" class="p-4 text-amber-700">{{ error }}</p>
    <p v-if="sampled" role="status" class="px-4 py-2 text-amber-700">结构已按预算截断；搜索仅覆盖已显示的节点。可切换原文预览查看其他内容。</p>
    <ul v-if="warnings.length" class="list-inside list-disc px-4 py-2 text-xs text-gray-500"><li v-for="(warning, i) in warnings" :key="i">{{ warning }}</li></ul>
    <div class="min-h-0 flex-1 overflow-auto px-4 pb-4" aria-label="只读数据结构树">
      <p v-if="!busy && nodes.length && !visible.length" class="py-4 text-sm text-gray-500">当前预览节点没有匹配项。</p>
      <div v-for="node in visible" :key="node.path" class="flex items-start gap-2 border-b border-gray-100 py-2 text-sm" :style="{ paddingLeft: `${(node.path.split('/').length - 2) * 16}px` }">
        <button v-if="branches.has(node.path)" class="shrink-0" :disabled="Boolean(search.trim())" :aria-expanded="Boolean(search.trim()) || expanded.has(node.path)" :aria-label="`${expanded.has(node.path) ? '收起' : '展开'} ${node.attributes.label}`" @click="toggle(node.path)">{{ search.trim() || expanded.has(node.path) ? '▾' : '▸' }}</button>
        <span v-else class="w-3 shrink-0" aria-hidden="true">·</span>
        <div class="min-w-0"><span class="whitespace-pre-wrap break-all font-medium">{{ node.attributes.label }}</span>
          <span class="ml-2 text-xs text-gray-500">{{ node.node_type }}{{ node.attributes.children_count !== undefined ? ` · ${node.attributes.children_count} 个子项` : '' }}</span>
          <span v-if="'value' in node.attributes" class="ml-2 whitespace-pre-wrap break-all font-mono">{{ scalarText(node.attributes.value) }}</span>
        </div>
      </div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { parentPath, parseStructuredTree, visibleTree, type StructuredNode } from './structureData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const nodes = shallowRef<StructuredNode[]>([]), expanded = ref(new Set(['/0']));
const search = ref(''), format = ref(''), busy = ref(false), error = ref(''), sampled = ref(false), warnings = ref<string[]>([]);
const loads = usePreviewLoad();
const branches = computed(() => new Set(nodes.value.map(node => parentPath(node.path)).filter(Boolean)));
const visible = computed(() => visibleTree(nodes.value, expanded.value, search.value));
const scalarText = (value: string | boolean | null | undefined) => value === null ? 'null' : String(value ?? '');
function toggle(path: string) {
  const updated = new Set(expanded.value);
  if (updated.has(path)) updated.delete(path); else updated.add(path);
  expanded.value = updated;
}
async function loadTree() {
  const load = loads.begin(); busy.value = true; error.value = ''; nodes.value = []; warnings.value = []; sampled.value = false;
  search.value = ''; format.value = ''; expanded.value = new Set(['/0']);
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', {}, load.signal);
    load.assertCurrent();
    if (result.kind !== 'tree') throw new Error('此插件需要结构树预览结果。');
    nodes.value = parseStructuredTree(result.payload.tree);
    format.value = typeof result.metadata.format === 'string' ? result.metadata.format.slice(0, 80) : '';
    sampled.value = result.sampled;
    warnings.value = result.warnings.slice(0, 8).map(value => value.slice(0, 512));
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '结构树无法安全读取。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
onMounted(loadTree);
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version, () => props.plugin.enabled], loadTree);
</script>
