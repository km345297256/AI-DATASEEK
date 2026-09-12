<template>
  <section class="min-h-0 flex-1 overflow-auto p-4">
    <p role="note" class="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{{ mode === 'gro-trajectory' ? GRO_WARNING : MESH_WARNING }} 本插件有界读取整个文件，不是大文件范围读取。</p>
    <form v-if="catalog" class="rounded border p-3 text-sm" @submit.prevent="loadGeometry">
      <div v-if="mode === 'gro-trajectory'" class="flex flex-wrap items-center gap-3"><label>轨迹帧 <select v-model.number="frameId" aria-label="轨迹帧" :disabled="busy" class="rounded border p-1"><option v-for="f in catalog.frames" :key="f.id" :value="f.id">{{ f.id + 1 }} · {{ f.atoms }} 原子 · {{ f.time_ps === null ? '时间未声明' : `${f.time_ps} ps` }}</option></select></label></div>
      <div v-else class="flex flex-wrap items-center gap-3"><p>{{ catalog.metadata.point_count }} 节点 · {{ catalog.metadata.cell_count }} 单元</p>
        <label>场 <select v-model="fieldId" aria-label="网格场" :disabled="busy" class="rounded border p-1"><option :value="null">仅网格</option><option v-for="f in catalog.fields" :key="f.id" :value="f.id">{{ f.name }} · {{ f.association === 'point' ? '节点' : '单元' }} · {{ f.components }} 分量</option></select></label>
        <label v-if="selectedField">分量 <select v-model.number="componentId" aria-label="场分量" :disabled="busy" class="rounded border p-1"><option v-for="n in selectedField.components" :key="n" :value="n - 1">{{ n - 1 }}</option></select></label>
      </div>
      <button type="submit" :disabled="busy" class="mt-3 rounded border px-3 py-1">{{ mode === 'gro-trajectory' ? '显示所选帧' : '绘制所选网格' }}</button>
      <p class="mt-2 text-xs text-gray-600">{{ mode === 'gro-trajectory' ? '保持原子记录顺序；不推断化学键、元素或周期解包。' : selectedField?.association === 'cell' ? '单元值只在各单元顶点算术均值位置显示；不是体积质心，不插值到节点。' : '按原始节点坐标显示；不把向量自动换算成模长。' }}</p>
    </form>
    <p v-if="busy" role="status" class="py-3">正在检查授权文件与几何数据…</p><p v-if="error" role="alert" class="py-3 text-amber-700">{{ error }}</p>
    <template v-if="displayed && scene"><GeometryScene :scene="scene" />
      <p v-if="displayed.trajectory" class="mt-3 text-sm" data-testid="gro-box">盒矢量（GRO 原始 9 分量顺序，nm）：{{ displayed.frames[frameId]?.box.join(', ') }}。全零表示未声明周期盒。</p>
      <div class="mt-3 overflow-x-auto"><p class="text-xs text-gray-600">前 {{ Math.min(scene.markers.length,20) }} 条原始值；表格截取不影响上方完整所选帧／网格。</p>
        <table class="w-full text-xs" data-testid="geometry-values"><thead><tr><th>记录序号（0 起）</th><th>X</th><th>Y</th><th>Z</th><th>{{ displayed.trajectory ? '原子标签（非元素）' : '所选分量' }}</th><th v-if="displayed.trajectory?.velocities">速度 (nm/ps)</th></tr></thead><tbody><tr v-for="(p,i) in scene.markers.slice(0,20)" :key="i"><td>{{ i }}</td><td v-for="(v,d) in p" :key="d">{{ v }}</td><td>{{ displayed.trajectory ? displayed.trajectory.atoms[i]?.atom_name : scene.values?.[i] ?? '—' }}</td><td v-if="displayed.trajectory?.velocities">{{ displayed.trajectory.velocities[i]?.join(', ') }}</td></tr></tbody></table>
      </div>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { displayError } from './domains/lifecycle';
import GeometryScene from './GeometryScene.vue';
import { GRO_WARNING,MESH_WARNING,geometrySelection,geometryCatalogIdentity,parseGeometryData,sceneGeometry,type GeometryData,type GeometryMode } from './geometryData';
const props = defineProps<{file:FileInfo;plugin:VisualizationPlugin;mode:GeometryMode}>();
const scope=usePreviewLoad(),catalog=shallowRef<GeometryData>(),displayed=shallowRef<GeometryData>(),busy=ref(false),error=ref(''),frameId=ref(0),fieldId=ref<string|null>(null),componentId=ref(0);
const selectedField=computed(()=>catalog.value?.fields.find(f=>f.id===fieldId.value)),scene=computed(()=>displayed.value?sceneGeometry(displayed.value):undefined);let version:string|undefined;
async function inspect() {
  const load=scope.begin();catalog.value=undefined;displayed.value=undefined;version=undefined;error.value='';busy.value=false;frameId.value=0;fieldId.value=null;componentId.value=0;
  if(!props.plugin.enabled)return;busy.value=true;
  try {const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},load.signal);load.assertCurrent();catalog.value=parseGeometryData(result,props.mode,'tree',{},props.file.size,props.file.filename);version=result.version;}
  catch(reason){if(load.isCurrent())error.value=displayError(reason);}finally{if(load.isCurrent())busy.value=false;}
}
async function loadGeometry() {
  if(busy.value)return;const load=scope.begin();displayed.value=undefined;error.value='';busy.value=true;
  try {
    if(!props.plugin.enabled||!catalog.value||!version)throw new Error('请先读取有效结构目录。');
    const expected=catalog.value,pinned=version,options=geometrySelection(props.mode,expected,frameId.value,fieldId.value,componentId.value);
    const result=await requestVisualization(props.file,props.plugin,'preview',{kind:'geometry',version:pinned,...options},load.signal);load.assertCurrent();
    if(result.version!==pinned)throw new Error('文件版本已变化，请重新打开预览。');
    const parsed=parseGeometryData(result,props.mode,'geometry',options,props.file.size,props.file.filename);
    if(geometryCatalogIdentity(parsed)!==geometryCatalogIdentity(expected))throw new Error('几何数据与已检查的目录不一致。');displayed.value=parsed;
  }catch(reason){if(load.isCurrent())error.value=displayError(reason);}finally{if(load.isCurrent())busy.value=false;}
}
watch(fieldId,()=>{componentId.value=0;},{flush:'sync'});
watch([frameId,fieldId,componentId],()=>{if(catalog.value){scope.begin();displayed.value=undefined;error.value='';busy.value=false;}},{flush:'sync'});
watch([()=>filePreviewIdentity(props.file),()=>pluginPreviewIdentity(props.plugin),()=>props.mode],()=>void inspect(),{immediate:true,flush:'sync'});
onScopeDispose(()=>{catalog.value=undefined;displayed.value=undefined;version=undefined;});
</script>
