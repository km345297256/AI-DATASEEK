<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">SQL 转储结构预览，不执行 SQL、不恢复数据库。只显示受支持的建表结构与 INSERT / COPY 字面量；类型声明不会触发转换，跳过的语句不会生效。整文件最多 16 MiB，首次检查会扫描转储结构与字面量，分页顺序不是数据库最终状态。</p>
    <form class="mb-3 flex flex-wrap items-center gap-3 rounded border p-3 text-sm" @submit.prevent="inspect">
      <label>来源数据库 <select v-model="dialect" aria-label="SQL 转储方言" class="ml-2 rounded border p-1" :disabled="busy"><option value="">请选择，不自动推断</option><option v-for="(label,value) in SQL_DIALECTS" :key="value" :value="value">{{ label }}</option></select></label>
      <button type="submit" class="rounded border bg-emerald-50 px-3 py-1" :disabled="busy || !dialect || !plugin.enabled">读取 SQL 转储目录</button>
      <span class="text-xs text-gray-500">无法确定来源或遇到不支持的 SQL 语法时，可切换原有文本预览。</span>
    </form>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadPage">
      <label class="mb-3 block">数据表 <select v-model="tableId" aria-label="SQL 转储数据表" class="ml-2 rounded border p-1" :disabled="busy"><option v-for="table in catalog.tables" :key="table.id" :value="table.id">{{ table.label }} · {{ table.columns.length }} 列</option></select></label>
      <fieldset :disabled="busy" class="mb-3 flex max-h-44 flex-wrap gap-3 overflow-auto"><legend class="mb-2">选择字段（最多 16 列）</legend><label v-for="column in selectedTable?.columns" :key="column.id" class="inline-flex items-center gap-1"><input v-model="columns" type="checkbox" :value="column.id" :disabled="!columns.includes(column.id) && columns.length >= 16" :aria-label="`SQL 转储列 ${column.id} ${column.label}`" />{{ column.label }}<span class="text-xs text-gray-500">{{ column.declared_type }}</span></label></fieldset>
      <div class="flex flex-wrap items-center gap-3"><label>起始偏移（从 0 开始） <input v-model.number="rowOffset" aria-label="SQL 转储起始行" type="number" min="0" max="100000" step="1" class="w-28 rounded border p-1" :disabled="busy" /></label><label>本页行数 <input v-model.number="rowLimit" aria-label="SQL 转储本页行数" type="number" min="1" max="200" step="1" class="w-20 rounded border p-1" :disabled="busy" /></label>
        <button type="submit" class="rounded border bg-emerald-50 px-3 py-1" :disabled="busy || !columns.length || columns.length > 16">读取 SQL 字面量分页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.selected || data.selected.row_offset === 0" @click="page(-1)">上一页</button><button type="button" class="rounded border px-3 py-1" :disabled="busy || !data?.hasMore || (data.selected?.row_offset ?? 0) + (data.selected?.row_limit ?? 0) > 100000" @click="page(1)">下一页</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">数字保留原文字面量，不转浮点数、不自动绘图；COPY 字段保持文本。NULL、空字符串、布尔值分别显示；二进制仅显示长度。更改选项后需手动读取。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在只读检查 SQL 转储…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="catalog" class="my-3 text-xs text-gray-500">{{ SQL_DIALECTS[catalog.dialect] }} · 源文件 {{ catalog.sourceBytes.toLocaleString() }} 字节 · {{ catalog.tables.length }} 张表 · {{ catalog.statements }} 个语句/控制片段 · {{ catalog.ignored }} 个未执行的跳过片段 · {{ catalog.sourceRows }} 行受支持字面量（非恢复后行数）。{{ data ? ` 本页 ${data.rows?.length ?? 0} 行。` : '' }}</p>
    <div v-if="data?.rows" class="overflow-auto"><table aria-label="SQL 转储字面量分页" class="min-w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">表内字面量行次序</th><th v-for="id in data.selected!.columns" :key="id" class="border p-2 text-left">{{ selectedTable!.columns[id]!.label }}</th></tr></thead><tbody><tr v-for="(row,index) in data.rows" :key="data.rowIds[index]"><th class="border p-2 text-left font-mono font-normal text-gray-500">{{ data.rowIds[index] }}</th><td v-for="(cell,c) in row" :key="c" class="max-w-md whitespace-pre-wrap break-words border p-2"><span class="mb-1 block text-[10px] text-gray-500">{{ cell.type }}</span><span class="font-mono">{{ sqlDumpCellText(cell) }}</span></td></tr></tbody></table><p v-if="data.rows.length === 0" class="py-4 text-sm text-gray-500">当前分页为空，未自动调整起始行。</p></div>
  </section>
