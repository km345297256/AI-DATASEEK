<template>
  <section class="min-h-0 flex-1 overflow-auto bg-slate-50 p-4 text-sm text-slate-700" data-testid="genome-tracks">
    <p role="note" class="mb-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs">{{ BIO_WARNINGS['genome-tracks'] }}</p>
    <form v-if="catalog" class="flex flex-wrap items-end gap-3 rounded border bg-white p-3" @submit.prevent="loadRegion">
      <label>染色体 / 序列 <select v-model.number="chromosome" aria-label="轨道染色体" class="block max-w-[260px] rounded border p-1"><option v-for="c in catalog.chromosomes" :key="c.id" :value="c.id">{{ c.name }} · {{ c.records }} 个记录</option></select></label>
      <label>起始（1 起）<input v-model.number="start" aria-label="轨道起始位置" type="number" min="1" max="2147483647" class="block w-32 rounded border p-1" /></label>
      <label>结束（含）<input v-model.number="end" aria-label="轨道结束位置" type="number" min="1" max="2147483647" class="block w-32 rounded border p-1" /></label>
      <button :disabled="busy" type="submit" class="rounded bg-slate-800 px-3 py-2 text-white disabled:opacity-40">绘制所选区域</button>
      <button :disabled="!displayed" type="button" class="rounded border px-3 py-2 disabled:opacity-40" aria-label="轨道缩小" @click="zoom(2)">−</button><button :disabled="!displayed" type="button" class="rounded border px-3 py-2 disabled:opacity-40" aria-label="轨道放大" @click="zoom(.5)">＋</button>
    </form>
    <div v-if="catalog" class="my-3 flex flex-wrap items-center gap-4 text-xs"><span>参考组装未声明 · {{ catalog.metadata.records.toLocaleString() }} 个原始记录</span><label v-for="t in definitions" :key="t.id" class="flex gap-1"><input v-model="visible" type="checkbox" :value="t.id" />{{ t.name }}轨道</label><span v-if="catalog.metadata.labels_redacted">{{ catalog.metadata.labels_redacted }} 个字段已截取或隐藏</span></div>
    <p v-if="busy" role="status" class="p-3">正在验证并读取区域；超过 3,000 个元素请缩窄范围…</p><p v-if="error" role="alert" class="p-3 text-amber-700">{{ error }}</p>
    <div v-if="displayed" class="rounded border bg-white p-3">
      <p class="mb-2 text-xs" data-testid="tracks-range">{{ catalog?.chromosomes[displayed.selected.chromosome]?.name }}:{{ displayed.selected.start+1 }}–{{ displayed.selected.end }} · {{ displayed.tracks?.length }} 个元素</p>
      <div class="overflow-x-auto"><svg ref="svg" :viewBox="`0 0 1100 ${height}`" class="min-w-[760px] touch-none select-none rounded border bg-slate-50" aria-label="基因组交互轨道" @pointerdown="beginPan" @pointermove="pan" @pointerup="endPan" @pointercancel="cancelPan" @wheel.prevent="wheelZoom">
        <g v-for="tick in ticks" :key="tick"><line :x1="x(tick)" y1="25" :x2="x(tick)" :y2="height-10" stroke="#dbe3ee"/><text :x="x(tick)" y="16" text-anchor="middle" font-size="10" fill="#64748b">{{ (tick+1).toLocaleString() }}</text></g>
        <g :transform="`translate(${dragOffset},0)`"><g v-for="(track,ti) in enabledTracks" :key="track.id"><text x="8" :y="trackTop(ti)+22" font-size="11" fill="#475569">{{ track.name }}</text><line x1="125" x2="1080" :y1="trackTop(ti)+45" :y2="trackTop(ti)+45" stroke="#94a3b8" />
          <template v-if="track.id==='signal'"><text x="8" :y="trackTop(ti)+42" font-size="9" fill="#64748b">±{{ signalMagnitude.toPrecision(3) }}</text><rect v-for="f in featureList(track.id)" :key="f.id" data-track-feature="signal" :x="x(f.start)" :width="featureWidth(f)" :y="trackTop(ti)+45-((f.value??0)>0?Math.abs(f.value??0)/signalMagnitude*32:0)" :height="Math.max(.8,Math.abs(f.value??0)/signalMagnitude*32)" :fill="(f.value??0)<0?'#dc2626':'#2563eb'" @click.stop="selected=f"><title>{{ title(f) }}</title></rect></template>
          <template v-else><g v-for="f in featureList(track.id)" :key="f.id" :data-track-feature="track.id" class="cursor-pointer" @click.stop="selected=f"><rect :x="x(f.start)" :y="trackTop(ti)+12+(f.id%3)*12" :width="featureWidth(f)" height="10" rx="2" :fill="track.color"/><path v-if="f.strand==='+'||f.strand==='-'" :d="arrow(f,trackTop(ti)+17+(f.id%3)*12)" :fill="track.color"/><text v-if="featureWidth(f)>70" :x="x(f.start)+3" :y="trackTop(ti)+20+(f.id%3)*12" font-size="9" fill="white">{{ f.label }}</text><title>{{ title(f) }}</title></g></template>
        </g></g>
      </svg></div>
      <p class="mt-2 text-xs text-slate-500">拖动平移、滚轮缩放，松开后读取新区域；点击元素查看详情。信号条之间的空白表示未提供数据，不补零。BED 零长度区间显示为位置标记。</p>
      <p v-if="!displayed.tracks?.length" class="py-4 text-center">当前区域没有记录。</p>
      <div v-if="selected" class="mt-3 rounded border border-blue-200 bg-blue-50 p-3 text-xs" data-testid="track-detail"><div class="flex justify-between"><strong>{{ selected.label }}</strong><button aria-label="关闭轨道详情" @click="selected=undefined">×</button></div><p>{{ title(selected) }}</p><p class="break-all">{{ selected.detail }}</p></div>
      <div class="mt-3 overflow-x-auto"><p class="text-xs text-slate-500">图中包含所选区域全部元素；下表列出前 50 项供键盘选择。</p><table class="w-full text-left text-xs"><thead><tr><th>名称</th><th>位置（1 起）</th><th>方向</th><th>值</th></tr></thead><tbody><tr v-for="f in (displayed.tracks??[]).slice(0,50)" :key="f.id"><td><button class="text-blue-700 underline" @click="selected=f">{{ f.label }}</button></td><td>{{ f.start===f.end?`插入边界 ${f.start}`:`${f.start+1}–${f.end}` }}</td><td>{{ f.strand }}</td><td>{{ f.value??'—' }}</td></tr></tbody></table></div>
    </div>
  </section>
