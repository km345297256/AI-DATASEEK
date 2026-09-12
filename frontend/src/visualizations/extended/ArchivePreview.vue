<template>
  <section class="flex min-h-0 flex-1 flex-col">
    <div class="flex flex-wrap items-center gap-3 border-b p-3 text-sm">
      <button :disabled="busy || rowOffset === 0" @click="rowOffset = Math.max(0, rowOffset - 200)">上一页</button>
      <span v-if="table">显示 {{ table.rows.length ? table.row_offset + 1 : 0 }}–{{ table.row_offset + table.rows.length }} / {{ table.total_rows }} 行</span>
      <button :disabled="busy || !table || rowOffset + 200 >= table.total_rows || rowOffset + 200 > 4095" @click="rowOffset += 200">下一页</button>
      <label>搜索本页 <input v-model="search" type="search" maxlength="128" class="rounded border px-2 py-1" /></label>
    </div>
    <p class="px-4 py-2 text-xs text-gray-500">{{ format || '压缩包' }} · 仅显示目录或流头元信息，不解压、打开成员或递归扫描；声明大小与内容完整性未经验证。GZIP 行数不是已验证成员数。</p>
    <p v-if="busy" role="status" class="p-4">正在读取目录…</p>
    <p v-if="error" role="alert" class="p-4 text-amber-700">{{ error }}</p>
    <p v-if="sampled" role="status" class="px-4 py-2 text-amber-700">目录或元信息已按预算截断，未显示的内容不代表不存在。</p>
    <ul v-if="warnings.length" class="list-inside list-disc px-4 py-2 text-xs text-gray-500"><li v-for="(warning, i) in warnings" :key="i">{{ warning }}</li></ul>
    <div class="min-h-0 flex-1 overflow-auto">
      <table v-if="table" class="w-full border-collapse text-xs"><thead class="sticky top-0 bg-gray-100"><tr><th v-for="(column, i) in table.columns" :key="i" class="border p-2 text-left">{{ column }}</th></tr></thead>
        <tbody><tr v-for="(row, i) in rows" :key="i"><td v-for="(cell, j) in row" :key="j" class="max-w-md whitespace-pre-wrap break-all border p-2">{{ cell === null ? '—' : cell }}</td></tr></tbody>
      </table>
      <p v-if="table && !rows.length" class="p-4 text-sm text-gray-500">当前页没有匹配项。</p>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { parseArchiveTable, type ArchiveTable } from './structureData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const table = shallowRef<ArchiveTable>(), rowOffset = ref(0), search = ref(''), format = ref('');
const busy = ref(false), error = ref(''), sampled = ref(false), warnings = ref<string[]>([]);
const loads = usePreviewLoad();
let version: string | undefined;
const rows = computed(() => {
  const query = search.value.trim().slice(0, 128).toLocaleLowerCase();
  return table.value?.rows.filter(row => !query || row.some(cell => String(cell ?? '').toLocaleLowerCase().includes(query))) ?? [];
});
async function loadArchive() {
  const load = loads.begin(); busy.value = true; error.value = ''; table.value = undefined; warnings.value = []; sampled.value = false; search.value = '';
  const offset = rowOffset.value;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { row_offset: offset, ...(version ? { version } : {}) }, load.signal);
    load.assertCurrent();
    if (result.kind !== 'table') throw new Error('此插件需要压缩包目录表。');
    const parsed = parseArchiveTable(result.payload.table);
    if (parsed.row_offset !== offset) throw new Error('压缩包目录页偏移不一致，请重新打开文件。');
    table.value = parsed; version = result.version;
    format.value = typeof result.metadata.format === 'string' ? result.metadata.format.slice(0, 80) : '';
    sampled.value = result.sampled; warnings.value = result.warnings.slice(0, 8).map(value => value.slice(0, 512));
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '压缩包目录无法安全读取。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
onMounted(loadArchive);
watch(rowOffset, loadArchive);
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version, () => props.plugin.enabled], () => {
  version = undefined; format.value = '';
  if (rowOffset.value !== 0) rowOffset.value = 0; else void loadArchive();
});
</script>
