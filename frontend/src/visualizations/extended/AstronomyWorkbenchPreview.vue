<script setup lang="ts">
import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { displayError } from './domains/lifecycle';
import { ASTRONOMY_WARNING, astronomySpectrumSegments, parseAstronomyWorkbench, sameAstronomy, validateAstronomyOptions, type AstronomyData, type AstronomyKind, type AstronomyOptions } from './astronomyWorkbenchData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const loads=usePreviewLoad(),catalog=shallowRef<AstronomyData>(),rendered=shallowRef<Record<string,any>>(),table=shallowRef<Record<string,any>>(),spectrum=shallowRef<Record<string,any>>();
const pixel=shallowRef<Record<string,any>>(),region=shallowRef<Record<string,any>>(),sources=shallowRef<Record<string,any>>(),selectedSource=shallowRef<Record<string,any>>();
const busy=ref(false),error=ref(''),dataset=ref(0),slices=ref<number[]>([]),band=ref(1),stretch=ref('linear'),interval=ref('zscale'),colour=ref('gray'),invert=ref(false),low=ref(0),high=ref(1),sigma=ref(5),rowOffset=ref(0),columnOffset=ref(0);
const imageElement=ref<HTMLImageElement>(),zoom=ref(1),pan=ref([0,0]),mode=ref<'pixel'|'region'|'pan'>('pixel'),bounds=ref([0,0,1,1]),pixelX=ref(0),pixelY=ref(0),drag=ref<{x:number;y:number;pan:number[];pixel:number[]}|null>();
let version:string|undefined;
const active=computed(()=>catalog.value?.datasets.find(d=>d.index===dataset.value));
const leading=computed(()=>catalog.value?.format==='fits'&&active.value?.kind==='image'?active.value.shape.slice(0,-2):[]);
const histogram=computed(()=>{const values=rendered.value?.histogram.counts as number[]|undefined;if(!values)return '';const max=Math.max(...values,1);return values.map((v,i)=>`${i*240/95},${70-v*68/max}`).join(' ');});
const segments=computed(()=>spectrum.value?astronomySpectrumSegments(spectrum.value.indices,spectrum.value.values):[]);
const format=(v:unknown):string=>v===null||v===undefined?'—':typeof v==='number'?Number(v.toPrecision(7)).toLocaleString():Array.isArray(v)?v.map(format).join(', '):String(v);
const labels:Record<string,string>={valid_count:'有效标量',missing_count:'无效标量',minimum:'最小值',maximum:'最大值',mean:'均值',median:'中位数',std:'标准差',sum:'总和'};
function clearDisplays(){rendered.value=table.value=spectrum.value=pixel.value=region.value=sources.value=selectedSource.value=undefined;drag.value=null;zoom.value=1;pan.value=[0,0];}
function invalidate(){loads.begin();busy.value=false;error.value='';clearDisplays();}
async function inspect(){
  const task=loads.begin();clearDisplays();catalog.value=undefined;version=undefined;busy.value=false;error.value='';if(!props.plugin.enabled)return;busy.value=true;
  try {const response=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal);task.assertCurrent();if(response.kind!=='tree'||!/^[0-9a-f]{64}$/.test(response.version))throw new Error('科学影像结构版本无效。');const data=parseAstronomyWorkbench('tree',response.payload,response.metadata,{});if(data.sourceBytes!==props.file.size)throw new Error('文件大小发生变化，请重新检查。');dataset.value=data.datasets.find(d=>d.kind!=='empty')?.index??0;slices.value=[];band.value=1;rowOffset.value=columnOffset.value=0;version=response.version;catalog.value=data;slices.value=leading.value.map(()=>0);}
  catch(reason){if(task.isCurrent())error.value=displayError(reason);}finally{if(task.isCurrent())busy.value=false;}
}
function planeOptions(action:string):AstronomyOptions{return {dataset:dataset.value,slices:[...slices.value],band:band.value,action};}
async function execute(kind:AstronomyKind,raw:AstronomyOptions){
  if(busy.value)return;const task=loads.begin(),initial=catalog.value,pinned=version;error.value='';busy.value=true;
  try {if(!props.plugin.enabled||!initial||!pinned)throw new Error('请先检查科学影像结构。');const options=validateAstronomyOptions(kind,raw);const response=await requestVisualization(props.file,props.plugin,'preview',{kind,version:pinned,...options},task.signal);task.assertCurrent();const resultKind=kind==='image'?'raster':kind;if(response.version!==pinned||response.kind!==resultKind)throw new Error('科学影像版本或结果类型变化。');const data=parseAstronomyWorkbench(kind,response.payload,response.metadata,options);if(data.sourceBytes!==initial.sourceBytes||data.format!==initial.format||!sameAstronomy(data.datasets,initial.datasets)||!sameAstronomy(data.geospatial,initial.geospatial))throw new Error('结果与初始文件结构不一致。');
    if(kind==='table'){table.value=data.result;spectrum.value=undefined;rendered.value=undefined;}
    else if(kind==='series'){spectrum.value=data.result;table.value=undefined;rendered.value=undefined;}
    else if(options.action==='render'){rendered.value=data.result;pixel.value=region.value=sources.value=selectedSource.value=undefined;bounds.value=[0,0,data.result.width,data.result.height];pixelX.value=pixelY.value=0;}
    else if(options.action==='pixel')pixel.value=data.result;
    else if(options.action==='region')region.value=data.result;
    else sources.value=data.result;
  }catch(reason){if(task.isCurrent())error.value=displayError(reason);}finally{if(task.isCurrent())busy.value=false;}
}
function loadSelected(){if(active.value?.kind==='table')void execute('table',{dataset:dataset.value,row_offset:rowOffset.value,column_offset:columnOffset.value});else if(active.value?.kind==='spectrum')void execute('series',{dataset:dataset.value});else void execute('image',{...planeOptions('render'),stretch:stretch.value,interval:interval.value,low:interval.value==='manual'?low.value:null,high:interval.value==='manual'?high.value:null,colour_map:colour.value,invert:invert.value});}
function readPixel(){void execute('image',{...planeOptions('pixel'),x:pixelX.value,y:pixelY.value});}
function readRegion(){void execute('image',{...planeOptions('region'),bounds:[...bounds.value]});}
function detect(){if(sources.value){sources.value=selectedSource.value=undefined;return;}void execute('image',{...planeOptions('sources'),threshold_sigma:sigma.value});}
function cancel(){loads.begin();busy.value=false;error.value='读取已取消。';}
function zoomBy(factor:number){zoom.value=Math.max(.25,Math.min(12,zoom.value*factor));}
function sourcePoint(event:PointerEvent):number[]|null{const rect=imageElement.value?.getBoundingClientRect(),data=rendered.value;if(!rect||!data||rect.width<=0||rect.height<=0)return null;const x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;if(x<0||x>1||y<0||y>1)return null;return [Math.min(data.width-1,Math.floor(x*data.width)),Math.min(data.height-1,Math.floor(y*data.height))];}
function pointerDown(event:PointerEvent){if(busy.value||event.button!==0)return;const p=sourcePoint(event);if(!p)return;drag.value={x:event.clientX,y:event.clientY,pan:[...pan.value],pixel:p};(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);if(mode.value==='region')bounds.value=[p[0]!,p[1]!,p[0]!+1,p[1]!+1];}
function pointerMove(event:PointerEvent){const start=drag.value;if(!start)return;if(mode.value==='pan'){pan.value=[start.pan[0]!+event.clientX-start.x,start.pan[1]!+event.clientY-start.y];return;}if(mode.value==='region'){const p=sourcePoint(event);if(p)bounds.value=[Math.min(start.pixel[0]!,p[0]!),Math.min(start.pixel[1]!,p[1]!),Math.max(start.pixel[0]!,p[0]!)+1,Math.max(start.pixel[1]!,p[1]!)+1];}}
function pointerUp(event:PointerEvent){const start=drag.value;drag.value=null;if(!start)return;if(mode.value==='region')readRegion();else if(mode.value==='pixel'&&Math.abs(event.clientX-start.x)+Math.abs(event.clientY-start.y)<8){const p=sourcePoint(event);if(p){pixelX.value=p[0]!;pixelY.value=p[1]!;readPixel();}}}
watch(dataset,()=>{invalidate();slices.value=leading.value.map(()=>0);band.value=1;rowOffset.value=columnOffset.value=0;},{flush:'sync'});
watch([slices,band],()=>{invalidate();},{deep:true,flush:'sync'});
watch([stretch,interval,colour,invert,low,high],()=>{if(rendered.value||busy.value)invalidate();},{flush:'sync'});
watch([rowOffset,columnOffset],()=>{loads.begin();busy.value=false;table.value=undefined;error.value='';},{flush:'sync'});
watch(sigma,()=>{loads.begin();busy.value=false;sources.value=selectedSource.value=undefined;},{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect();},{immediate:true,flush:'sync'});
onScopeDispose(()=>{catalog.value=undefined;version=undefined;clearDisplays();});
</script>
<template><section class="astro-workbench">
  <p class="notice" role="note">{{ ASTRONOMY_WARNING }}</p>
  <p class="notice">FITS/TIFF 工作台 · 整文件最多 32 MiB（gzip 解压后也需 ≤32 MiB），每平面最多 2,097,152 个标量；不是大型文件流式预览。坐标零起始、行向下；选区右/下边界不包含。近似 Viridis 与显示插值仅用于观察。</p>
  <header><button :disabled="busy||!plugin.enabled" @click="inspect">重新检查影像结构</button><label v-if="catalog">HDU / 页面<select v-model.number="dataset" aria-label="科学影像 HDU 或页面"><option v-for="d in catalog.datasets" :key="d.index" :value="d.index">{{ d.index }} · {{ d.name }} · {{ d.kind }} · {{ d.shape.join('×') }}</option></select></label></header>
  <template v-if="active&&active.kind!=='empty'">
    <div v-if="active.kind==='image'" class="controls">
      <label v-for="(size,index) in leading" :key="index">切片轴 {{ index+1 }}<input v-model.number="slices[index]" :aria-label="`科学影像切片轴 ${index+1}`" type="number" min="0" :max="size-1" /></label>
      <label v-if="catalog?.format==='tiff'">通道<select v-model.number="band" aria-label="科学影像通道"><option v-if="active.channels>=3" :value="0">RGB 合成（前 3 通道）</option><option v-for="n in active.channels" :key="n" :value="n">{{ n }}</option></select></label>
      <label>范围<select v-model="interval" aria-label="科学影像显示范围"><option value="zscale">ZScale</option><option value="percentile">1–99%</option><option value="manual">手动</option></select></label>
      <label>拉伸<select v-model="stretch" aria-label="科学影像拉伸"><option value="linear">线性</option><option value="log">对数</option><option value="sqrt">平方根</option><option value="asinh">Asinh</option></select></label>
      <label>色带<select v-model="colour" aria-label="科学影像色带"><option value="gray">灰度</option><option value="viridis">近似 Viridis</option><option value="heat">热力</option><option value="cool">冷色</option></select></label><label><input v-model="invert" type="checkbox" />反色</label>
      <template v-if="interval==='manual'"><label>最小<input v-model.number="low" type="number" step="any" aria-label="科学影像最小值" /></label><label>最大<input v-model.number="high" type="number" step="any" aria-label="科学影像最大值" /></label></template>
    </div>
    <div v-if="active.kind==='table'" class="controls"><label>起始行<input v-model.number="rowOffset" type="number" min="0" :max="active.row_count" aria-label="FITS 表起始行" /></label><label>起始列<input v-model.number="columnOffset" type="number" min="0" :max="active.columns.length" aria-label="FITS 表起始列" /></label></div>
    <button class="load" :disabled="busy||!plugin.enabled" @click="loadSelected">{{ active.kind==='image'?'渲染当前影像':active.kind==='table'?'读取 FITS 表格页':'读取 FITS 一维曲线' }}</button>
  </template>
  <p v-if="active?.kind==='empty'" class="notice">此 HDU 没有数据，请选择其他 HDU。</p>
  <p v-if="busy" role="status">正在隔离环境中读取… <button @click="cancel">取消读取</button></p><p v-if="error" role="alert">{{ error }}</p>
  <div v-if="rendered" class="work-layout">
    <main><div class="controls"><button @click="zoomBy(1.3)">放大</button><button @click="zoomBy(1/1.3)">缩小</button><button @click="zoom=1;pan=[0,0]">复位</button><label>鼠标<select v-model="mode" aria-label="科学影像鼠标模式"><option value="pixel">点击像素</option><option value="region">框选统计</option><option value="pan">平移</option></select></label><span>{{ rendered.width }}×{{ rendered.height }} · {{ Math.round(zoom*100) }}%</span></div>
      <div class="viewport" @wheel.prevent="zoomBy($event.deltaY<0?1.1:1/1.1)"><div class="image-wrap" :style="{'--aspect':rendered.width/rendered.height,transform:`translate(${pan[0]}px,${pan[1]}px) scale(${zoom})`}" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="drag=null">
        <img ref="imageElement" :src="`data:image/png;base64,${rendered.image_base64}`" alt="科学影像所选平面" data-testid="astronomy-workbench-image" draggable="false" />
        <div v-if="mode==='region'" class="region-box" :style="{left:`${bounds[0]!/rendered.width*100}%`,top:`${bounds[1]!/rendered.height*100}%`,width:`${(bounds[2]!-bounds[0]!)/rendered.width*100}%`,height:`${(bounds[3]!-bounds[1]!)/rendered.height*100}%`}" />
        <button v-for="p in sources?.sources||[]" :key="p.id" class="source" :style="{left:`${(p.x+.5)/rendered.width*100}%`,top:`${(p.y+.5)/rendered.height*100}%`}" :title="`峰候选 ${p.id}`" @pointerdown.stop @click.stop="selectedSource=p" />
      </div></div>
      <div class="controls"><label>x<input v-model.number="pixelX" type="number" min="0" :max="rendered.width-1" aria-label="科学影像像素 x" /></label><label>y<input v-model.number="pixelY" type="number" min="0" :max="rendered.height-1" aria-label="科学影像像素 y" /></label><button :disabled="busy" @click="readPixel">读取像素</button></div>
      <div class="controls"><label v-for="(name,i) in ['x0','y0','x1','y1']" :key="name">{{ name }}<input v-model.number="bounds[i]" type="number" min="0" :aria-label="`科学影像选区 ${name}`" /></label><button :disabled="busy" @click="readRegion">读取区域统计</button></div>
      <div class="controls"><label>检测阈值 σ<input v-model.number="sigma" type="number" min="1" max="50" step="0.5" aria-label="源检测阈值" /></label><button :disabled="busy" @click="detect">{{ sources?'隐藏峰候选':'检测并叠加峰候选' }}</button><span v-if="sources">{{ sources.sources.length }} 个{{ sources.truncated?'（已截断）':'' }} · 背景 {{ format(sources.background) }} · 噪声 {{ format(sources.noise) }}</span></div>
    </main>
    <aside><section><h4>范围与直方图</h4><p>{{ rendered.display_limits.map((p:number[])=>p.map(format).join(' ～ ')).join('；') }}</p><svg viewBox="0 0 240 72" class="histogram"><polyline :points="histogram" fill="none" stroke="#34b3c5" stroke-width="2" /></svg><p class="notice">直方图使用 {{ rendered.histogram.sample_count }} 个有限标量，96 桶，仅计入显示范围内值；RGB 的统计按标量通道合并。</p></section>
      <section><h4>平面统计</h4><dl><template v-for="(value,key) in rendered.statistics" :key="key"><dt>{{ labels[String(key)]||key }}</dt><dd>{{ format(value) }}</dd></template></dl></section>
      <section v-if="pixel" data-testid="astronomy-workbench-pixel"><h4>像素与 WCS</h4><p>({{ pixel.x }}, {{ pixel.y }}) = {{ format(pixel.value) }}</p><p v-if="pixel.world">WCS 世界坐标 {{ format(pixel.world) }}（{{ active?.wcs.units.join(', ') }}）</p></section>
      <section v-if="region" data-testid="astronomy-workbench-region"><h4>选区统计</h4><p>{{ region.bounds.join(', ') }} · {{ region.pixel_count }} 个标量</p><dl><template v-for="(value,key) in region.statistics" :key="key"><dt>{{ labels[String(key)]||key }}</dt><dd>{{ format(value) }}</dd></template></dl></section>
      <section v-if="selectedSource"><h4>峰候选 {{ selectedSource.id }}</h4><p>({{ selectedSource.x }}, {{ selectedSource.y }}) · 峰 {{ format(selectedSource.peak) }} · S/N {{ format(selectedSource.snr) }}</p><p v-if="selectedSource.world">WCS {{ format(selectedSource.world) }}</p></section>
      <section v-if="rendered.wcs.corners"><h4>天球 WCS 四角</h4><p>{{ active?.wcs.axis_types.join(', ') }}（{{ active?.wcs.units.join(', ') }}）</p><p v-for="(point,i) in rendered.wcs.corners" :key="i">{{ format(point) }}</p></section>
    </aside>
  </div>
  <div v-if="table" class="table-wrap" data-testid="astronomy-workbench-table"><table><thead><tr><th v-for="c in table.columns" :key="c.index">{{ c.name }}<small>{{ c.unit }}</small></th></tr></thead><tbody><tr v-for="(row,i) in table.rows" :key="i"><td v-for="(value,j) in row" :key="j">{{ format(value) }}</td></tr></tbody></table><p>第 {{ table.row_offset }} 行起 {{ table.rows.length }} / {{ table.total_rows }} 行；{{ table.columns.length }} / {{ table.total_columns }} 列。固定向量 ≤100 项；变长和复数列明确拒绝。</p></div>
  <div v-if="spectrum" class="spectrum" data-testid="astronomy-workbench-spectrum"><svg viewBox="0 0 1000 420" preserveAspectRatio="none"><g v-for="(points,i) in segments" :key="i"><polyline :points="points" fill="none" stroke="#208fa5" stroke-width="2" vector-effect="non-scaling-stroke" /><circle v-if="!points.includes(' ')" :cx="Number(points.split(',')[0])" :cy="Number(points.split(',')[1])" r="3" fill="#208fa5" /></g></svg><p>{{ spectrum.total_points }} 个原始采样点，每 {{ spectrum.stride }} 点取一，显示 {{ spectrum.values.length }} 点；无效值断开，横轴为原始索引，不推断物理频率。</p></div>
  <section v-if="catalog?.geospatial" class="geo"><h4>GeoTIFF 空间信息（无在线底图）</h4><p>CRS {{ catalog.geospatial.crs }} · 分辨率 {{ format(catalog.geospatial.resolution) }} · 原始范围 {{ format(catalog.geospatial.bounds) }}</p><p>WGS84 范围 {{ format(catalog.geospatial.bounds_wgs84) }} · Nodata {{ format(catalog.geospatial.nodata) }}</p></section>
