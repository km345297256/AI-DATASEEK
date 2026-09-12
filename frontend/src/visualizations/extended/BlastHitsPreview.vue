<template>
  <section class="min-h-0 flex-1 overflow-auto bg-slate-50 p-4 text-sm text-slate-700" data-testid="blast-hits">
    <p role="note" class="mb-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs">{{ BIO_WARNINGS['blast-hits'] }}</p>
    <form v-if="catalog" class="flex flex-wrap items-end gap-3 rounded border bg-white p-3" @submit.prevent="filterHits">
      <label>Query <select v-model="query" aria-label="BLAST Query" class="block max-w-[260px] rounded border p-1"><option :value="null">全部 Query</option><option v-for="q in catalog.queries" :key="q.id" :value="q.id">{{ q.name }} · {{ q.hits }} 命中</option></select></label>
      <label>最低一致性 (%) <input v-model.number="minIdentity" aria-label="最低一致性" type="number" min="0" max="100" step="any" class="block w-28 rounded border p-1" /></label>
      <label>最低覆盖度 (%) <input v-model.number="minCoverage" aria-label="最低覆盖度" type="number" min="0" max="100" step="any" class="block w-28 rounded border p-1" /></label>
      <label>每页 <select v-model.number="count" aria-label="BLAST每页数量" class="block rounded border p-1"><option v-for="n in [50,100,200,500]" :key="n" :value="n">{{ n }}</option></select></label>
      <button :disabled="busy" class="rounded bg-slate-800 px-3 py-2 text-white disabled:opacity-40" type="submit">筛选并显示命中</button>
    </form>
    <p v-if="busy" role="status" class="p-3">正在验证来源与命中过滤条件…</p><p v-if="error" role="alert" class="p-3 text-amber-700">{{ error }}</p>
    <template v-if="displayed?.hits">
      <div class="my-3 flex flex-wrap items-center gap-3 text-xs"><span data-testid="blast-total">筛选后 {{ displayed.hits.total }} 条 · 当前 {{ offset+1 }}–{{ offset+displayed.hits.rows.length }}</span><span v-if="displayed.hits.unknown_coverage">{{ displayed.hits.unknown_coverage }} 条缺少 Query 全长，覆盖度未知；正阈值会排除。</span><button class="ml-auto rounded border bg-white px-3 py-1" @click="copyTable">复制本页结果</button></div>
      <div v-if="selected" class="mb-3 rounded border border-blue-200 bg-blue-50 p-3 text-xs" data-testid="blast-detail"><div class="flex justify-between"><strong>{{ catalog?.queries[selected.query]?.name }} → {{ selected.subject }}</strong><button aria-label="关闭命中详情" @click="selected=undefined">×</button></div><p>一致性 {{ selected.identity }}% · 覆盖度 {{ coverageLabel(selected) }} · E-value {{ selected.evalue }} · Bit score {{ selected.bitscore }}</p><p>Query {{ selected.qstart }}–{{ selected.qend }}（{{ selected.qstart>selected.qend?'反向':'正向' }}）；Subject {{ selected.sstart }}–{{ selected.send }}（{{ selected.sstart>selected.send?'反向':'正向' }}）；比对长度 {{ selected.alignment_length }}，错配 {{ selected.mismatches }}，gap opens {{ selected.gap_opens }}</p></div>
      <div class="overflow-x-auto rounded border bg-white p-3"><svg :viewBox="`0 0 1100 ${Math.max(100,displayed.hits.rows.length*48+35)}`" class="min-w-[760px]" aria-label="BLAST命中区间图"><text x="8" y="16" font-size="11" fill="#475569">Query / Subject</text><text x="260" y="16" font-size="11" fill="#475569">逐 Query 坐标尺；箭头为 Query 方向，含终点的原始命中区间</text>
        <g v-for="(h,i) in displayed.hits.rows" :key="h.id" :data-blast-hit="h.id" class="cursor-pointer" @click="selected=h"><text x="8" :y="43+i*48" font-size="11" fill="#334155">{{ shorten(catalog?.queries[h.query]?.name??'') }}</text><text x="8" :y="58+i*48" font-size="10" fill="#64748b">{{ shorten(h.subject) }}</text><line x1="260" x2="1080" :y1="42+i*48" :y2="42+i*48" stroke="#cbd5e1"/><rect :x="260+range(h).left*820" :y="32+i*48" :width="Math.max(2,range(h).width*820)" height="18" rx="3" :fill="color(h.identity)"/><path :d="arrow(h,41+i*48)" fill="#0f172a"/><text x="1080" :y="61+i*48" text-anchor="end" font-size="9" fill="#64748b">1–{{ catalog?.queries[h.query]?.length??catalog?.queries[h.query]?.extent }} · {{ catalog?.queries[h.query]?.length===null?'仅观测命中范围':'已声明 Query 全长' }}</text><title>{{ h.subject }} · {{ h.identity }}% · {{ coverageLabel(h) }}</title></g>
      </svg><p v-if="!displayed.hits.rows.length" class="p-4 text-center">没有满足条件的命中。</p></div>
      <div class="mt-3 overflow-x-auto"><table class="w-full min-w-[760px] border-collapse bg-white text-left text-xs" data-testid="blast-table"><thead><tr><th v-for="c in ['Query','Subject','一致性','覆盖度','E-value','比对长度','Query方向','Subject方向']" :key="c" class="border-b p-2">{{ c }}</th></tr></thead><tbody><tr v-for="h in displayed.hits.rows" :key="h.id" class="border-b"><td class="p-2">{{ catalog?.queries[h.query]?.name }}</td><td class="p-2"><button class="text-blue-700 underline" @click="selected=h">{{ h.subject }}</button></td><td class="p-2">{{ h.identity.toFixed(2) }}%</td><td class="p-2">{{ coverageLabel(h) }}</td><td class="p-2">{{ h.evalue }}</td><td class="p-2">{{ h.alignment_length }}</td><td class="p-2">{{ h.qstart>h.qend?'−':'+' }}</td><td class="p-2">{{ h.sstart>h.send?'−':'+' }}</td></tr></tbody></table></div>
      <div class="my-3 flex gap-3"><button :disabled="busy||offset===0" class="rounded border bg-white px-3 py-1 disabled:opacity-40" @click="page(-1)">上一页</button><button :disabled="busy||offset+count>=displayed.hits.total" class="rounded border bg-white px-3 py-1 disabled:opacity-40" @click="page(1)">下一页</button><p v-if="copyStatus" role="status" class="text-xs">{{ copyStatus }}</p></div>
    </template>
  </section>
