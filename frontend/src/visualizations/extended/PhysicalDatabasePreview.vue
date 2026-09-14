<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{{ notice }}</p>
    <p v-if="busy" role="status">正在离线校验并读取物理文件…</p>
    <p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <template v-if="data">
      <p class="mb-3 text-xs text-gray-500">离线工具 {{ data.toolVersion }} · {{ data.sourceBytes.toLocaleString() }} 字节 · {{ data.rows.length }} 条目录／物理记录</p>
      <label class="mb-3 text-sm">搜索显示内容 <input v-model="search" aria-label="物理文件 搜索" maxlength="128" class="rounded border p-1" /></label>
      <div class="overflow-auto"><table aria-label="物理数据库文件预览" class="w-full border-collapse text-sm"><thead><tr><th v-for="c in data.columns" :key="c" class="border p-2 text-left">{{ labels[c] ?? c }}</th></tr></thead><tbody><tr v-for="(row,i) in visible" :key="i"><td v-for="(cell,j) in row" :key="j" class="max-w-md break-all border p-2">{{ cell === '' ? '（空）' : cell }}</td></tr></tbody></table></div>
      <div class="mt-3 flex items-center gap-3 text-sm"><button :disabled="page===0" class="rounded border px-3 py-1" @click="page--">上一页</button><span>第 {{ page+1 }} 页 · 共 {{ filtered.length }} 条 · 每页 50 条</span><button :disabled="(page+1)*50>=filtered.length" class="rounded border px-3 py-1" @click="page++">下一页</button></div>
    </template>
  </section>
</template>
<script setup lang="ts">
import {computed,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {parsePhysical,type PhysicalData} from './physicalDatabaseData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),data=shallowRef<PhysicalData>(),busy=ref(false),error=ref(''),search=ref(''),page=ref(0);
const labels:Record<string,string>={category:'类别',table:'所属表',name:'名称',definition:'结构属性',key_hex:'键（十六进制）',key_bytes:'键字节数',sequence:'序号（精确文本）',record_type:'物理类型',value_hex:'值（十六进制）',value_bytes:'值字节数',omitted:'超限省略'};
const notice=computed(()=>props.plugin.reader==='mysql-sdi'?'MySQL SDI 结构目录：只支持 MySQL 8.0、未加密／未压缩的 16 KiB 独立表空间。不读取行、redo 或 undo，不保证提交一致性。hidden=1 为可见列，其他值为隐藏列；索引 columns 为从 0 开始的列位置，含引擎内部字段。最多 16 MiB、1024 条结构记录。':'SST 物理点记录：只读单文件，不合并其他 SST/WAL，不代表最新逻辑状态；拒绝范围删除和自定义比较器。键值仅展示十六进制，不猜测文本或 JSON。omitted=key/value/both 表示超限省略，不能把其空显示当作空值。最多 16 MiB、1024 条记录。');
const filtered=computed(()=>data.value?.rows.filter(row=>row.some(cell=>cell.toLowerCase().includes(search.value.toLowerCase())))??[]);
const visible=computed(()=>filtered.value.slice(page.value*50,(page.value+1)*50));
watch(search,()=>{page.value=0},{flush:'sync'});
async function inspect(){
  const task=scope.begin();data.value=undefined;error.value='';busy.value=false;search.value='';page.value=0;
  if(!props.plugin.enabled)return;
  busy.value=true;
  try{
    const reader=props.plugin.reader;if(reader!=='mysql-sdi'&&reader!=='sst-records')throw new Error('物理文件读取器不匹配。');
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();
    if(result.kind!=='table'||result.version.length!==64||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('物理文件版本无效。');
    const parsed=parsePhysical(result.payload,result.metadata,reader);
    if(parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('物理预览不属于当前文件。');
    data.value=parsed;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'物理文件读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
