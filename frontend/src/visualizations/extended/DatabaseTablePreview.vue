<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">只读数据库快照：整文件最多 16 MiB，不是大库分块查询。首次只查询目录，不自动读取表行或统计总行数。不执行用户 SQL、视图、宏或外部连接；仅支持各读取器声明的文件子集。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadPage">
      <label class="mb-3 block">数据表 <select v-model="tableId" aria-label="数据库 数据表" class="ml-2 rounded border p-1" :disabled="busy"><option v-for="table in catalog.tables" :key="table.id" :value="table.id">{{ table.label }} · {{ table.columns.length }} 列</option></select></label>
      <fieldset :disabled="busy" class="mb-3 flex max-h-44 flex-wrap gap-3 overflow-auto"><legend class="mb-2">选择列（最多 16 列）</legend><label v-for="column in selectedTable?.columns" :key="column.id" class="inline-flex items-center gap-1"><input v-model="columns" type="checkbox" :value="column.id" :disabled="!column.previewable || (!columns.includes(column.id) && columns.length >= 16)" :aria-label="`数据库 列 ${column.id} ${column.label}`" />{{ column.label }}<span class="text-xs text-gray-500">{{ column.data_type }}{{ column.previewable ? '' : '（不支持读取）' }}</span></label></fieldset>
      <details class="mb-3"><summary class="cursor-pointer">字段结构与约束</summary><div class="max-h-48 overflow-auto"><table aria-label="数据库 字段结构" class="mt-2 border-collapse text-xs"><thead><tr><th class="border p-1">字段</th><th class="border p-1">数据类型</th><th class="border p-1">允许空值</th><th class="border p-1">主键次序</th></tr></thead><tbody><tr v-for="c in selectedTable?.columns" :key="c.id"><td class="border p-1">{{ c.label }}</td><td class="border p-1">{{ c.data_type }}</td><td class="border p-1">{{ c.nullable === null ? '未知' : c.nullable ? '是' : '否' }}</td><td class="border p-1">{{ c.primary_key === null ? '未知' : c.primary_key || '非主键' }}</td></tr></tbody></table></div></details>
      <div class="flex flex-wrap items-center gap-3"><label>起始偏移（从 0 开始） <input v-model.number="rowOffset" aria-label="数据库 起始行" type="number" min="0" max="100000" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label><label>本页行数 <input v-model.number="rowLimit" aria-label="数据库 本页行数" type="number" min="1" max="200" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
        <button type="submit" class="rounded border bg-emerald-50 px-3 py-1" :disabled="busy || !columns.length || columns.length > 16">读取数据库分页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.selected || data.selected.row_offset === 0" @click="page(-1)">上一页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.hasMore || (data.selected?.row_offset ?? 0) + (data.selected?.row_limit ?? 0) > 100000" @click="page(1)">下一页</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">整数及定点小数保持精确文本；NULL 与空字符串分别显示。BLOB 仅显示长度；附件、Memo 等不支持字段不可选。每页最多 200 行 × 16 列。更改选项后需手动读取。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取已授权数据库快照…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="catalog" class="my-3 text-xs text-gray-500">{{ DATABASE_ENGINES[catalog.engine] }} · 源文件 {{ catalog.sourceBytes.toLocaleString() }} 字节 · {{ catalog.tables.length }} 张表 · 总行数未统计。{{ orderLabel }}{{ data ? ` 本页 ${data.rows?.length ?? 0} 行。` : '' }}</p>
    <div v-if="data?.rows" class="overflow-auto"><table aria-label="数据库 数据分页" class="min-w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">快照行标识</th><th v-for="id in data.selected!.columns" :key="id" class="border p-2 text-left">{{ selectedTable!.columns[id]!.label }}</th></tr></thead><tbody><tr v-for="(row,index) in data.rows" :key="data.rowIds[index]"><th class="border p-2 text-left font-mono font-normal text-gray-500">{{ data.rowIds[index] }}</th><td v-for="(cell,c) in row" :key="c" class="max-w-md whitespace-pre-wrap break-words border p-2"><span class="mb-1 block text-[10px] text-gray-500">{{ cell.type }}</span><span class="font-mono">{{ databaseCellText(cell) }}</span></td></tr></tbody></table><p v-if="data.rows.length === 0" class="py-4 text-sm text-gray-500">当前分页为空，未自动调整起始行。</p></div>
    <details v-if="data?.rows?.length" class="mt-4 rounded border p-3" @toggle="plotOpen = ($event.target as HTMLDetailsElement).open">
      <summary class="cursor-pointer text-sm">当前页数值图表（不扫描全表）</summary>
      <label class="mt-3 block text-sm">数值列 <select v-model.number="plotColumn" aria-label="数据库 图表列" class="ml-2 rounded border p-1"><option v-for="(id,index) in data.selected!.columns" :key="id" :value="index">{{ selectedTable!.columns[id]!.label }}</option></select></label>
      <p class="my-2 text-xs text-gray-500">横轴为当前页顺序，不是时间或业务主键。只绘制有限实数和可精确表示的整数；定点小数、大整数及其他值仍以原文留在表格中。本页排除 {{ plot.skipped }} 个值，不按 0 填补。</p>
      <svg v-if="plotOpen && plot.points.length" role="img" aria-label="数据库 当前页数值图" viewBox="0 0 700 225" class="max-h-64 w-full"><line x1="30" x2="675" y1="110" y2="110" stroke="#94a3b8"/><text x="5" y="113" font-size="10">0</text><g v-for="p in plot.points" :key="p.index"><line :x1="p.x" :x2="p.x" y1="110" :y2="p.y" stroke="#059669" :stroke-width="Math.max(1,Math.min(8,500 / (data.rows?.length || 1)))"/><circle :cx="p.x" :cy="p.y" r="2" fill="#047857"><title>{{ `行 ${p.id}: ${p.value}` }}</title></circle></g><text x="30" y="220" font-size="11">{{ `对称纵轴范围：±${plot.scale}；${plot.points.length} 个有效点` }}</text></svg>
      <p v-else-if="plotOpen" class="py-2 text-sm text-gray-500">当前列没有可安全绘制的数值。</p>
    </details>
  </section>
