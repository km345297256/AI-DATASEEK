<template>
  <section class="flex min-h-0 flex-1 flex-col">
    <div class="flex flex-wrap items-center gap-3 border-b p-3 text-sm">
      <label>工作表 <select v-model="sheet" :disabled="busy"><option v-for="name in sheets" :key="name">{{ name }}</option></select></label>
      <button :disabled="busy || rowOffset === 0" @click="rowOffset = Math.max(0, rowOffset - 200)">前200行</button>
      <button :disabled="busy || !table || rowOffset + 200 >= table.total_rows" @click="rowOffset += 200">后200行</button>
      <button :disabled="busy || columnOffset === 0" @click="columnOffset = Math.max(0, columnOffset - 100)">前100列</button>
      <button :disabled="busy || !table || columnOffset + 100 >= table.total_columns" @click="columnOffset += 100">后100列</button>
      <label><input type="checkbox" v-model="showFormulas" /> 显示公式文本</label>
    </div>
    <p class="px-4 py-2 text-xs text-gray-500">只读缓存值；不重算公式，不执行宏或外部连接。每次最多 200 行 × 100 列。</p>
    <p v-if="busy" role="status" class="p-4">正在读取工作表…</p><p v-if="error" role="alert" class="p-4 text-amber-700">{{ error }}</p>
    <div class="min-h-0 flex-1 overflow-auto"><table v-if="table" class="w-full border-collapse text-xs"><thead class="sticky top-0 bg-gray-100"><tr><th class="border p-2">行号</th><th v-for="(name, i) in table.columns" :key="i" class="border p-2">{{ name }}</th></tr></thead><tbody><tr v-for="(row, i) in table.rows" :key="i"><th class="border px-2">{{ rowOffset + i + 1 }}</th><td v-for="(value, j) in row" :key="j" class="max-w-sm whitespace-pre-wrap break-words border p-2">{{ showFormulas && table.formulas?.[i]?.[j] ? table.formulas[i][j] : value }}</td></tr></tbody></table></div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestPreview } from './runtime';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
interface Table { columns: string[]; rows: (string | number | boolean | null)[][]; formulas?: (string | null)[][]; total_rows: number; total_columns: number }
const sheet = ref(''), sheets = ref<string[]>([]), table = ref<Table>();
const rowOffset = ref(0), columnOffset = ref(0), showFormulas = ref(false), busy = ref(false), error = ref('');
const loads = usePreviewLoad();
let loadedWindow = '';
const windowKey = (name = sheet.value) => JSON.stringify([name, rowOffset.value, columnOffset.value]);
async function loadTable() {
  if (loadedWindow === windowKey()) return;
  const load = loads.begin(); busy.value = true; error.value = ''; table.value = undefined;
  try {
    const data = await requestPreview(props.file, props.plugin, { ...(sheet.value ? { sheet: sheet.value } : {}), row_offset: rowOffset.value, column_offset: columnOffset.value }, load.signal);
    load.assertCurrent();
    const choices = data.choices as { sheets?: string[] } | undefined;
    sheets.value = choices?.sheets ?? [];
    table.value = data.table as Table;
    const selected = data.selected as { sheet?: string } | undefined;
    loadedWindow = windowKey(selected?.sheet ?? sheet.value);
    if (!sheet.value && selected?.sheet) sheet.value = selected.sheet;
  } catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '工作表无法读取。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
watch([sheet, rowOffset, columnOffset], ([name], [previous]) => {
  if (name !== previous && (rowOffset.value || columnOffset.value)) {
    rowOffset.value = 0; columnOffset.value = 0;
    return; // The coalesced next callback requests the new sheet's first window once.
  }
  void loadTable();
});
onMounted(loadTable);
</script>
