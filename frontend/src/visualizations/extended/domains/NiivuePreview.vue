<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { validateNiftiOrNrrd } from './guards';
import { displayError, useDomainScope } from './lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const canvas = ref<HTMLCanvasElement>(); const error = ref(''); const ready = ref(false); const mode = ref('multi');
const scope = useDomainScope(); let nv: any;
function changeMode() { if (nv) nv.setSliceType(mode.value === 'render' ? nv.sliceTypeRender : nv.sliceTypeMultiplanar); }
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); validateNiftiOrNrrd(bytes, props.file.filename); scope.check();
    const { Niivue } = await import('@niivue/niivue'); scope.check();
    nv = new Niivue({ dragAndDropEnabled: false, isResizeCanvas: true, show3Dcrosshair: true, backColor: [0.08, 0.1, 0.14, 1] });
    scope.add(() => { nv?.cleanup(); nv = undefined; });
    await nv.attachToCanvas(canvas.value!); scope.check();
    await nv.loadFromArrayBuffer(bytes, props.file.filename); scope.check();
    nv.setSliceType(nv.sliceTypeMultiplanar); ready.value = true;
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template><section class="domain-preview"><header>NiiVue · 科研影像（非临床诊断）<select v-if="ready" v-model="mode" aria-label="影像布局" @change="changeMode"><option value="multi">正交切片</option><option value="render">体渲染</option></select></header><p>单文件 NIfTI-1 / 内嵌 raw NRRD；不执行外部加载、宏或数据修改。</p><p v-if="error" role="alert">{{ error }}</p><canvas v-show="!error" ref="canvas" class="surface"/></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{flex:1;min-height:300px;width:100%;max-height:80vh}header{display:flex;justify-content:space-between;gap:8px;padding:8px}p{font-size:12px;padding:8px;color:#667085}[role=alert]{color:#b42318}select{border:1px solid #ccc;border-radius:4px;padding:2px}</style>