</template>
<script setup lang="ts">
import {computed,nextTick,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {parseDatabaseTable,validateDatabaseSelection,databaseCatalogMatches,databaseCellText,databasePagePlot,DATABASE_ENGINES,type DatabaseData} from './databaseTableData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<DatabaseData>(),data=shallowRef<DatabaseData>();
const tableId=ref(''),columns=ref<number[]>([]),rowOffset=ref(0),rowLimit=ref(50),busy=ref(false),error=ref(''),plotColumn=ref(0),plotOpen=ref(false);
const selectedTable=computed(()=>catalog.value?.tables.find(t=>t.id===tableId.value));
const plot=computed(()=>databasePagePlot(plotOpen.value?data.value:undefined,plotColumn.value));
const orderLabel=computed(()=>catalog.value?.engine==='duckdb'?'按快照 rowid 升序。':catalog.value?.engine==='dbf'?'按 DBF 有效记录次序，忽略已删除记录。':'按 Access 本地表导出次序，不等同于主键排序。');
let version:string|undefined;
async function inspect(){
  const task=scope.begin();catalog.value=undefined;data.value=undefined;version=undefined;busy.value=false;error.value='';tableId.value='';columns.value=[];rowOffset.value=0;rowLimit.value=50;plotColumn.value=0;plotOpen.value=false;
  if(!props.plugin.enabled)return;busy.value=true;
  try{
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();
    if(result.kind!=='tree'||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('数据库目录或文件版本无效。');
    const parsed=parseDatabaseTable('tree',result.payload,result.metadata);
    if(parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('数据库目录不属于当前文件。');
    version=result.version;catalog.value=parsed;tableId.value=parsed.tables[0]!.id;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'数据库目录读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function loadPage(){
  const task=scope.begin();data.value=undefined;error.value='';busy.value=true;plotColumn.value=0;plotOpen.value=false;
  try{
    if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先检查数据库目录。');
    const selected=validateDatabaseSelection({table:tableId.value,columns:columns.value,row_offset:rowOffset.value,row_limit:rowLimit.value},catalog.value);
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'table',version,...selected},task.signal);task.assertCurrent();
    if(result.kind!=='table'||result.version!==version)throw new Error('文件版本变化，请重新打开预览。');
    const parsed=parseDatabaseTable('table',result.payload,result.metadata,selected);
    if(!databaseCatalogMatches(catalog.value,parsed))throw new Error('返回分页与已检查的目录不一致。');
    data.value=parsed;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'数据库分页读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function page(direction:number){
  if(!data.value?.selected||busy.value)return;
  const next=Math.max(0,data.value.selected.row_offset+direction*data.value.selected.row_limit);
  if(next>100000||direction>0&&!data.value.hasMore)return;
  rowOffset.value=next;await nextTick();await loadPage();
}
function clearSelection(){if(catalog.value){scope.begin();data.value=undefined;busy.value=false;error.value='';plotOpen.value=false}}
watch(tableId,()=>{clearSelection();columns.value=selectedTable.value?.columns.filter(c=>c.previewable).slice(0,8).map(c=>c.id)??[];rowOffset.value=0},{flush:'sync'});
watch([columns,rowOffset,rowLimit],clearSelection,{deep:true,flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
