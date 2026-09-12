<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">受限只读文件预览，最多 16 MiB；不会连接或恢复数据库。目录读取会校验整个受支持文件，只保留所选页内容。Redis 仅支持 RDB 11 的字符串、列表、集合、哈希和有序集合，不支持的编码或对象将明确报错。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadPage">
      <div class="flex flex-wrap items-center gap-3">
        <label>分组 <select v-model="groupId" aria-label="记录 分组" :disabled="busy" class="rounded border p-1"><option v-for="g in catalog.groups" :key="g.id" :value="g.id">{{ g.label }} · {{ g.record_count }} 条</option></select></label>
        <label>起始偏移 <input v-model.number="offset" aria-label="记录 起始偏移" type="number" min="0" max="100000" step="1" :disabled="busy" class="w-28 rounded border p-1" /></label>
        <label>本页条数 <input v-model.number="limit" aria-label="记录 本页条数" type="number" min="1" max="50" step="1" :disabled="busy" class="w-20 rounded border p-1" /></label>
        <button type="submit" :disabled="busy" class="rounded border bg-emerald-50 px-3 py-1">读取记录分页</button>
        <button type="button" :disabled="busy || !data?.selected || data.selected.offset === 0" class="rounded border px-3 py-1" @click="page(-1)">上一页</button>
        <button type="button" :disabled="busy || !data?.hasMore || (data.selected?.offset ?? 0) + (data.selected?.limit ?? 0) > 100000" class="rounded border px-3 py-1" @click="page(1)">下一页</button>
      </div>
      <p class="mt-2 text-xs text-gray-500">{{ groupSummary }}。改变选项后手动读取，每页最多 50 条、每条最多 128 个节点。文档内字段按源顺序展示，不合并同名键。</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在读取已授权记录文件…</p>
    <p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <p v-if="catalog" class="my-3 text-xs text-gray-500">{{ catalog.engine === 'bson' ? 'BSON 文档流' : 'Redis RDB 11 · CRC64 已校验' }} · {{ catalog.sourceBytes.toLocaleString() }} 字节 · {{ catalog.total }} 条。过期键保留原始过期时刻，不按当前时间过滤；毫秒日期和 MongoDB 逻辑时间分别展示。</p>
    <p v-if="data" class="mb-3 text-sm">本页 {{ data.records?.length ?? 0 }} 条 · 省略 {{ data.omitted }} 处文本／字段名 · {{ data.unsupported }} 处类型未解释。</p>
    <p v-if="data?.records?.length === 0" class="py-4 text-sm text-gray-500">当前分页为空，未自动调整偏移。</p>
    <details v-for="record in data?.records" :key="`${data!.selected!.group_id}-${record.index}`" class="mb-3 rounded border p-3">
      <summary class="cursor-pointer break-words text-sm">记录 {{ record.index }}<span v-if="record.key"> · 键 [{{ record.key.type }}] {{ recordCellText(record.key) }}</span><span v-if="record.truncated"> · 存在明确省略项</span></summary>
      <p v-if="catalog?.engine === 'redis-rdb'" class="my-2 text-xs text-gray-500">{{ record.expires_at_ms === null ? '未设置过期时间' : `原始过期时刻：${record.expires_at_ms}（UTC Unix 毫秒）` }}</p>
      <div class="mt-2 overflow-auto"><table aria-label="记录 字段与类型" class="w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">字段／位置</th><th class="border p-2 text-left">类型</th><th class="border p-2 text-left">值</th></tr></thead><tbody><tr v-for="node in record.nodes" :key="node.id"><td class="break-words border p-2" :style="{ paddingLeft: `${8 + 16 * (nodeDepths.get(record)?.[node.id] ?? 0)}px` }">{{ node.parent === null ? '根节点' : node.key }}</td><td class="border p-2 font-mono">{{ node.cell.type }}</td><td class="max-w-xl whitespace-pre-wrap break-words border p-2 font-mono">{{ recordCellText(node.cell) }}</td></tr></tbody></table></div>
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
import {parseDatabaseRecords,validateRecordSelection,recordsCatalogMatches,recordCellText,recordNodeDepths,type RecordsData} from './databaseRecordsData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<RecordsData>(),data=shallowRef<RecordsData>();
const groupId=ref(''),offset=ref(0),limit=ref(20),busy=ref(false),error=ref('');
const groupSummary=computed(()=>Object.entries(catalog.value?.groups.find(g=>g.id===groupId.value)?.counts??{}).map(([type,count])=>`${type}: ${count}`).join(' · ')||'空分组');
const nodeDepths=computed(()=>new Map((data.value?.records??[]).map(record=>[record,recordNodeDepths(record)])));
let version:string|undefined;
async function inspect(){
  const task=scope.begin();catalog.value=undefined;data.value=undefined;version=undefined;busy.value=false;error.value='';groupId.value='';offset.value=0;limit.value=20;
  if(!props.plugin.enabled)return;busy.value=true;
  try{
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();
    if(result.kind!=='tree'||result.version.length!==64||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('记录目录或文件版本无效。');
    const parsed=parseDatabaseRecords('tree',result.payload,result.metadata);
    if(parsed.engine!==props.plugin.reader||parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('记录目录不属于当前文件。');
    version=result.version;catalog.value=parsed;groupId.value=parsed.groups[0]!.id;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'记录目录读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function loadPage(){
  const task=scope.begin();data.value=undefined;busy.value=true;error.value='';
  try{
    if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先读取记录目录。');
    const selected=validateRecordSelection({group_id:groupId.value,offset:offset.value,limit:limit.value},catalog.value.groups);
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'table',version,...selected},task.signal);task.assertCurrent();
    if(result.kind!=='table'||result.version!==version)throw new Error('文件版本变化，请重新打开预览。');
    const parsed=parseDatabaseRecords('table',result.payload,result.metadata,selected);
    if(!recordsCatalogMatches(catalog.value,parsed))throw new Error('记录分页与已读取目录不一致。');
    data.value=parsed;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'记录分页读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
async function page(direction:number){
  if(!data.value?.selected||busy.value)return;const next=Math.max(0,data.value.selected.offset+direction*data.value.selected.limit);
  if(next>100000||direction>0&&!data.value.hasMore)return;offset.value=next;await nextTick();await loadPage();
}
function clearSelection(){if(catalog.value){scope.begin();data.value=undefined;busy.value=false;error.value=''}}
watch(groupId,()=>{clearSelection();offset.value=0},{flush:'sync'});
watch([offset,limit],clearSelection,{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
