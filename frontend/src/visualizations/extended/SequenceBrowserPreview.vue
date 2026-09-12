<template>
  <section class="min-h-0 flex-1 overflow-auto bg-slate-50 p-4 text-sm text-slate-700" data-testid="sequence-browser">
    <p class="mb-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs" role="note">{{ BIO_WARNINGS['sequence-browser'] }}</p>
    <form v-if="catalog" class="flex flex-wrap items-end gap-3 rounded border bg-white p-3" @submit.prevent="loadWindow">
      <label>序列 <select v-model.number="recordId" aria-label="序列" class="block max-w-[280px] rounded border p-1"><option v-for="r in catalog.records" :key="r.id" :value="r.id">{{ r.name }} · {{ r.length.toLocaleString() }} 字符</option></select></label>
      <label>起始位置（1 起）<input v-model.number="start" aria-label="序列起始位置" type="number" min="1" :max="current?.length" class="block w-32 rounded border p-1" /></label>
      <label>窗口 <select v-model.number="count" aria-label="序列窗口" class="block rounded border p-1"><option v-for="n in [50,100,250,500,1000]" :key="n" :value="n">{{ n }}</option></select></label>
      <label v-if="current?.qualities">质量编码 <select v-model="encoding" aria-label="FASTQ质量编码" class="block rounded border p-1"><option value="phred33">Phred+33（请确认来源）</option><option value="phred64">Phred+64（请确认来源）</option></select></label>
      <button class="rounded bg-slate-800 px-3 py-2 text-white disabled:opacity-40" :disabled="busy" type="submit">显示所选序列</button>
    </form>
    <div v-if="current" class="mt-3 space-y-3">
      <div class="flex flex-wrap gap-4 rounded border bg-white p-3 text-xs" data-testid="sequence-statistics"><span>目录 {{ catalog?.records.length }} 条 · 当前 {{ current.length.toLocaleString() }} 字符</span><span>GC 字符占比 {{ (100*current.gc_count/current.length).toFixed(2) }}%</span><span>N 字符占比 {{ (100*current.n_count/current.length).toFixed(2) }}%</span><template v-if="displayed?.sequence?.quality_stats"><span>整条平均质量 Q{{ (displayed.sequence.quality_stats.sum/current.length).toFixed(2) }}</span><span>Q20 以下 {{ displayed.sequence.quality_stats.low_count }} 个</span></template></div>
      <div class="rounded border bg-white p-3"><p class="mb-2 text-xs">全序列 GC 概览；点击跳转。{{ current.length.toLocaleString() }} 字符，不推断分子类型。</p>
        <div class="relative flex h-9 cursor-crosshair overflow-hidden rounded border" role="button" tabindex="0" aria-label="全序列GC概览" @click="overviewJump" @keydown.home.prevent="navigate(1)" @keydown.end.prevent="navigate(Math.max(1,current.length-count+1))">
          <span v-for="(gc,i) in current.gc_bins" :key="i" class="h-full min-w-0 flex-1" :style="{backgroundColor:`rgba(16,185,129,${.15+.8*gc/(Math.floor((i+1)*current.length/current.gc_bins.length)-Math.floor(i*current.length/current.gc_bins.length))})`}"></span>
          <span v-if="displayed" class="pointer-events-none absolute inset-y-0 border-2 border-slate-900 bg-white/20" :style="{left:`${100*(displayed.selected.start-1)/current.length}%`,width:`${Math.max(.5,100*(displayed.sequence?.bases.length??0)/current.length)}%`}"></span>
        </div><div class="flex justify-between text-xs text-slate-400"><span>1</span><span>{{ current.length }}</span></div>
      </div>
      <form class="flex flex-wrap items-center gap-2 rounded border bg-white p-3" @submit.prevent="searchMotif"><label>字面模体搜索 <input v-model="motif" aria-label="序列模体" maxlength="64" placeholder="例如 ATG" class="w-40 rounded border p-1 font-mono uppercase" /></label><button :disabled="busy" type="submit" class="rounded border px-3 py-1">搜索</button>
        <template v-if="displayed?.sequence"><span class="text-xs" data-testid="sequence-search-count">{{ displayed.sequence.search.total }} 处命中<span v-if="displayed.sequence.search.truncated">（仅导航前 10,000 处）</span></span><button type="button" :disabled="busy||!displayed.sequence.search.positions.length" class="rounded border px-2 py-1 disabled:opacity-40" @click="moveHit(-1)">上个命中</button><button type="button" :disabled="busy||!displayed.sequence.search.positions.length" class="rounded border px-2 py-1 disabled:opacity-40" @click="moveHit(1)">下个命中</button></template>
      </form>
    </div>
    <p v-if="busy" role="status" class="p-3">正在验证版本并读取有界序列…</p><p v-if="error" role="alert" class="p-3 text-amber-700">{{ error }}</p>
    <div v-if="displayed?.sequence" class="mt-3 rounded border bg-white p-4">
      <div class="mb-3 flex flex-wrap items-center gap-2"><button :disabled="busy||start<=1" class="rounded border px-2 py-1 disabled:opacity-40" @click="navigate(start-count)">上一窗口</button><span data-testid="sequence-range">{{ displayed.selected.start }}–{{ displayed.selected.start+displayed.sequence.bases.length-1 }}</span><button :disabled="busy||start+count>(current?.length??0)" class="rounded border px-2 py-1 disabled:opacity-40" @click="navigate(start+count)">下一窗口</button><button class="ml-auto rounded border px-3 py-1" @click="copyWindow">复制窗口</button></div>
      <p class="mb-3 text-xs text-slate-500">A 蓝 / C 绿 / G 橙 / T、U 红 / N 紫；黄色背景为字面模体。<span v-if="displayed.sequence.qualities">质量：红 &lt; Q20，橙 Q20–29，绿 ≥ Q30；当前编码 {{ displayed.selected.quality_encoding }}。</span></p>
      <div class="overflow-x-auto font-mono" data-testid="sequence-bases"><div v-for="(row,i) in rows" :key="i" class="mb-4 flex min-w-max gap-3"><span class="w-16 pt-5 text-right text-xs text-slate-400">{{ row[0]?.position }}</span><div v-for="(group,j) in groups(row)" :key="j" class="border-l pl-1"><p class="mb-1 text-[10px] text-slate-400">{{ group[0]?.position }}</p><div class="flex"><span v-for="b in group" :key="b.position" :title="`位置 ${b.position}，${b.base}${b.quality===undefined?'':`，Q${b.quality}`}`" class="inline-flex h-6 w-4 items-center justify-center rounded-sm font-semibold" :class="{'bg-yellow-200 ring-1 ring-yellow-500':b.hit,'underline decoration-wavy':b.quality!==undefined&&b.quality<20}" :style="{color:baseColor(b.base)}">{{ b.base }}</span></div><div v-if="displayed.sequence.qualities" class="mt-1 flex h-8 items-end" aria-label="逐碱基质量轨道"><span v-for="b in group" :key="b.position" class="mx-0.5 w-3 rounded-t" :title="`位置 ${b.position} Q${b.quality}`" :style="{height:`${Math.max(3,100*(b.quality??0)/qualityCeiling)}%`,backgroundColor:(b.quality??0)<20?'#ef4444':(b.quality??0)<30?'#f59e0b':'#10b981'}"></span></div></div></div></div>
      <p v-if="copyStatus" role="status" class="text-xs">{{ copyStatus }}</p>
    </div>
  </section>
