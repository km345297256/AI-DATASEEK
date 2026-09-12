<template>
 <section class="h-full overflow-auto bg-white p-4 text-sm" aria-label="比对区域工作台">
  <h3 class="text-base font-semibold">SAM / BAM / CRAM 比对区域工作台</h3>
  <p class="my-2 text-xs text-gray-500">整文件最多 64 MiB；无索引顺序扫描，最多 100,000 条记录 / 4,000,000 查询碱基。首次只检查头信息，不自动读取比对。CRAM 仅离线内嵌参考或无参考编码，不访问外部参考。坐标输入采用 0 基半开区间。</p>
  <form v-if="catalog" class="my-3 flex flex-wrap items-center gap-3 rounded border p-3" @submit.prevent="loadRegion">
   <label>参考序列 <select v-model.number="reference" aria-label="比对参考序列" :disabled="busy"><option v-for="r in catalog.references" :key="r.id" :value="r.id">{{r.name}} · {{r.length.toLocaleString()}}</option></select></label>
   <label>起点 <input v-model.number="start" aria-label="比对起点" type="number" min="0" :disabled="busy" class="w-28 border" /></label>
   <label>终点 <input v-model.number="end" aria-label="比对终点" type="number" min="1" :disabled="busy" class="w-28 border" /></label>
   <label>展示条数 <input v-model.number="maxReads" aria-label="比对展示条数" type="number" min="1" max="2000" :disabled="busy" class="w-20 border" /></label>
   <button type="submit" :disabled="busy" class="rounded border bg-emerald-50 px-3 py-1">读取比对区域</button>
   <button type="button" :disabled="busy||!data" class="rounded border px-2 py-1" @click="zoom(.5)">放大</button><button type="button" :disabled="busy||!data" class="rounded border px-2 py-1" @click="zoom(2)">缩小</button>
  </form>
  <p v-if="catalog" class="text-xs text-gray-500">{{catalog.format.toUpperCase()}} · {{catalog.sourceBytes.toLocaleString()}} 字节 · 排序声明 {{catalog.sortOrder}} · {{catalog.readGroups}} 个 Read Group</p>
  <p v-if="busy" role="status" class="py-3">正在隔离沙箱读取比对…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{error}}</p>
  <template v-if="data?.selected">
   <div class="my-3 flex items-center gap-4"><label><input v-model="logScale" type="checkbox" /> log(1 + 覆盖度)</label><button class="rounded border px-2 py-1" @click="exportRegion">导出当前区域 JSON</button><span class="text-xs text-gray-500">拖动轨道平移；Ctrl + 滚轮缩放。点击 read 查看详情。</span></div>
   <p class="my-2" :class="data.scanComplete?'text-emerald-700':'text-amber-700'">{{data.scanComplete?'已扫描至文件末尾':'扫描预算已用尽：覆盖度仅代表已扫描记录，不是全区域统计'}}。已扫描 {{data.recordsScanned.toLocaleString()}} 条，命中 {{data.matchedReads.toLocaleString()}} 条，展示 {{data.reads.length}} 条{{data.readsTruncated?'（展示截断）':''}}。</p>
   <p class="text-xs text-gray-500">覆盖度 = 每箱 M / = / X 对齐碱基数 ÷ 箱长度（包含重复、次级和补充比对，删除与剪接不计深度）。没有 MD 的 read 不推断错配。MAPQ 255 表示不可用。</p>
   <div class="my-3 rounded border p-2" aria-label="注释插件组合">
    <label>叠加已授权注释 <select v-model="annotationId" aria-label="比对注释文件" :disabled="overlayBusy||!annotationPlugin"><option value="">选择当前预览的关联文件</option><option v-for="f in annotationFiles" :key="f.file_id" :value="f.file_id">{{f.filename}}</option></select></label>
    <button class="ml-2 rounded border px-2 py-1" :disabled="!annotationId||!annotationPlugin||overlayBusy" @click="loadAnnotations">叠加当前区域</button><button v-if="annotations.length" class="ml-2 rounded border px-2 py-1" @click="clearAnnotations">移除注释</button>
    <p class="mt-1 text-xs text-gray-500">独立调用“基因组注释轨道”插件，停用它将立即移除叠加。只使用已授权关联文件；坐标与参考序列名称需一致，系统不推断基因组组装或重新映射。{{!annotationPlugin?'该插件未启用或目录尚未确认。':''}}{{!annotationFiles.length?'当前无可用关联注释文件。':''}}</p>
    <p v-if="overlayBusy" role="status">正在读取注释插件…</p><p v-if="overlayError" role="alert" class="text-amber-700">{{overlayError}}</p>
    <svg v-if="annotations.length" :viewBox="`0 0 1000 ${Math.max(40,annotationLanes.length*20)}`" class="mt-2 w-full" role="img" aria-label="比对注释叠加"><g v-for="(track,i) in annotationLanes" :key="track"><text x="0" :y="i*20+12" font-size="10">{{track}}</text><rect v-for="feature in annotations.filter(f=>f.track===track)" :key="feature.id" :x="x(feature.start)" :y="i*20+3" :width="Math.max(1,x(feature.end)-x(feature.start))" height="12" :fill="track==='variant'?'#dc2626':'#059669'"><title>{{feature.label}} [{{feature.start}},{{feature.end}}) {{feature.detail}}</title></rect></g></svg>
   </div>
   <div ref="trackElement" class="mt-3 overflow-auto rounded border" @pointerdown="panStart" @pointerup="panEnd" @pointercancel="drag=null" @wheel.prevent="wheel">
    <svg viewBox="0 0 1000 120" class="w-full min-w-[500px]" role="img" aria-label="比对覆盖度">
     <path :d="coveragePath" fill="none" stroke="#059669" stroke-width="1.5" /><text x="5" y="12" font-size="10">{{logScale?'log1p':'depth'}} · max {{maxDepth.toFixed(2)}}</text>
     <text v-for="tick in 6" :key="tick" :x="(tick-1)*196+2" y="116" font-size="10">{{Math.round(data.selected.start+(tick-1)*(data.selected.end-data.selected.start)/5).toLocaleString()}}</text>
    </svg>
    <svg :viewBox="`0 0 1000 ${readHeight}`" class="w-full min-w-[500px]" :style="{minHeight:`${readHeight}px`}" role="img" aria-label="比对 reads 轨道">
     <g v-for="pair in pairs" :key="pair.key"><line :x1="x(pair.start)" :x2="x(pair.end)" :y1="pair.y" :y2="pair.y" stroke="#94a3b8" stroke-dasharray="3 3" /></g>
     <g v-for="item in packed" :key="item.index" role="button" tabindex="0" :aria-label="`read ${item.read.name}`" @click.stop="selectedRead=item.read" @keydown.enter="selectedRead=item.read">
      <title>{{item.read.name}} · {{item.read.cigar}} · MAPQ {{item.read.mapq===255?'未提供':item.read.mapq}}</title>
      <line :x1="x(item.read.start)" :x2="x(item.read.end)" :y1="item.lane*20+12" :y2="item.lane*20+12" stroke="#cbd5e1" />
      <rect v-for="(block,i) in item.read.blocks" :key="`b${i}`" :x="x(block.start)" :y="item.lane*20+8" :width="Math.max(0,x(block.end)-x(block.start))" height="8" :fill="readColour(item.read)" />
      <path v-for="(s,i) in item.read.splices" :key="`s${i}`" :d="`M${x(s.start)},${item.lane*20+12} Q${(x(s.start)+x(s.end))/2},${item.lane*20} ${x(s.end)},${item.lane*20+12}`" fill="none" stroke="#7c3aed" />
      <rect v-for="(d,i) in item.read.deletions" :key="`d${i}`" :x="x(d.start)" :y="item.lane*20+8" :width="Math.max(0,x(d.end)-x(d.start))" height="8" fill="none" stroke="#ea580c" />
      <circle v-for="(m,i) in item.read.mismatches.filter(m=>visible(m.position))" :key="`m${i}`" :cx="x(m.position)" :cy="item.lane*20+12" r="2" fill="#ef4444"><title>{{m.position}}: {{m.reference}} → {{m.query}}</title></circle>
      <path v-for="(s,i) in item.read.insertions.filter(s=>visible(s.position))" :key="`i${i}`" :d="`M${x(s.position)-3},${item.lane*20+5} l6,0 l-3,5 Z`" fill="#db2777"><title>插入 {{s.length}}: {{s.sequence}}</title></path>
     </g>
    </svg>
   </div>
   <p class="my-2 text-xs text-gray-500">蓝：正向；紫：反向；灰：次级/补充；橙：重复。红点：MD 错配；粉三角：插入；橙框：删除；紫弧：剪接。配对虚线仅连接同名且互为配对坐标的 primary reads，不推断未展示的配偶。</p>
   <div v-if="selectedRead" class="my-3 rounded border p-3" aria-label="Read 详情"><button class="float-right" @click="selectedRead=null">关闭</button><strong>{{selectedRead.name}}</strong>
    <dl class="mt-2 grid grid-cols-2 gap-1 break-all"><dt>0 基区间 / 方向</dt><dd>[{{selectedRead.start}}, {{selectedRead.end}}) · {{selectedRead.reverse?'反向':'正向'}}</dd><dt>CIGAR / FLAG</dt><dd>{{selectedRead.cigar}} / {{selectedRead.flag}}</dd><dt>MAPQ / NM</dt><dd>{{selectedRead.mapq===255?'未提供':selectedRead.mapq}} / {{selectedRead.nm??'未提供'}}</dd><dt>MD / 错配信息</dt><dd>{{selectedRead.md??'未提供'}} / {{selectedRead.mismatch_available?'可用':'不可推断'}}</dd><dt>Read Group / 模板长度</dt><dd>{{selectedRead.read_group??'未提供'}} / {{selectedRead.template_length}}</dd><dt>Mate</dt><dd>{{selectedRead.mate_reference??'未提供'}}:{{selectedRead.mate_start??'未提供'}}</dd></dl>
    <p v-if="selectedRead.detail_truncated" class="mt-2 text-amber-700">该 read 的事件详情达到显示预算，已明确截断。</p>
   </div>
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
import {useFilePanel} from '../../composables/useFilePanel';
import {useVisualizationCatalog} from '../catalog';
import {parseBioData,bioCatalogIdentity,type GenomeFeature} from './sequenceBrowserData';
import {parseAlignment,validateAlignmentSelection,packAlignmentReads,alignmentWindow,type AlignmentRead,type AlignmentData} from './alignmentBrowserData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<AlignmentData>(),data=shallowRef<AlignmentData>(),selectedRead=shallowRef<AlignmentRead|null>(null);
const reference=ref(0),start=ref(0),end=ref(1000),maxReads=ref(500),busy=ref(false),error=ref(''),logScale=ref(false),trackElement=ref<HTMLElement>();
const overlayScope=usePreviewLoad(),annotationId=ref(''),overlayBusy=ref(false),overlayError=ref(''),annotations=shallowRef<GenomeFeature[]>([]);
const {relatedFiles}=useFilePanel(),plugins=useVisualizationCatalog();
const annotationPlugin=computed(()=>plugins.catalog.value?.plugins.find(p=>p.reader==='genome-tracks'&&p.enabled));
const annotationFiles=computed(()=>relatedFiles.value.filter(f=>f.file_id!==props.file.file_id&&annotationPlugin.value?.extensions.some(ext=>f.filename.toLowerCase().endsWith('.'+ext))));
const annotationLanes=computed(()=>[...new Set(annotations.value.map(f=>f.track))]);
let version:string|undefined;let drag:{x:number;id:number}|null=null;
const packed=computed(()=>data.value?.selected?packAlignmentReads(data.value.reads,data.value.selected.start,data.value.selected.end):[]);
const readHeight=computed(()=>Math.max(50,...packed.value.map(p=>(p.lane+1)*20)));
const maxDepth=computed(()=>Math.max(0,...(data.value?.coverage??[]).map(b=>b.depth)));
const pairs=computed(()=>{
 const byName=new Map<string,typeof packed.value>();
 for(const p of packed.value){
  if(!p.read.paired||p.read.secondary||p.read.supplementary)continue;
  const key=JSON.stringify([p.read.name,p.read.read_group]);const group=byName.get(key)??[];group.push(p);byName.set(key,group);
 }
 const selected=data.value?.selected,ref=selected?data.value?.references[selected.reference]?.name:undefined;
 return [...byName].flatMap(([key,p])=>p.length===2&&p[0]!.read.read1!==p[1]!.read.read1&&p.every(x=>x.read.mate_reference===ref)
  &&p[0]!.read.mate_start===p[1]!.read.start&&p[1]!.read.mate_start===p[0]!.read.start
  ?[{key,start:p[0]!.read.start,end:p[1]!.read.end,y:p[0]!.lane*20+12}]:[]);
});
const coveragePath=computed(()=>{const maximum=logScale.value?Math.log1p(maxDepth.value):maxDepth.value;return(data.value?.coverage??[]).map((b,i)=>`${i?'L':'M'}${x((b.start+b.end)/2)},${100-80*(maximum?(logScale.value?Math.log1p(b.depth):b.depth)/maximum:0)}`).join(' ')});
function x(value:number){const s=data.value?.selected;if(!s)return 0;return Math.max(0,Math.min(1000,(value-s.start)/(s.end-s.start)*1000))}
function visible(value:number){const s=data.value?.selected;return !!s&&value>=s.start&&value<s.end}
function readColour(r:AlignmentRead){return r.duplicate?'#d97706':r.secondary||r.supplementary?'#94a3b8':r.reverse?'#8b5cf6':'#3b82f6'}
function clearAnnotations(){overlayScope.begin();annotations.value=[];overlayBusy.value=false;overlayError.value=''}
function clear(){scope.begin();clearAnnotations();data.value=undefined;selectedRead.value=null;busy.value=false;error.value='';drag=null}
async function inspect(){const task=scope.begin();clearAnnotations();catalog.value=undefined;data.value=undefined;selectedRead.value=null;version=undefined;busy.value=false;error.value='';if(!props.plugin.enabled)return;busy.value=true;
 try{const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();const parsed=parseAlignment(result);if(parsed.format!==props.file.filename.split('.').pop()?.toLowerCase()||typeof props.file.size==='number'&&props.file.size>0&&props.file.size!==parsed.sourceBytes)throw new Error('比对目录不属于当前文件。');reference.value=0;start.value=0;end.value=Math.min(1000,parsed.references[0]!.length);maxReads.value=500;catalog.value=parsed;version=result.version}
 catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'比对目录读取失败。'}finally{if(task.isCurrent())busy.value=false}}