</template>
<script setup lang="ts">
import {computed,nextTick,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {parseSqlDump,validateSqlDumpSelection,sqlDumpCatalogMatches,sqlDumpCellText,SQL_DIALECTS,type SqlDialect,type SqlDumpData} from './sqlDumpData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<SqlDumpData>(),data=shallowRef<SqlDumpData>();
const dialect=ref<SqlDialect|''>(''),tableId=ref(''),columns=ref<number[]>([]),rowOffset=ref(0),rowLimit=ref(50),busy=ref(false),error=ref('');
const selectedTable=computed(()=>catalog.value?.tables.find(t=>t.id===tableId.value));
let version:string|undefined;
function reset(){scope.begin();catalog.value=undefined;data.value=undefined;version=undefined;busy.value=false;error.value='';tableId.value='';columns.value=[];rowOffset.value=0;rowLimit.value=50}
async function inspect(){
  reset();const task=scope.begin();
  if(!props.plugin.enabled||!dialect.value)return;busy.value=true;
  try{
    const expected={dialect:dialect.value};
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree',...expected},task.signal);task.assertCurrent();
    if(result.kind!=='tree'||result.version.length!==64||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('SQL 转储目录或文件版本无效。');
    const parsed=parseSqlDump('tree',result.payload,result.metadata,expected);
    if(props.file.filename.split('.').pop()?.toLowerCase()!=='sql'||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('SQL 转储目录不属于当前文件。');
    version=result.version;catalog.value=parsed;tableId.value=parsed.tables[0]!.id;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'SQL 转储目录读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function loadPage(){
  const task=scope.begin();data.value=undefined;error.value='';busy.value=true;
  try{
    if(!props.plugin.enabled||!catalog.value||!version||!dialect.value)throw new Error('请先选择方言并检查 SQL 转储目录。');
    const selected=validateSqlDumpSelection({dialect:dialect.value,table:tableId.value,columns:columns.value,row_offset:rowOffset.value,row_limit:rowLimit.value},catalog.value);
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'table',version,...selected},task.signal);task.assertCurrent();
    if(result.kind!=='table'||result.version!==version)throw new Error('文件版本变化，请重新打开预览。');
    const parsed=parseSqlDump('table',result.payload,result.metadata,selected);
    if(!sqlDumpCatalogMatches(catalog.value,parsed))throw new Error('返回分页与已检查的 SQL 转储目录不一致。');
    data.value=parsed;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'SQL 字面量分页读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function page(direction:number){
  if(!data.value?.selected||busy.value)return;
  const next=Math.max(0,data.value.selected.row_offset+direction*data.value.selected.row_limit);
  if(next>100000||direction>0&&!data.value.hasMore)return;
  rowOffset.value=next;await nextTick();await loadPage();
}
function clearSelection(){if(catalog.value){scope.begin();data.value=undefined;busy.value=false;error.value=''}}
watch(tableId,()=>{clearSelection();columns.value=selectedTable.value?.columns.slice(0,8).map(c=>c.id)??[];rowOffset.value=0},{flush:'sync'});
watch([columns,rowOffset,rowLimit],clearSelection,{deep:true,flush:'sync'});
watch(dialect,reset,{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{reset();dialect.value=''},{immediate:true,flush:'sync'});
</script>