</template>
<script setup lang="ts">
import {computed,onScopeDispose,ref,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {BIO_WARNINGS,type GenomeFeature} from './sequenceBrowserData';
import {useBioPreview} from './useBioPreview';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const {catalog,displayed,busy,error,clear,loadData}=useBioPreview(()=>props.file,()=>props.plugin,'genome-tracks');
const chromosome=ref(0),start=ref(1),end=ref(1000),visible=ref(['variant','annotation','interval','signal']),selected=ref<GenomeFeature>(),svg=ref<SVGSVGElement>(),dragOffset=ref(0);let dragStart:number|undefined,dragId:number|undefined;
const current=computed(()=>catalog.value?.chromosomes[chromosome.value]);
const definitions=computed(()=>catalog.value?.metadata.format==='vcf'?[{id:'variant',name:'变异',color:'#dc2626'}]:['bedgraph','wig'].includes(catalog.value?.metadata.format)?[{id:'signal',name:'信号',color:'#2563eb'}]:catalog.value?.metadata.format==='bed'?[{id:'interval',name:'区间',color:'#7c3aed'}]:[{id:'annotation',name:'注释',color:'#16a34a'}]);
const enabledTracks=computed(()=>definitions.value.filter(t=>visible.value.includes(t.id))),height=computed(()=>45+enabledTracks.value.length*90),trackTop=(i:number)=>30+i*90;
const left=computed(()=>displayed.value?.selected.start??0),right=computed(()=>displayed.value?.selected.end??1000),span=computed(()=>Math.max(1,right.value-left.value));
const x=(n:number)=>125+Math.max(0,Math.min(1,(n-left.value)/span.value))*955;
const ticks=computed(()=>Array.from({length:6},(_,i)=>Math.min(right.value-1,left.value+Math.floor(i*span.value/5))).filter((v,i,a)=>a.indexOf(v)===i));
const featureList=(track:string)=>(displayed.value?.tracks??[]).filter(f=>f.track===track);
const featureWidth=(f:GenomeFeature)=>Math.max(2,x(f.end)-x(f.start));
const signalMagnitude=computed(()=>Math.max(1e-300,...featureList('signal').map(f=>Math.abs(f.value??0))));
const arrow=(f:GenomeFeature,y:number)=>{const endpoint=f.strand==='+'?x(f.end):x(f.start),d=f.strand==='+'?-4:4;return `M ${endpoint} ${y} L ${endpoint+d} ${y-4} L ${endpoint+d} ${y+4} Z`;};
const title=(f:GenomeFeature)=>`${catalog.value?.chromosomes[f.chromosome]?.name} ${f.start===f.end?`插入边界 ${f.start}`:`${f.start+1}–${f.end}`} · 方向 ${f.strand}${f.value===null?'':` · 值 ${f.value}`}`;
async function loadRegion(){selected.value=undefined;await loadData({chromosome:chromosome.value,start:start.value-1,end:end.value});}
function chooseBounds(){const c=current.value;if(!c)return;start.value=Math.min(2147483647,c.start+1);end.value=Math.min(2147483647,Math.max(start.value,Math.min(c.end,start.value+99999)));}
async function navigateRegion(a:number,b:number){const n=Math.min(2147483647,Math.max(1,Math.round(b-a)));const origin=Math.max(0,Math.min(2147483647-n,Math.round(a)));start.value=origin+1;end.value=origin+n;await loadRegion();}
function zoom(factor:number){if(!displayed.value||busy.value)return;const middle=(left.value+right.value)/2,n=Math.max(1,Math.min(2147483647,Math.round(span.value*factor)));void navigateRegion(middle-n/2,middle+n/2);}
function wheelZoom(event:WheelEvent){zoom(event.deltaY>0?2:.5);}
function beginPan(event:PointerEvent){if(event.button!==0||busy.value||!svg.value||(event.target as Element)?.closest('[data-track-feature]'))return;dragStart=event.clientX;dragId=event.pointerId;svg.value.setPointerCapture(event.pointerId);}
function pan(event:PointerEvent){if(dragStart===undefined||!svg.value)return;dragOffset.value=(event.clientX-dragStart)*1100/Math.max(1,svg.value.getBoundingClientRect().width);}
function cancelPan(){if(dragId!==undefined&&svg.value?.hasPointerCapture(dragId))svg.value.releasePointerCapture(dragId);dragStart=undefined;dragId=undefined;dragOffset.value=0;}
function endPan(){const delta=-dragOffset.value/955*span.value;cancelPan();if(Math.abs(delta)>=1)void navigateRegion(left.value+delta,right.value+delta);}
watch(catalog,()=>{chromosome.value=0;chooseBounds();visible.value=['variant','annotation','interval','signal'];selected.value=undefined;},{flush:'sync'});
watch(chromosome,chooseBounds,{flush:'sync'});
watch([chromosome,start,end],()=>{clear();selected.value=undefined;},{flush:'sync'});
onScopeDispose(cancelPan);
</script>