</template>
<script setup lang="ts">
import {ref,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {copyToClipboard} from '../../utils/dom';
import {BIO_WARNINGS,blastRange,type BlastHit} from './sequenceBrowserData';
import {useBioPreview} from './useBioPreview';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const {catalog,displayed,busy,error,clear,loadData}=useBioPreview(()=>props.file,()=>props.plugin,'blast-hits');
const query=ref<number|null>(null),minIdentity=ref(0),minCoverage=ref(0),offset=ref(0),count=ref(100),selected=ref<BlastHit>(),copyStatus=ref('');
const coverageLabel=(h:BlastHit)=>h.coverage===null?'未知':`${h.coverage.toFixed(2)}%`;
const range=(h:BlastHit)=>blastRange(h,catalog.value!.queries[h.query]!);
const color=(v:number)=>v>=95?'#15803d':v>=80?'#2563eb':v>=60?'#d97706':'#dc2626';
const shorten=(s:string)=>s.length>28?s.slice(0,27)+'…':s;
const arrow=(h:BlastHit,y:number)=>{const r=range(h),x=260+(r.reverse?r.left:r.left+r.width)*820,d=r.reverse?6:-6;return `M ${x} ${y} L ${x+d} ${y-5} L ${x+d} ${y+5} Z`;};
async function loadHits(){selected.value=undefined;copyStatus.value='';await loadData({query:query.value,min_identity:minIdentity.value,min_coverage:minCoverage.value,offset:offset.value,count:count.value});}
async function filterHits(){offset.value=0;await loadHits();}
async function page(direction:number){offset.value=Math.max(0,offset.value+direction*count.value);await loadHits();}
async function copyTable(){if(!displayed.value?.hits)return;const text=['Query\tSubject\tIdentity\tCoverage\tE-value\tAlignmentLength',...displayed.value.hits.rows.map(h=>`${catalog.value?.queries[h.query]?.name}\t${h.subject}\t${h.identity}\t${h.coverage??'unknown'}\t${h.evalue}\t${h.alignment_length}`)].join('\n');try{await copyToClipboard(text);copyStatus.value='本页结果已复制。';}catch{copyStatus.value='复制失败，请检查剪贴板权限。';}}
watch(catalog,()=>{query.value=null;offset.value=0;selected.value=undefined;copyStatus.value='';},{flush:'sync'});
watch([query,minIdentity,minCoverage,count],()=>{offset.value=0;clear();selected.value=undefined;},{flush:'sync'});
</script>
