<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">只读源文件顺序，不排序、不聚合、不执行 SQL。整数和 decimal 以精确文本展示，避免 64 位数值舍入；∅ 表示空值（含非有限浮点值）。仅支持平面标量列，Arrow 流、嵌套类型不在本版支持范围内。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadWindow">
      <p class="mb-2">{{ catalog.totalRows.toLocaleString() }} 行 · {{ catalog.columns.length }} 列 · {{ catalog.totalGroups }} 个行组／批次</p>
      <fieldset :disabled="busy" class="mb-3 flex max-h-48 flex-wrap gap-3 overflow-auto"><legend class="mb-2">选择列（最多 32 列）</legend>
        <label v-for="column in catalog.columns" :key="column.id" class="inline-flex items-center gap-1"><input v-model="selectedColumns" type="checkbox" :value="column.id" :aria-label="`列 ${column.id} ${column.label}`" />{{ column.label }} <span class="text-xs text-gray-500">{{ column.type }}{{ column.scale !== null ? `(${column.precision},${column.scale})` : '' }}</span></label>
      </fieldset>
      <div class="flex flex-wrap items-center gap-3"><label>起始行（从 0 开始） <input v-model.number="rowOffset" aria-label="起始行" type="number" min="0" :max="Math.max(0, catalog.totalRows - 1)" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label>
        <label>本页行数 <input v-model.number="rowLimit" aria-label="本页行数" type="number" min="1" max="200" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
        <button type="submit" class="rounded border px-3 py-1" :disabled="busy || !selectedColumns.length || selectedColumns.length > 32">读取选定窗口</button>
        <button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.selected || data.selected.row_offset === 0" @click="page(-1)">上一页</button>
        <button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.selected || data.selected.row_offset + data.selected.row_limit >= catalog.totalRows" @click="page(1)">下一页</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">首次仅检查结构，不自动读取表格。Parquet 仅读取命中的选定列块；Arrow/Feather 读取并解码命中批次，再展示所选列。每窗 ≤200 行 ×32 列；读取 ≤8 MiB，解码页／缓冲区 ≤16 MiB，扫描 ≤262,144 行；超大行组或批次会明确拒绝。最后一页仅显示剩余行。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取授权的列式字节窗口…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="current" data-testid="columnar-stats" class="my-3 text-xs text-gray-500">本次读取 {{ current.readBytes.toLocaleString() }} 字节 / 源文件 {{ current.sourceBytes.toLocaleString() }} 字节，{{ current.reads }} 次范围请求；扫描 {{ current.scanRows.toLocaleString() }} 行，解码页／缓冲区 {{ current.decodedBytes.toLocaleString() }} 字节。</p>
    <div v-if="data?.rows" class="overflow-auto"><table aria-label="列式数据窗口" class="min-w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">源行索引</th><th v-for="id in data.selected!.columns" :key="id" class="border p-2 text-left">{{ data.columns[id]!.label }}</th></tr></thead>
      <tbody><tr v-for="(row, index) in data.rows" :key="index"><th class="border p-2 text-left font-normal text-gray-500">{{ data.selected!.row_offset + index }}</th><td v-for="(cell, column) in row" :key="column" class="max-w-lg whitespace-pre-wrap break-words border p-2 font-mono" :class="{ 'text-gray-400': cell === null }">{{ cell === null ? '∅' : String(cell) }}</td></tr></tbody></table><p v-if="!data.rows.length" class="py-3 text-sm text-gray-500">文件结构有效，当前表格为空。</p></div>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { parseColumnarWindow, validateColumnarSelection, type ColumnarData } from './columnarWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), catalog = shallowRef<ColumnarData>(), data = shallowRef<ColumnarData>();
const selectedColumns = ref<number[]>([]), rowOffset = ref(0), rowLimit = ref(50), busy = ref(false), error = ref('');
const current = computed(() => data.value ?? catalog.value);
let version: string | undefined;
async function inspect() {
  const request = scope.begin(); catalog.value = undefined; data.value = undefined; version = undefined; busy.value = false; error.value = '';
  selectedColumns.value = []; rowOffset.value = 0; rowLimit.value = 50;
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(result.version)) throw new Error('列式结构或文件版本无效。');
    const parsed = parseColumnarWindow('tree', result.payload, result.metadata);
    if (parsed.format !== props.file.filename.split('.').pop()?.toLowerCase() || typeof props.file.size === 'number' && props.file.size > 0 && parsed.sourceBytes !== props.file.size) throw new Error('返回结构与当前文件不一致。');
    catalog.value = parsed; version = result.version;
    selectedColumns.value = catalog.value.columns.slice(0, 8).map(c => c.id);
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '列式目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function loadWindow() {
  const request = scope.begin(); data.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !catalog.value || !version) throw new Error('请先检查文件结构。');
    const selected = validateColumnarSelection({ columns: selectedColumns.value, row_offset: rowOffset.value, row_limit: rowLimit.value }, catalog.value);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'table', version, ...selected }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'table' || result.version !== version) throw new Error('文件版本已变化，请重新打开预览。');
    const parsed = parseColumnarWindow('table', result.payload, result.metadata, selected);
    if (parsed.totalRows !== catalog.value.totalRows || parsed.totalGroups !== catalog.value.totalGroups || parsed.sourceBytes !== catalog.value.sourceBytes || parsed.format !== catalog.value.format
      || JSON.stringify(parsed.columns) !== JSON.stringify(catalog.value.columns)) throw new Error('返回表格与已检查的结构不一致。');
    data.value = parsed;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '列式窗口读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function page(direction: number) {
  if (!data.value?.selected || !catalog.value || busy.value) return;
  const next = Math.max(0, data.value.selected.row_offset + direction * data.value.selected.row_limit);
  if (next >= catalog.value.totalRows) return;
  rowOffset.value = next; await nextTick(); await loadWindow();
}
watch([selectedColumns, rowOffset, rowLimit], () => { if (catalog.value) { scope.begin(); data.value = undefined; busy.value = false; error.value = ''; } }, { deep: true, flush: 'sync' });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true, flush: 'sync' });
</script>