</section></template>
<style scoped>
.astro-workbench{padding:14px;display:flex;flex:1;min-height:0;overflow:auto;flex-direction:column;gap:10px;font-size:12px;color:#253448}.notice{font-size:11px;color:#667085;line-height:1.7}p,h4{margin:0}header,.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}label{display:flex;gap:5px;align-items:center}select,button,input{padding:5px;border:1px solid #c6cfd9;border-radius:4px;background:#fff}input[type=number]{width:80px}button:disabled{opacity:.5}.load{align-self:flex-start;background:#edf8f4}.work-layout{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:12px;flex:none}.work-layout main{min-width:0;display:flex;flex-direction:column;gap:10px}.viewport{height:430px;background:#10161e;overflow:hidden;display:flex;align-items:center;justify-content:center;touch-action:none}.image-wrap{position:relative;max-width:100%;max-height:100%;line-height:0;touch-action:none;user-select:none}.image-wrap img{display:block;max-width:100%;max-height:420px;object-fit:contain;user-select:none}.region-box{position:absolute;border:1px solid #5de2ee;background:#5de2ee22;pointer-events:none}.source{position:absolute;width:10px;height:10px;border:1px solid #f29ee6;border-radius:50%;transform:translate(-50%,-50%);padding:0;background:transparent}.histogram{height:72px;width:100%}aside{display:flex;flex-direction:column;gap:12px;min-width:0;overflow-wrap:anywhere}aside section,.geo{border:1px solid #e0e5eb;border-radius:5px;padding:8px}h4{font-weight:600;margin-bottom:5px}dl{display:grid;grid-template-columns:80px 1fr;gap:4px}dd{margin:0;overflow-wrap:anywhere}.table-wrap{overflow:auto}.table-wrap table{border-collapse:collapse;min-width:100%}td,th{padding:5px;border:1px solid #d7dce3;text-align:left;max-width:280px;overflow-wrap:anywhere}small{display:block;font-weight:400}.spectrum svg{width:100%;height:320px;border:1px solid #e0e5eb}[role=alert]{color:#b42318}@media(max-width:760px){.work-layout{grid-template-columns:minmax(0,1fr)}aside{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.viewport{height:320px}.image-wrap img{max-height:310px}}
.image-wrap{width:min(100%,calc(400px * var(--aspect)),720px)}.image-wrap img{width:100%;height:auto;max-height:none}@media(max-width:760px){.image-wrap{width:min(100%,calc(290px * var(--aspect)),720px)}}
</style>
