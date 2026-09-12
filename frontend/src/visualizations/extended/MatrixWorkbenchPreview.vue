<template>
  <section class="matrix-preview">
    <header>
      <strong>高维矩阵工作台</strong>
      <select v-model="variable" aria-label="数组变量" @change="selectVariable">
        <option v-for="item in preparation?.arrays" :key="item.id" :value="item.id">{{ item.name }} · {{ item.shape.join(' × ') || '标量' }}</option>
      </select>
      <template v-if="(active?.shape.length || 0) >= 2">
        <label>行轴 <select v-model.number="axes[0]" @change="resetRegion"><option v-for="(_, i) in active?.shape" :key="i" :value="i">{{ i }}</option></select></label>
        <label>列轴 <select v-model.number="axes[1]" @change="resetRegion"><option v-for="(_, i) in active?.shape" :key="i" :value="i">{{ i }}</option></select></label>
      </template>
      <select v-model="component" aria-label="复数分量" @change="scheduleRender"><option value="real">实部</option><option value="imaginary">虚部</option><option value="magnitude">幅值</option><option value="phase">相位（弧度）</option></select>
      <select v-model="mode" aria-label="显示方式"><option value="heatmap">热力图</option><option value="table">数值表格</option></select>
      <select v-model="palette" aria-label="色带"><option value="blue">蓝红色带</option><option value="gray">灰度</option></select>
      <label><input v-model="logScale" type="checkbox" />对称对数</label>
      <label><input v-model="structure" type="checkbox" @change="scheduleRender" />非零结构</label>
      <button @click="resetRegion">恢复完整区域</button>
      <button @click="zoom = Math.min(8, zoom * 1.5)">放大</button><button @click="zoom = Math.max(1, zoom / 1.5)">缩小</button><button @click="zoom = 1">适应窗口</button>
      <label>每轴点数 <select v-model.number="maxPoints" aria-label="每轴点数" @change="scheduleRender"><option :value="32">32</option><option :value="128">128</option><option :value="256">256</option><option :value="512">512</option></select></label><button :disabled="busy || !preparation" @click="render">显示矩阵切片</button><button :disabled="busy || !preparation || !plane" @click="readCurves">读取已保存结果曲线</button>
    </header>
    <div v-if="active" class="slices">
      <span>{{ active.dtype }} · {{ active.sparse ? '稀疏矩阵' : `${active.shape.length} 维数组` }} · 索引从 0 开始</span>
      <label v-for="axis in fixedAxes" :key="axis">轴 {{ axis }}
        <input v-model.number="indices[axis]" type="range" min="0" :max="active.shape[axis] - 1" @input="scheduleRender" />
        <input v-model.number="indices[axis]" type="number" min="0" :max="active.shape[axis] - 1" @change="scheduleRender" />
      </label>
      <template v-if="fixedAxes.length">
        <button @click="togglePlay">{{ playing ? '暂停切片' : '播放切片' }}</button>
        <select v-model.number="playDelay" aria-label="播放间隔"><option :value="500">0.5 秒</option><option :value="1000">1 秒</option><option :value="2000">2 秒</option></select>
      </template>
      <template v-if="active.shape.length === 3">
        <button v-for="pair in [[0, 1], [0, 2], [1, 2]]" :key="pair.join()" @click="axes = pair; resetRegion()">切面 {{ pair.join(' / ') }}</button>
      </template>
    </div>
    <p role="note">本插件每次有界读取整个文件（最多 128 MiB），不是大文件范围读取。不执行任何矩阵分析。首次仅读取目录，请选择后显示切片。</p><p v-if="curveStatus" role="status">{{ curveStatus }}</p><p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="busy" role="status">正在读取矩阵切片…</p>
    <div v-if="plane" class="viewport">
      <div v-if="orthogonalPlanes.length" class="profiles">
        <figure v-for="slice in orthogonalPlanes" :key="slice.axes.join()">
          <figcaption>联动切面 {{ slice.axes.join(' / ') }}（点击定位）</figcaption>
          <svg viewBox="0 0 128 128" role="img" aria-label="张量正交切面" @click="locateSlice($event, slice)">
            <template v-for="(row, y) in slice.plane.values" :key="y">
              <rect v-for="(value, x) in row" :key="x" :x="Number(x) * 128 / row.length" :y="y * 128 / slice.plane.values.length" :width="128 / row.length + .1" :height="128 / slice.plane.values.length + .1" :fill="cellColor(value, slice.plane)" />
            </template>
          </svg>
        </figure>
      </div>
      <div v-if="mode === 'heatmap'" class="heatmap" :style="{ width: `${zoom * 100}%` }">
        <canvas ref="canvas" @mousemove="hover" @pointerdown="startSelection" @pointerup="finishSelection" @pointercancel="selectionStart = null" @mouseleave="hoverText = ''" aria-label="矩阵切片热力图，拖动框选原始区域，移动指针查看坐标和值" />
      </div>
      <table v-else>
        <thead><tr><th>行 / 列</th><th v-for="column in plane.columns" :key="column">{{ column }}</th></tr></thead>
        <tbody><tr v-for="(row, i) in plane.values" :key="i"><th>{{ plane.rows[i] }}</th><td v-for="(value, j) in row" :key="j">{{ format(value) }}</td></tr></tbody>
      </table>
      <div class="profiles">
        <figure v-for="profile in [{ title: '行均值剖面', values: plane.row_profile }, { title: '列均值剖面', values: plane.column_profile }]" :key="profile.title">
          <figcaption>{{ profile.title }}</figcaption>
          <svg viewBox="0 0 400 100" role="img" :aria-label="profile.title"><polyline v-for="(points,index) in matrixProfileSegments(profile.values)" :key="index" :points="points" fill="none" stroke="#38bdf8" stroke-width="2" /></svg>
        </figure>
      </div>
      <div v-if="preparation?.curves?.length" class="profiles">
        <figure v-for="curve in preparation.curves" :key="curve.title">
          <figcaption>{{ curve.title }}</figcaption>
          <svg viewBox="0 0 400 100" role="img" :aria-label="curve.title">
            <template v-if="curve.kind === 'scatter'">
              <circle v-for="(point, i) in matrixScatterPoints(curve.x, curve.y)" :key="i" :cx="point[0]" :cy="point[1]" r="3" fill="#38bdf8"><title>{{ curve.x[i] }} + {{ curve.y[i] }}i</title></circle>
            </template>
            <polyline v-for="(points,index) in curve.kind === 'line' ? matrixProfileSegments(curve.y) : []" :key="index" :points="points" fill="none" stroke="#38bdf8" stroke-width="2" />
          </svg>
        </figure>
      </div>
    </div>
    <footer v-if="plane">
      <div class="legend" :style="{ background: palette === 'gray' ? 'linear-gradient(to right,#000,#fff)' : 'linear-gradient(to right,hsl(240,75%,48%),hsl(120,75%,48%),hsl(0,75%,48%))' }" />
      <span>最小 {{ format(plane.minimum) }} · 最大 {{ format(plane.maximum) }} · 均值 {{ format(plane.mean) }} · 标准差 {{ format(plane.standard_deviation) }} · 有效点 {{ plane.finite_count }}/{{ plane.count }}</span>
      <span>{{ hoverText || '拖动框选区域后重新读取原始数据；移动指针查看索引和值。' }}</span>
      <span>行 [{{ plane.row_range.join(', ') }}) · 列 [{{ plane.column_range.join(', ') }}) · 抽样步长 {{ plane.row_step }} × {{ plane.column_step }}</span>
      <span>{{ plane.sampled ? '等步长抽样预览，每轴点数受显式设置约束；统计仅基于显示样本，可能遗漏局部极值。' : '显示当前完整切片，统计仅针对当前切片。' }} 灰色表示非有限值。</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { displayError } from './domains/lifecycle';
