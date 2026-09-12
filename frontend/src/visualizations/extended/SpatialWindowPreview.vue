<script setup lang="ts">
import {computed,nextTick,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {loadBrowserLibrary} from './scientific/browserLibraries';
import {parseSpatialWindow,validateSpatialSelection,spatialCatalogMatches,type SpatialData} from './spatialWindowData';
const props=defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const scope=usePreviewLoad(),catalog=shallowRef<SpatialData>(),data=shallowRef<SpatialData>(),target=ref<HTMLDivElement>();
const feature=ref(0),observationStart=ref(0),observationCount=ref(1),busy=ref(false),error=ref('');
const current=computed(()=>data.value??catalog.value);let version:string|undefined;
async function inspect(){
 const request=scope.begin();catalog.value=undefined;data.value=undefined;version=undefined;busy.value=false;error.value='';
 feature.value=0;observationStart.value=0;observationCount.value=1;
 if(!props.plugin.enabled)return;busy.value=true;
 try{
  const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},request.signal);request.assertCurrent();
  if(result.kind!=='tree'||!/^[0-9a-f]{64}$/.test(result.version)||props.file.filename.split('.').pop()?.toLowerCase()!=='h5ad')throw new Error('AnnData 结构或版本无效。');
  const parsed=parseSpatialWindow('tree',result.payload,result.metadata);
  if(parsed.sourceBytes!==props.file.size)throw new Error('返回结构与当前文件大小不一致。');
  observationCount.value=Math.min(1024,parsed.observations);version=result.version;catalog.value=parsed;
 }catch(reason){if(request.isCurrent())error.value=reason instanceof Error?reason.message:'空间结构读取失败。'}
 finally{if(request.isCurrent())busy.value=false;}
}
async function loadWindow(){
 const request=scope.begin();data.value=undefined;error.value='';busy.value=true;
 try{
  if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先读取 AnnData 空间目录。');
  const initial=catalog.value,pinned=version,selected=validateSpatialSelection({feature:feature.value,observation_start:observationStart.value,observation_count:observationCount.value,decode:'raw'},initial);
  const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'geometry',version:pinned,...selected},request.signal);request.assertCurrent();
  if(result.kind!=='geometry'||result.version!==pinned)throw new Error('文件版本已变化，请重新读取目录。');
  const parsed=parseSpatialWindow('geometry',result.payload,result.metadata,selected);
  if(!spatialCatalogMatches(parsed,initial))throw new Error('返回点集与原目录不一致。');
  data.value=parsed;const s=parsed.spatial!;
  if([...s.x,...s.y,...s.values].some(v=>v!==null&&Math.abs(v)>1e100))throw new Error('原值超出当前线性图可靠范围；未重缩放或截断，请选择其他窗口。');
  const library=await loadBrowserLibrary('plotly',request.signal);await nextTick();request.assertCurrent();if(!target.value)return;
  const element=document.createElement('div');element.dataset.testid='spatial-plot';element.style.height='440px';target.value.replaceChildren(element);
  request.onDispose(()=>{library.purge(element);element.remove();});
  // Preserve ordinal positions in the payload. Only complete triples can draw.
  const visible=s.observations.map((_,i)=>i).filter(i=>s.x[i]!==null&&s.y[i]!==null&&s.values[i]!==null);
  const traces:import('plotly.js').Data[]=[{type:'scatter',mode:'markers',x:visible.map(i=>s.x[i]!),y:visible.map(i=>s.y[i]!),customdata:visible.map(i=>s.observations[i]!),
   marker:{size:7,opacity:.8,color:visible.map(i=>s.values[i]!),colorscale:'Viridis',showscale:true,colorbar:{title:{text:'X 存储原值'}}},connectgaps:false,
   text:visible.map(i=>String(s.values[i])),hovertemplate:'观测 %{customdata}<br>坐标0=%{x}<br>坐标1=%{y}<br>特征原值=%{text}<extra></extra>'}];
  await library.newPlot(element,traces,{margin:{l:90,r:70,t:20,b:75},showlegend:false,
   xaxis:{title:{text:'spatial 列 0（单位未知）'},type:'linear',automargin:true},
   yaxis:{title:{text:'spatial 列 1（单位未知）'},type:'linear',automargin:true,scaleanchor:'x',scaleratio:1}},
   {responsive:true,displayModeBar:false,displaylogo:false});
  if(!request.isCurrent()){library.purge(element);element.remove();return;}
  if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(()=>{if(request.isCurrent())void library.Plots.resize(element);});observer.observe(element);request.onDispose(()=>observer.disconnect());}
 }catch(reason){if(request.isCurrent())error.value=reason instanceof Error?reason.message:'空间窗口读取失败。';}
 finally{if(request.isCurrent())busy.value=false;}
}
watch([feature,observationStart,observationCount],()=>{if(catalog.value){scope.begin();data.value=undefined;busy.value=false;error.value='';}},{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin)],()=>{void inspect();},{immediate:true,flush:'sync'});
</script>
<template><section class="spatial-preview">
 <p role="note">AnnData 空间窗口试点：只显示 X 存储原值和 obsm/spatial 的同序号坐标。上游是否归一化未知；不归一化、不配准、不推断物理单位、不反转 Y、不叠加影像或地理底图，不代表完整 SpatialData。</p>
 <button :disabled="busy||!plugin.enabled" @click="inspect">重新读取空间结构</button>
 <form v-if="catalog" @submit.prevent="loadWindow">
  <p>{{catalog.observations.toLocaleString()}} 个观测 × {{catalog.features.toLocaleString()}} 个特征 · {{catalog.storage.toUpperCase()}}</p>
  <p class="notice">不读取 obs/var 名称或身份。请按数据说明使用从 0 开始的特征序号；序号不是基因名。</p>
  <div class="fields"><label>特征序号<input v-model.number="feature" aria-label="空间特征序号" type="number" min="0" :max="catalog.features-1"/></label>
   <label>起始观测<input v-model.number="observationStart" aria-label="空间起始观测" type="number" min="0" :max="catalog.observations-1"/></label>
   <label>观测数<input v-model.number="observationCount" aria-label="空间观测数" type="number" min="1" max="8192"/></label>
   <button type="submit" :disabled="busy||!plugin.enabled">读取空间窗口</button></div>
  <p class="notice" data-testid="spatial-strategy">{{catalog.storage==='dense'?'Dense：选择观测行和特征列；HDF5 块可能包含其他值。':catalog.storage==='csr'?'CSR：读取所选观测行的有界完整稀疏段，再选择特征。':'CSC：读取所选特征列的有界完整稀疏段，再选择观测。'}} 稀疏缺失项按格式定义为 0；重复或未排序索引拒绝，不自动求和。</p>
 </form>
 <p class="notice">首目录不解码表达值或坐标（HDF5 元信息页可能预读邻近字节）。每窗 ≤8192 观测，累计取回 ≤8 MiB；单块解码 ≤4 MiB／合计 ≤16 MiB。仅检查命中窗口，不声明全文件有效。</p>
 <p v-if="busy" role="status">正在读取已授权的空间数据范围…</p><p v-if="error" role="alert">{{error}}</p>
 <p v-if="current" class="notice" data-testid="spatial-stats">读取 {{current.readBytes.toLocaleString()}} / {{current.sourceBytes.toLocaleString()}} 字节，{{current.reads}} 次；数值解码 {{current.numericBytes.toLocaleString()}} 字节，块解码 {{current.decodedChunkBytes.toLocaleString()}} 字节。{{data?`完整可绘制 ${current.plottable} 点，空坐标 ${current.nullCoordinates} 项，空表达值 ${current.nullExpressions} 项。`:'仅元信息，尚未读取数值窗口。'}}</p>
 <div v-if="data" ref="target" class="plot" aria-label="空间表达原值散点图"/>
</section></template>
<style scoped>
.spatial-preview{display:flex;flex-direction:column;gap:12px;overflow:auto;min-height:0;flex:1;padding:16px;font-size:13px}p{margin:0;line-height:1.65}[role=note]{background:#fffbeb;border:1px solid #fcd34d;border-radius:5px;padding:12px;color:#92400e}.notice{font-size:12px;color:#667085}form{border:1px solid #d0d5dd;border-radius:5px;padding:12px;display:flex;flex-direction:column;gap:12px}.fields{display:flex;flex-wrap:wrap;gap:12px}label{display:flex;align-items:center;gap:6px}input{width:90px}button,input{border:1px solid #ccd3db;border-radius:4px;padding:6px}button{align-self:flex-start}button:disabled{opacity:.5}[role=alert]{color:#b45309}.plot{width:100%;min-height:440px;flex-shrink:0}
</style>