async function loadRegion(){const task=scope.begin();clearAnnotations();data.value=undefined;selectedRead.value=null;busy.value=true;error.value='';
 try{if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先检查比对头信息。');const selection=validateAlignmentSelection({reference:reference.value,start:start.value,end:end.value,max_reads:maxReads.value,bins:500},catalog.value.references);const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'table',version,...selection},task.signal);task.assertCurrent();if(result.version!==version)throw new Error('文件版本已变化，请重新打开。');const parsed=parseAlignment(result,selection);if(JSON.stringify(parsed.references)!==JSON.stringify(catalog.value.references)||parsed.sourceBytes!==catalog.value.sourceBytes)throw new Error('比对区域与已检查的目录不一致。');data.value=parsed}
 catch(reason){if(task.isCurrent())error.value=reason instanceof Error?reason.message:'比对区域读取失败。'}finally{if(task.isCurrent())busy.value=false}}
async function zoom(factor:number,anchor=.5){const s=data.value?.selected,c=catalog.value;if(!s||!c||busy.value)return;const next=alignmentWindow(s.start,s.end,c.references[s.reference]!.length,factor,anchor);start.value=next.start;end.value=next.end;await loadRegion()}
function panStart(event:PointerEvent){if(event.button!==0||busy.value||!data.value||((event.target as Element).closest('[role=button]')))return;drag={x:event.clientX,id:event.pointerId};trackElement.value?.setPointerCapture(event.pointerId)}
async function panEnd(event:PointerEvent){const d=drag;drag=null;const s=data.value?.selected,c=catalog.value;if(!d||d.id!==event.pointerId||!s||!c||busy.value||Math.abs(event.clientX-d.x)<8)return;const width=s.end-s.start;const left=Math.max(0,Math.min(c.references[s.reference]!.length-width,Math.round(s.start-(event.clientX-d.x)/(trackElement.value?.clientWidth||1000)*width)));start.value=left;end.value=left+width;await loadRegion()}
function wheel(event:WheelEvent){if(!event.ctrlKey||busy.value)return;const box=trackElement.value?.getBoundingClientRect();void zoom(event.deltaY>0?1.5:1/1.5,box?Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)):.5)}
function exportRegion(){if(!data.value)return;const url=URL.createObjectURL(new Blob([JSON.stringify({source:props.file.filename,version,coordinate_system:'0-based-half-open',...data.value},null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='alignment-region.json';link.click();URL.revokeObjectURL(url)}
async function loadAnnotations(){const task=overlayScope.begin();annotations.value=[];overlayError.value='';overlayBusy.value=true;
 try{const plugin=annotationPlugin.value,file=annotationFiles.value.find(f=>f.file_id===annotationId.value),s=data.value?.selected;if(!plugin||!file||!s||!catalog.value)throw new Error('请先选择当前区域和已授权注释文件。');const tree=await requestVisualization(file,plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();const cat=parseBioData(tree,'genome-tracks','tree',{},file.size,file.filename);const ref=catalog.value.references[s.reference]!.name;const chrom=cat.chromosomes.find(c=>c.name===ref);if(!chrom)throw new Error('注释文件中没有同名参考序列；不会推断或重映射。');const selected={chromosome:chrom.id,start:s.start,end:s.end};const result=await requestVisualization(file,plugin,'preview',{kind:'map',version:tree.version,...selected},task.signal);task.assertCurrent();if(result.version!==tree.version)throw new Error('注释文件版本变化，请重新打开。');const map=parseBioData(result,'genome-tracks','map',selected,file.size,file.filename);if(bioCatalogIdentity(cat)!==bioCatalogIdentity(map))throw new Error('注释目录变化。');annotations.value=map.tracks??[]}
 catch(reason){if(task.isCurrent())overlayError.value=reason instanceof Error?reason.message:'注释叠加失败。'}finally{if(task.isCurrent())overlayBusy.value=false}}
watch(reference,()=>{if(!catalog.value)return;start.value=0;end.value=Math.min(1000,catalog.value.references[reference.value]?.length??1000);clear()},{flush:'sync'});
watch([start,end,maxReads],()=>{if(catalog.value)clear()},{flush:'sync'});
watch([annotationId,()=>annotationPlugin.value?pluginPreviewIdentity(annotationPlugin.value):'',()=>annotationFiles.value.map(f=>filePreviewIdentity(f)).join('|')],clearAnnotations,{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect()},{immediate:true,flush:'sync'});
</script>
