<template>
 <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
  <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">SQLite 只读快照：整文件最多 16 MiB，不是大库分块查询。只支持普通 rowid 表，不执行用户 SQL、视图、触发器、虚拟表或生成列。按 rowid 升序分页，不做连接、汇总或类型转换。</p>
  <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadPage">
   <label class="mb-3 block">数据表 <select v-model="tableId" aria-label="SQLite 数据表" class="ml-2 rounded border p-1" :disabled="busy"><option v-for="table in catalog.tables" :key="table.id" :value="table.id">{{ table.label }} · {{ table.columns.length }} 列</option></select></label>
   <fieldset :disabled="busy" class="mb-3 flex max-h-44 flex-wrap gap-3 overflow-auto"><legend class="mb-2">选择列（最多 16 列）</legend><label v-for="column in selectedTable?.columns" :key="column.id" class="inline-flex items-center gap-1"><input v-model="columns" type="checkbox" :value="column.id" :aria-label="`SQLite 列 ${column.id} ${column.label}`" />{{ column.label }}<span class="text-xs text-gray-500">{{ column.affinity }}</span></label></fieldset>
   <div class="flex flex-wrap items-center gap-3"><label>起始行（从 0 开始） <input v-model.number="rowOffset" aria-label="SQLite 起始行" type="number" min="0" max="100000" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label><label>本页行数 <input v-model.number="rowLimit" aria-label="SQLite 本页行数" type="number" min="1" max="200" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
    <button type="submit" class="rounded border bg-emerald-50 px-3 py-1" :disabled="busy || !columns.length || columns.length > 16">读取 SQLite 分页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.selected || data.selected.row_offset === 0" @click="page(-1)">上一页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.hasMore || (data.selected?.row_offset ?? 0) + (data.selected?.row_limit ?? 0) > 100000" @click="page(1)">下一页</button>
   </div>
   <p class="mt-2 text-xs text-gray-500">首次只查询目录，不自动读取用户行；不扫描统计全表总行数。列名旁是 SQLite 类型亲和性，每个单元格标记实际存储类型。整数与 rowid 保留精确文本，BLOB 只显示长度；文本超过 512 字节或含不安全路径时明确省略。每次查询最多 200 行 × 16 列。</p>
  </form>
  <p v-if="busy" role="status" class="py-3">正在读取已授权 SQLite 快照…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
  <p v-if="catalog" class="my-3 text-xs text-gray-500">源文件 {{ catalog.sourceBytes.toLocaleString() }} 字节 · {{ catalog.tables.length }} 张表 · 总行数未统计。{{ data ? `本页 ${data.rows?.length ?? 0} 行，${data.blobs} 个 BLOB，${data.omitted} 个省略文本，${data.nonfinite} 个非有限实数。` : '' }}</p>
  <div v-if="data?.rows" class="overflow-auto"><table aria-label="SQLite 数据分页" class="min-w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">rowid（升序）</th><th v-for="id in data.selected!.columns" :key="id" class="border p-2 text-left">{{ selectedTable!.columns[id]!.label }}</th></tr></thead><tbody><tr v-for="(row,index) in data.rows" :key="data.rowIds[index]"><th class="border p-2 text-left font-mono font-normal text-gray-500">{{ data.rowIds[index] }}</th><td v-for="(cell,c) in row" :key="c" class="max-w-md whitespace-pre-wrap break-words border p-2"><span class="mb-1 block text-[10px] text-gray-500">{{ cell.type }}</span><span class="font-mono" :class="{'text-gray-500':['null','blob','text-omitted','nonfinite'].includes(cell.type)}">{{ sqliteCellText(cell) }}</span></td></tr></tbody></table><p v-if="data.rows.length === 0" class="py-4 text-sm text-gray-500">当前分页为空，未自动调整起始行。</p></div>
 </section>
</template>
<script setup lang="ts">
import {computed,nextTick,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {parseSqliteTable,validateSqliteSelection,sqliteCatalogMatches,sqliteCellText,type SqliteData} from './sqliteTableData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<SqliteData>(),data=shallowRef<SqliteData>();
const tableId=ref(''),columns=ref<number[]>([]),rowOffset=ref(0),rowLimit=ref(50),busy=ref(false),error=ref('');
const selectedTable=computed(()=>catalog.value?.tables.find(t=>t.id===tableId.value));
let version:string|undefined;
async function inspect(){
 const task=scope.begin();catalog.value=undefined;data.value=undefined;version=undefined;busy.value=false;error.value='';tableId.value='';columns.value=[];rowOffset.value=0;rowLimit.value=50;
 if(!props.plugin.enabled)return;busy.value=true;
 try{
  const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();
  if(result.kind!=='tree'||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('SQLite 目录或文件版本无效。');
  const parsed=parseSqliteTable('tree',result.payload,result.metadata);
  if(parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('SQLite 目录不属于当前文件。');
  version=result.version;catalog.value=parsed;tableId.value=parsed.tables[0]!.id;
 }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'SQLite 目录读取失败。'}
 finally{if(task.isCurrent())busy.value=false}
}
async function loadPage(){
 const task=scope.begin();data.value=undefined;error.value='';busy.value=true;
 try{
  if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先检查 SQLite 目录。');
  const selected=validateSqliteSelection({table:tableId.value,columns:columns.value,row_offset:rowOffset.value,row_limit:rowLimit.value},catalog.value);
  const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'table',version,...selected},task.signal);task.assertCurrent();
  if(result.kind!=='table'||result.version!==version)throw new Error('文件版本变化，请重新打开预览。');
  const parsed=parseSqliteTable('table',result.payload,result.metadata,selected);
  if(!sqliteCatalogMatches(catalog.value,parsed))throw new Error('返回分页与已检查的目录不一致。');
  data.value=parsed;
 }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'SQLite 分页读取失败。'}
 finally{if(task.isCurrent())busy.value=false}
}
async function page(direction:number){
 if(!data.value?.selected||busy.value)return;
 const next=Math.max(0,data.value.selected.row_offset+direction*data.value.selected.row_limit);
 if(next<0||next>100000||direction>0&&!data.value.hasMore)return;
 rowOffset.value=next;await nextTick();await loadPage();
}
function clearSelection(){if(catalog.value){scope.begin();data.value=undefined;busy.value=false;error.value=''}}
watch(tableId,()=>{clearSelection();columns.value=selectedTable.value?.columns.slice(0,8).map(c=>c.id)??[];rowOffset.value=0},{flush:'sync'});
watch([columns,rowOffset,rowLimit],clearSelection,{deep:true,flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
