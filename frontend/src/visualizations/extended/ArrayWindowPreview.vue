<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900" role="note">原始存储值：未应用 CF scale_factor、add_offset 或有限填充值掩膜。非有限值留空；坐标为从 0 开始的维度索引，不是地理坐标。此预览不重采样、不转置、不修改源数据。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadWindow">
      <label>变量 <select v-model="variableId" aria-label="数组变量" class="ml-2 max-w-full rounded border p-1" :disabled="busy"><option value="">请选择数值变量</option><option v-for="variable in catalog.variables" :key="variable.id" :value="variable.id" :disabled="!variable.selectable">{{ variable.label }} · {{ variable.shape.join(' × ') || '标量／不支持维度' }} · {{ variable.dtype }} {{ variable.reason ? `（${reasonLabel(variable.reason)}）` : '' }}</option></select></label>
      <label class="ml-3">视图 <select v-model="viewKind" aria-label="数组视图" class="rounded border p-1" :disabled="busy"><option value="series">数值曲线（1 个切片轴）</option><option value="image">数值热图（2 个切片轴）</option></select></label>
      <div v-if="variable" class="my-3 grid gap-2">
        <div v-for="(dimension, axis) in dimensions" :key="axis" class="flex flex-wrap items-center gap-2">
          <span class="w-32">维度 {{ axis }} · {{ variable.shape[axis] }}</span>
          <select v-model="dimension.mode" :aria-label="`维度 ${axis} 模式`" class="rounded border p-1" :disabled="busy"><option value="fixed">固定索引</option><option value="slice">切片</option></select>
          <label>{{ dimension.mode === 'fixed' ? '索引' : '起点' }} <input v-model.number="dimension.start" :aria-label="`维度 ${axis} 起点`" type="number" min="0" :max="variable.shape[axis]! - 1" step="1" class="w-24 rounded border p-1" :disabled="busy" /></label>
          <template v-if="dimension.mode === 'slice'"><label>终点（不含） <input v-model.number="dimension.stop" :aria-label="`维度 ${axis} 终点`" type="number" min="1" :max="variable.shape[axis]" step="1" class="w-24 rounded border p-1" :disabled="busy" /></label><label>步长 <input v-model.number="dimension.step" :aria-label="`维度 ${axis} 步长`" type="number" min="1" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label></template>
        </div>
      </div>
      <button type="submit" class="rounded border px-3 py-1" :disabled="busy || !variable">读取显式切片</button>
      <p class="mt-2 text-xs text-gray-500">每窗最多 16,384 个值；累计读取 ≤8 MiB，解码分块 ≤16 MiB。树检查不会自动加载变量数据。经典 NetCDF3 请使用原 NetCDF 预览。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取授权的数组字节范围…</p>
    <p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="current" data-testid="array-window-stats" class="my-3 text-xs text-gray-500">本次读取 {{ current.readBytes.toLocaleString() }} 字节 / 源文件 {{ current.sourceBytes.toLocaleString() }} 字节，{{ current.reads }} 次范围请求。{{ current.truncated ? '目录受预算限制，仅列出部分节点。' : '' }}</p>
    <p v-if="data && Object.keys(data.attributes).length" class="my-2 text-xs text-gray-600">文件声明的属性（仅展示，未应用）：{{ JSON.stringify(data.attributes) }}</p>
    <div ref="target" class="min-h-0 w-full" aria-label="原始数组切片数值图" />
    <details v-if="catalog" class="mt-3 text-xs text-gray-500"><summary>受控文件目录（不追踪软链接、外部文件或虚拟数据源）</summary><ul class="my-2"><li v-for="node in catalog.tree" :key="node.path" :style="{ paddingLeft: `${node.attributes.depth * 12}px` }">{{ node.attributes.label }} · {{ node.node_type }} {{ reasonLabel(node.attributes.reason) }}</li></ul></details>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import { parseArrayWindow, validateArraySelection, type ArrayWindowData } from './arrayWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), plotScope = usePreviewLoad();