import { parseMatrixData, matrixCatalogIdentity, matrixSelection, matrixProfileSegments, matrixScatterPoints, type MatrixData, type MatrixPlane, type MatrixSelection } from './matrixWorkbenchData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope=usePreviewLoad();
const preparation = shallowRef<MatrixData|null>(null), plane = shallowRef<MatrixPlane|null>(null);
const variable=ref(''),active=computed(()=>preparation.value?.arrays.find(a=>a.id===variable.value));
const axes=ref([0,1]),indices=ref<number[]>([]),fixedAxes=computed(()=>active.value?.shape.map((_,i)=>i).filter(i=>active.value!.shape.length>1&&!axes.value.includes(i))||[]);
const component=ref('real'),mode=ref('heatmap'),palette=ref('blue'),logScale=ref(false),zoom=ref(1),maxPoints=ref(256);
const busy=ref(false),error=ref(''),hoverText=ref(''),curveStatus=ref(''),canvas=ref<HTMLCanvasElement>();
const structure=ref(false),rowRange=ref<number[]>(),columnRange=ref<number[]>(),playing=ref(false),playDelay=ref(1000);
const selectionStart=ref<{x:number;y:number}|null>(null),orthogonalPlanes=shallowRef<{axes:number[];plane:MatrixPlane}[]>([]);
let pinnedVersion:string|undefined,timer:ReturnType<typeof setTimeout>|undefined,playTimer:ReturnType<typeof setTimeout>|undefined;
function format(value:number|null){return value==null?'—':Number(value.toPrecision(7)).toString();}
function stopPlay(){playing.value=false;clearTimeout(playTimer);}
function clearResult(){scope.begin();plane.value=null;orthogonalPlanes.value=[];busy.value=false;error.value='';hoverText.value='';}
function selectVariable(event?:Event){stopPlay();rowRange.value=undefined;columnRange.value=undefined;orthogonalPlanes.value=[];indices.value=active.value?.shape.map(()=>0)||[];const n=active.value?.shape.length||0;axes.value=n>=2?[n-2,n-1]:[0,1];if(active.value?.sparse)axes.value=[0,1];plane.value=null;zoom.value=1;if(event)scheduleRender();}
function resetRegion(){rowRange.value=undefined;columnRange.value=undefined;zoom.value=1;scheduleRender();}
function togglePlay(){if(playing.value){stopPlay();return;}playing.value=true;playNext();}
function playNext(){clearTimeout(playTimer);if(!playing.value||!active.value||!fixedAxes.value.length)return;playTimer=setTimeout(async()=>{if(!playing.value||!active.value)return;if(!busy.value){const axis=fixedAxes.value[0];indices.value[axis]=(indices.value[axis]+1)%active.value.shape[axis];await render();}playNext();},playDelay.value);}
function scheduleRender(){clearTimeout(timer);clearResult();if(preparation.value&&pinnedVersion)timer=setTimeout(()=>void render(),200);}
function selection(pair=axes.value,points=maxPoints.value,full=false):MatrixSelection{
 if(!active.value)throw new Error('请先选择有效变量。');
 return matrixSelection(active.value,{variable:variable.value,axes:[...pair],indices:[...indices.value],component:component.value,row_range:full?null:rowRange.value?[...rowRange.value]:null,column_range:full?null:columnRange.value?[...columnRange.value]:null,max_points:points,structure:structure.value});
}
async function render(){
 clearTimeout(timer);if(!preparation.value||!pinnedVersion||!props.plugin.enabled)return;
 const expected=preparation.value,version=pinnedVersion,load=scope.begin();busy.value=true;error.value='';plane.value=null;orthogonalPlanes.value=[];hoverText.value='';
 try{
  const options=selection();
  const response=await requestVisualization(props.file,props.plugin,'preview',{kind:'image',version,...options},load.signal);load.assertCurrent();
  if(response.version!==version)throw new Error('文件版本已变化，请重新打开预览。');
  const result=parseMatrixData(response,'image',options,props.file.size,props.file.filename,props.plugin.id);
  if(matrixCatalogIdentity(result)!==matrixCatalogIdentity(expected))throw new Error('变量目录发生变化，请重新打开预览。');
  const slices:{axes:number[];plane:MatrixPlane}[]=[];
  if(active.value?.shape.length===3){
   // Sequential bounded calls respect the same isolated-parser concurrency pool.
   for(const pair of [[0,1],[0,2],[1,2]]){
    const chosen=selection(pair,32,true);
    const r=await requestVisualization(props.file,props.plugin,'preview',{kind:'image',version,...chosen},load.signal);load.assertCurrent();
    if(r.version!==version)throw new Error('文件版本已变化，请重新打开预览。');
    const parsed=parseMatrixData(r,'image',chosen,props.file.size,props.file.filename,props.plugin.id);
    if(matrixCatalogIdentity(parsed)!==matrixCatalogIdentity(expected))throw new Error('变量目录发生变化。');
    slices.push({axes:pair,plane:parsed.plane!});
   }
  }
  plane.value=result.plane;orthogonalPlanes.value=slices;await nextTick();load.assertCurrent();paint();
 }catch(reason){if(load.isCurrent()){plane.value=null;orthogonalPlanes.value=[];error.value=displayError(reason);stopPlay();}}
 finally{if(load.isCurrent())busy.value=false;}
}
async function readCurves(){
 if(busy.value||!preparation.value||!pinnedVersion||!props.plugin.enabled)return;
 const expected=preparation.value,version=pinnedVersion,load=scope.begin();busy.value=true;error.value='';curveStatus.value='';stopPlay();
 try{
  const r=await requestVisualization(props.file,props.plugin,'preview',{kind:'series',version},load.signal);load.assertCurrent();
  if(r.version!==version)throw new Error('文件版本已变化，请重新打开预览。');
  const parsed=parseMatrixData(r,'series',{},props.file.size,props.file.filename,props.plugin.id);
  if(matrixCatalogIdentity(parsed)!==matrixCatalogIdentity(expected))throw new Error('变量目录发生变化。');
  preparation.value={...expected,curves:parsed.curves};curveStatus.value=parsed.curves.length?'这些曲线来自文件内已保存的数值，不是现场计算结果。':'未找到可显示的有限数值 S、singular_values、residual_history 或 eigenvalues（每条最多 4096 点）。';
 }catch(reason){if(load.isCurrent())error.value=displayError(reason);}finally{if(load.isCurrent())busy.value=false;}
}
function cellColor(value:number|null,p:MatrixPlane){
 if(value===null)return '#71717a';const low=p.minimum??0,high=p.maximum??1,scale=Math.max(Math.abs(low),Math.abs(high))||1;
 const fraction=high===low?.5:(value/scale-low/scale)/(high/scale-low/scale);
 return `hsl(${240*(1-fraction)} 75% 48%)`;
}
function locateSlice(event:MouseEvent,slice:{axes:number[];plane:MatrixPlane}){const r=(event.currentTarget as SVGElement).getBoundingClientRect();const x=Math.min(slice.plane.columns.length-1,Math.max(0,Math.floor((event.clientX-r.left)/r.width*slice.plane.columns.length))),y=Math.min(slice.plane.rows.length-1,Math.max(0,Math.floor((event.clientY-r.top)/r.height*slice.plane.rows.length)));indices.value[slice.axes[0]]=slice.plane.rows[y];indices.value[slice.axes[1]]=slice.plane.columns[x];scheduleRender();}
function canvasPoint(event:PointerEvent){if(!canvas.value||!plane.value)return null;const r=canvas.value.getBoundingClientRect();return {x:Math.max(0,Math.min(plane.value.columns.length-1,Math.floor((event.clientX-r.left)/r.width*plane.value.columns.length))),y:Math.max(0,Math.min(plane.value.rows.length-1,Math.floor((event.clientY-r.top)/r.height*plane.value.rows.length)))};}
function startSelection(event:PointerEvent){if(event.button!==0)return;selectionStart.value=canvasPoint(event);canvas.value?.setPointerCapture(event.pointerId);}
function finishSelection(event:PointerEvent){const start=selectionStart.value,end=canvasPoint(event),p=plane.value;selectionStart.value=null;if(!start||!end||!p||(start.x===end.x&&start.y===end.y))return;stopPlay();rowRange.value=[p.rows[Math.min(start.y,end.y)],Math.min(p.row_range[1],p.rows[Math.max(start.y,end.y)]+p.row_step)];columnRange.value=[p.columns[Math.min(start.x,end.x)],Math.min(p.column_range[1],p.columns[Math.max(start.x,end.x)]+p.column_step)];scheduleRender();}
function paint(){
 if(!canvas.value||!plane.value)return;const p=plane.value,c=canvas.value;c.width=p.columns.length;c.height=p.rows.length;
 const ctx=c.getContext('2d');if(!ctx)return;const transform=(v:number)=>logScale.value?Math.sign(v)*Math.log1p(Math.abs(v)):v;
 const low=transform(p.minimum??0),high=transform(p.maximum??1),scale=Math.max(Math.abs(low),Math.abs(high))||1;
 p.values.forEach((row,y)=>row.forEach((value,x)=>{const fraction=value===null?0:high===low?.5:(transform(value)/scale-low/scale)/(high/scale-low/scale);ctx.fillStyle=value===null?'#71717a':palette.value==='gray'?`hsl(0 0% ${fraction*100}%)`:`hsl(${240*(1-fraction)} 75% 48%)`;ctx.fillRect(x,y,1,1);}));c.dataset.matrixValues=String(p.count);
}
function hover(event:MouseEvent){if(!canvas.value||!plane.value)return;const r=canvas.value.getBoundingClientRect(),p=plane.value;const x=Math.min(p.columns.length-1,Math.max(0,Math.floor((event.clientX-r.left)/r.width*p.columns.length))),y=Math.min(p.rows.length-1,Math.max(0,Math.floor((event.clientY-r.top)/r.height*p.rows.length)));const point=[...indices.value];if(point.length>=2){point[axes.value[0]]=p.rows[y];point[axes.value[1]]=p.columns[x];}else if(point.length)point[0]=p.columns[x];hoverText.value=`[${point.join(', ')}] = ${format(p.values[y][x])}`;}
async function inspect(){
 clearTimeout(timer);stopPlay();const load=scope.begin();preparation.value=null;plane.value=null;orthogonalPlanes.value=[];pinnedVersion=undefined;error.value='';curveStatus.value='';busy.value=false;
 if(!props.plugin.enabled)return;busy.value=true;
 try{const response=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},load.signal);load.assertCurrent();preparation.value=parseMatrixData(response,'tree',{},props.file.size,props.file.filename,props.plugin.id);pinnedVersion=response.version;variable.value=preparation.value.arrays[0].id;selectVariable();}
 catch(reason){if(load.isCurrent())error.value=displayError(reason);}finally{if(load.isCurrent())busy.value=false;}
}
watch([mode,palette,logScale],async()=>{await nextTick();paint();});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>void inspect(),{immediate:true,flush:'sync'});
onScopeDispose(()=>{scope.dispose();clearTimeout(timer);stopPlay();preparation.value=null;plane.value=null;orthogonalPlanes.value=[];pinnedVersion=undefined;if(canvas.value){canvas.value.width=0;canvas.value.height=0;}});
</script>

