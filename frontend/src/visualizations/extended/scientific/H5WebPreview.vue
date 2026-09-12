<template>
  <PreviewFrame :loading="loading" :error="error" note="H5Web · 文件内变量树与有界数值切片；不接收外部 HDF 服务地址。">
    <details class="mb-3 rounded border p-2 text-xs" :open="!datasets.length">
      <summary>文件内目录 · {{ nodes.length }} 个节点（最多 256 个／8 层）</summary>
      <ul class="max-h-48 overflow-auto"><li v-for="node in nodes" :key="node.path" class="py-1">
        <button v-if="datasets.some(item => item.path === node.path)" type="button" class="underline" :disabled="loading" @click="path = node.path; indices = []">{{ node.path }}</button><span v-else>{{ node.path }}</span>
        · {{ node.node_type }}<span v-if="node.dtype"> · {{ node.dtype }}</span><span v-if="node.shape"> · {{ node.shape.join(' × ') }}</span>
      </li></ul>
    </details>
    <p v-for="warning in warnings" :key="warning" class="mb-2 text-xs text-amber-700">{{ warning }}</p>
    <form class="mb-2 flex flex-wrap gap-3 text-xs" @submit.prevent="loadData">
      <label>变量 <select v-model="path" @change="indices = []"><option v-for="node in datasets" :key="node.path" :value="node.path">{{ node.path }} · {{ node.shape?.join(' × ') }}</option></select></label>
      <label>图式 <select v-model="kind"><option value="series">曲线</option><option value="heatmap">热图</option></select></label>
      <label v-for="(size, index) in sliceShape" :key="index">dim_{{ index }} <input v-model.number="indices[index]" type="number" min="0" :max="size - 1" step="1" class="w-16 border" /></label>
      <button class="rounded border px-2" type="submit" :disabled="loading || !path">读取切片</button>
    </form>
    <div v-show="datasets.length" ref="target" class="flex flex-none flex-col bg-white" style="height: 480px" />
    <p v-if="!loading && !datasets.length" class="text-xs">目录中没有当前预算内可绘制的数值节点；元数据仍可浏览。</p>
    <p v-if="sampled" class="text-xs text-amber-700">显示有界切片／降采样；其余维度默认索引 0。</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { parseArray, plainLabel } from './data';
import PreviewFrame from './PreviewFrame.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
const datasets = ref<Array<{ path: string; shape?: number[] }>>([]), path = ref(''), kind = ref<'series' | 'heatmap'>('series');
const nodes = ref<Array<{ path: string; node_type: string; shape?: number[]; dtype?: string }>>([]), warnings = ref<string[]>([]), version = ref<string>();
const indices = ref<number[]>([]);
const sliceShape = computed(() => { const shape = datasets.value.find((node) => node.path === path.value)?.shape ?? []; return shape.slice(0, Math.max(0, shape.length - (kind.value === 'series' ? 1 : 2))); });
async function loadData() {
  const load = scope.begin(); loading.value = true; error.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, { version: version.value, path: path.value, kind: kind.value, indices: sliceShape.value.map((_, index) => indices.value[index] ?? 0) }, load.signal);
    load.assertCurrent();
    const array = parseArray(result.array);
    const [React, ReactDom, H5, ndarray] = await Promise.all([import('react'), import('react-dom/client'), import('@h5web/lib'), import('ndarray'), import('@h5web/lib/styles.css')]);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    // React 18 publishes CommonJS: dynamic-import interop may expose only default.
    const react = React.default ?? React, reactDom = ReactDom.default ?? ReactDom;
    const root = reactDom.createRoot(target.value!); load.onDispose(() => root.unmount());
    const finite = array.values.filter((value): value is number => value !== null);
    const domain: [number, number] | undefined = finite.length ? [Math.min(...finite), Math.max(...finite)] : undefined;
    const dataArray = ndarray.default(Float64Array.from(array.values.map((value) => value ?? NaN)), array.shape);
    const metadata = result.metadata as Record<string, unknown> | undefined;
    const steps = Array.isArray(metadata?.strides) && metadata.strides.every((step) => typeof step === 'number' && step >= 1) ? metadata.strides as number[] : array.shape.map(() => 1);
    const common = { dataArray, domain, title: plainLabel(path.value), showGrid: true, abscissaParams: { label: '原数组索引', value: Float64Array.from({ length: array.shape[array.shape.length - 1]! }, (_, index) => index * (steps[steps.length - 1] ?? 1)) } };
    root.render(array.shape.length === 1 ? react.createElement(H5.LineVis, common) : react.createElement(H5.HeatmapVis, { ...common, colorMap: 'Viridis', ordinateParams: { label: '原数组索引', value: Float64Array.from({ length: array.shape[0]! }, (_, index) => index * (steps[0] ?? 1)) } }));
    warnings.value = Array.isArray(result.warnings) ? result.warnings.map(value => plainLabel(value)) : [];
    sampled.value = result.sampled === true;
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'HDF5 切片读取失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id], async () => {
  const load = scope.begin(); loading.value = true; error.value = ''; datasets.value = []; nodes.value = []; warnings.value = []; version.value = undefined; path.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, { kind: 'tree' }, load.signal); load.assertCurrent();
    if (!Array.isArray(result.tree) || result.tree.length > 256 || typeof result.version !== 'string' || !/^[a-f0-9]{64}$/.test(result.version)) throw new Error('变量目录或文件版本无效。');
    version.value = result.version;
    nodes.value = result.tree.filter((node): node is { path: string; node_type: string; shape?: number[]; dtype?: string } => !!node && typeof node === 'object' && typeof node.path === 'string' && node.path.length <= 512 && typeof node.node_type === 'string');
    const variables = (result.choices as { variables?: unknown[] } | undefined)?.variables;
    if (!Array.isArray(variables)) throw new Error('变量目录缺少批准的数值节点。');
    datasets.value = nodes.value.filter(node => node.node_type === 'dataset' && variables.includes(node.path) && Array.isArray(node.shape) && node.shape.length > 0);
    warnings.value = Array.isArray(result.warnings) ? result.warnings.map(value => plainLabel(value)) : [];
    sampled.value = result.sampled === true;
    path.value = datasets.value[0]?.path ?? '';
    if (path.value) await loadData();
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'HDF5 目录读取失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}, { immediate: true });
</script>