const catalog = shallowRef<ArrayWindowData>(), data = shallowRef<ArrayWindowData>();
const variableId = ref(''), viewKind = ref<'series' | 'image'>('series'), busy = ref(false), error = ref('');
const dimensions = ref<{ mode: 'fixed' | 'slice'; start: number; stop: number; step: number }[]>([]);
const target = ref<HTMLDivElement>();
const variable = computed(() => catalog.value?.variables.find(v => v.id === variableId.value && v.selectable));
const current = computed(() => data.value ?? catalog.value);
let version: string | undefined;
const reasonLabel = (reason: string) => ({ link: '链接不读取', alias: '重复或循环引用', depth: '目录深度超限', type: '不支持的数据类型', shape: '不支持的形状', storage: '外部或虚拟存储', filter: '不支持的过滤器', chunk: '解码分块超预算' }[reason] ?? '');
function resetSelection() {
  scope.begin(); data.value = undefined; plotScope.begin(); error.value = ''; busy.value = false;
  const count = viewKind.value === 'series' ? 1 : 2, rank = variable.value?.shape.length ?? 0;
  dimensions.value = (variable.value?.shape ?? []).map((size, axis) => ({ mode: axis >= rank - count ? 'slice' : 'fixed', start: 0, stop: Math.min(size, count === 1 ? 1024 : 128), step: 1 }));
}
async function inspect() {
  const request = scope.begin(); plotScope.begin(); catalog.value = undefined; data.value = undefined; version = undefined; variableId.value = ''; dimensions.value = []; error.value = ''; busy.value = false;
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version)) throw new Error('数组结构或文件版本无效。');
    const parsed = parseArrayWindow('tree', result.payload, result.metadata);
    catalog.value = parsed; version = result.version; variableId.value = parsed.variables.find(v => v.selectable)?.id ?? '';
    if (!variableId.value) error.value = '目录中没有可安全读取的数值变量；可查看目录或选择其他已启用插件。';
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '数组目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(parsed: ArrayWindowData) {
  const load = plotScope.begin();
  try {
    const array = parsed.array;
    if (!array) return;
    const library = await loadBrowserLibrary('plotly', load.signal);
    await nextTick(); load.assertCurrent(); if (!target.value) return;
    const element = document.createElement('div'); target.value.replaceChildren(element); element.style.height = '420px';
    load.onDispose(() => { library.purge(element); element.remove(); });
    let traces: import('plotly.js').Data[];
    if (array.shape.length === 1) {
      traces = [{ type: 'scatter', mode: array.shape[0] === 1 ? 'markers' : 'lines', x: parsed.axes[0]!.indices, y: array.values, connectgaps: false, name: '原始存储值' }];
    } else {
      const width = array.shape[1]!, z = Array.from({ length: array.shape[0]! }, (_, row) => array.values.slice(row * width, (row + 1) * width));
      traces = [{ type: 'heatmap', z, x: parsed.axes[1]!.indices, y: parsed.axes[0]!.indices, zsmooth: false, connectgaps: false, colorscale: 'Viridis' }];
    }
    await library.newPlot(element, traces, { margin: { l: 75, r: 40, t: 25, b: 55 }, xaxis: { title: { text: array.dimensions[array.shape.length - 1] } }, yaxis: { title: { text: array.shape.length === 1 ? '原始存储值' : array.dimensions[0] }, ...(array.shape.length === 2 ? { autorange: 'reversed' as const } : {}) } }, { responsive: true, displaylogo: false, displayModeBar: false });
    if (!load.isCurrent()) { library.purge(element); element.remove(); return; }
    if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (load.isCurrent()) void library.Plots.resize(element); }); observer.observe(element); load.onDispose(() => observer.disconnect()); }
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '数组绘图失败。'; }
}
async function loadWindow() {
  const request = scope.begin(); plotScope.begin(); data.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !version || !variable.value) throw new Error('请先检查目录并选择数值变量。');
    const kind = viewKind.value;
    const selection = validateArraySelection(kind, { variable: variableId.value, decode: 'raw', selection: dimensions.value.map(d => d.mode === 'fixed' ? d.start : { start: d.start, stop: d.stop, step: d.step }) }, variable.value);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind, version, ...selection }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'array' || result.version !== version) throw new Error('文件版本已变化，请重新打开预览。');
    const parsed = parseArrayWindow(kind, result.payload, result.metadata, selection);
    const returned = parsed.variables.find(v => v.id === selection.variable);
    if (!returned || returned.id !== variable.value?.id || returned.dtype !== variable.value.dtype || returned.label !== variable.value.label
      || returned.reason !== variable.value.reason || JSON.stringify(returned.shape) !== JSON.stringify(variable.value.shape)
      || JSON.stringify(returned.chunks) !== JSON.stringify(variable.value.chunks)) throw new Error('返回变量与已检查的目录不一致。');
    data.value = parsed; await draw(parsed); request.assertCurrent();
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '数组窗口读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([variableId, viewKind], resetSelection);
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true });
</script>