<style scoped>
.matrix-preview { display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; height:100%; background:#111827; color:#e5e7eb; font-size:12px; }
header,.slices { display:flex; flex-wrap:wrap; gap:8px; padding:10px; align-items:center; flex-shrink:0; border-bottom:1px solid #374151; }
label { display:flex; align-items:center; gap:4px; } select,button,input[type=number] { background:#1f2937; color:#f3f4f6; border:1px solid #4b5563; border-radius:4px; padding:4px; max-width:230px; } button:disabled { opacity:.5; }
input[type=number] { width:70px; } input[type=range] { width:100px; }
.viewport { flex:1; min-height:100px; min-width:0; overflow:auto; padding:12px; }
.heatmap { min-width:100%; } canvas { display:block; width:100%; min-height:100px; image-rendering:pixelated; touch-action:none; cursor:crosshair; }
.profiles { display:flex; flex-wrap:wrap; gap:12px; } figure { flex:1 1 220px; min-width:0; margin:12px 0; } figure svg { width:100%; max-height:120px; }
table { border-collapse:collapse; font-variant-numeric:tabular-nums; } td,th { padding:5px 8px; border:1px solid #374151; white-space:nowrap; } th { background:#1f2937; position:sticky; top:0; }
footer { flex-shrink:0; display:flex; flex-direction:column; gap:4px; padding:10px; border-top:1px solid #374151; overflow-wrap:anywhere; } .legend { height:8px; width:160px; } p { padding:8px; } .error { color:#fca5a5; }
</style>
