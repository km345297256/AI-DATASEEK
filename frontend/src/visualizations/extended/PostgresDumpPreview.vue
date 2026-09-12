<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">PostgreSQL 备份目录预览：仅完成归档目录读取；未解压、校验或恢复数据段。不展示对象 SQL、所有者或源数据库名称，目录可读不代表备份可恢复。最多 16 MiB、1024 个目录对象。</p>
    <p v-if="busy" role="status">正在读取已授权备份目录…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <template v-if="data">
      <p class="mb-3 text-xs text-gray-500">{{ data.container }} · 归档版本 {{ data.archiveVersion }} · {{ data.sourceBytes.toLocaleString() }} 字节 · {{ data.objects.length }} 个对象</p>
      <div class="mb-3 flex flex-wrap items-center gap-3 text-sm"><label>对象类型 <select v-model="type" aria-label="备份 对象类型" class="rounded border p-1"><option value="">全部类型</option><option v-for="t in types" :key="t" :value="t">{{ t }}</option></select></label><label>搜索名称 <input v-model="search" aria-label="备份 搜索名称" maxlength="128" class="rounded border p-1" /></label><span>{{ filtered.length }} 个匹配对象</span></div>
      <div class="overflow-auto"><table aria-label="备份 对象目录" class="w-full border-collapse text-sm"><thead><tr><th class="border p-2 text-left">对象类型</th><th class="border p-2 text-left">命名空间</th><th class="border p-2 text-left">名称</th></tr></thead><tbody><tr v-for="o in visible" :key="o.path"><td class="border p-2">{{ o.attributes.object_type }}</td><td class="break-words border p-2">{{ o.attributes.schema }}</td><td class="break-words border p-2">{{ o.attributes.name }}</td></tr></tbody></table></div>
      <div class="mt-3 flex gap-3 text-sm"><button :disabled="page === 0" class="rounded border px-3 py-1" @click="page--">上一页</button><span>第 {{ page + 1 }} 页 · 每页 100 个对象</span><button :disabled="(page + 1) * 100 >= filtered.length" class="rounded border px-3 py-1" @click="page++">下一页</button></div>
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
import {parsePgDump,type PgDumpData} from './pgDumpData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),data=shallowRef<PgDumpData>(),busy=ref(false),error=ref(''),type=ref(''),search=ref(''),page=ref(0);
const types=computed(()=>[...new Set(data.value?.objects.map(o=>o.attributes.object_type)??[])].sort());
const filtered=computed(()=>data.value?.objects.filter(o=>(!type.value||o.attributes.object_type===type.value)&&o.attributes.name.toLowerCase().includes(search.value.toLowerCase()))??[]);
const visible=computed(()=>filtered.value.slice(page.value*100,(page.value+1)*100));
watch([type,search],()=>{page.value=0},{flush:'sync'});
async function inspect(){
  const task=scope.begin();data.value=undefined;busy.value=false;error.value='';type.value='';search.value='';page.value=0;
  if(!props.plugin.enabled)return;busy.value=true;
  try{
    if(props.plugin.reader!=='pg-dump')throw new Error('读取器不匹配。');
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();
    if(result.kind!=='tree'||result.version.length!==64||!/^[0-9a-f]{64}$/.test(result.version))throw new Error('备份目录版本无效。');
    const parsed=parsePgDump(result.payload,result.metadata);
    if(parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&parsed.sourceBytes!==props.file.size)throw new Error('备份目录不属于当前文件。');
    data.value=parsed;
  }catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'备份目录读取失败。'}
  finally{if(task.isCurrent())busy.value=false}
}
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