</template>
<script setup lang="ts">
import {computed,ref,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {copyToClipboard} from '../../utils/dom';
import {BIO_WARNINGS,sequenceBases} from './sequenceBrowserData';
import {useBioPreview} from './useBioPreview';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const {catalog,displayed,busy,error,clear,loadData}=useBioPreview(()=>props.file,()=>props.plugin,'sequence-browser');
const recordId=ref(0),start=ref(1),count=ref(250),motif=ref(''),encoding=ref('phred33'),hitIndex=ref(-1),copyStatus=ref('');
const current=computed(()=>catalog.value?.records[recordId.value]),bases=computed(()=>displayed.value?sequenceBases(displayed.value):[]),rows=computed(()=>{const out=[];for(let i=0;i<bases.value.length;i+=60)out.push(bases.value.slice(i,i+60));return out;});
type Base=(typeof bases.value)[number];
const groups=(row:Base[])=>{const out=[];for(let i=0;i<row.length;i+=10)out.push(row.slice(i,i+10));return out;};
const qualityCeiling=computed(()=>Math.max(42,...(displayed.value?.sequence?.qualities??[])));
const baseColor=(b:string)=>({A:'#2563eb',C:'#16a34a',G:'#d97706',T:'#dc2626',U:'#dc2626',N:'#7c3aed'}[b]??'#64748b');
async function loadWindow(){copyStatus.value='';await loadData({record:recordId.value,start:start.value,count:count.value,motif:motif.value.trim().toUpperCase(),quality_encoding:current.value?.qualities?encoding.value:null});}
async function navigate(position:number){if(!current.value)return;start.value=Math.max(1,Math.min(current.value.length,Math.floor(position)));await loadWindow();}
function overviewJump(event:MouseEvent){if(!current.value||busy.value)return;const bounds=(event.currentTarget as HTMLElement).getBoundingClientRect();void navigate(Math.floor((event.clientX-bounds.left)/Math.max(1,bounds.width)*current.value.length)-Math.floor(count.value/2)+1);}
async function searchMotif(){hitIndex.value=-1;await loadWindow();}
async function moveHit(direction:number){const hits=displayed.value?.sequence?.search.positions;if(!hits?.length)return;hitIndex.value=hitIndex.value<0?0:(hitIndex.value+direction+hits.length)%hits.length;await navigate(hits[hitIndex.value]!);}
async function copyWindow(){const text=displayed.value?.sequence?.bases;if(!text)return;try{await copyToClipboard(text);copyStatus.value='窗口已复制。';}catch{copyStatus.value='复制失败，请检查剪贴板权限。';}}
watch(catalog,()=>{recordId.value=0;start.value=1;motif.value='';hitIndex.value=-1;copyStatus.value='';},{flush:'sync'});
watch(recordId,()=>{start.value=1;motif.value='';hitIndex.value=-1;},{flush:'sync'});
watch([recordId,start,count,motif,encoding],()=>{clear();copyStatus.value='';},{flush:'sync'});
</script>
