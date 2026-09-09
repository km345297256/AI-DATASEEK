<template>
  <PreviewFrame :loading="loading" :error="error" note="vtk.js · 本地小型网格／VTI 轴向切片；XML 首期仅 ASCII，无外部材质、纹理或脚本。">
    <label v-if="maxSlice > 0" class="mb-2 flex gap-2 text-xs">轴向切片 {{ slice }} <input v-model.number="slice" type="range" :min="minSlice" :max="maxSlice" @input="updateSlice" /></label>
    <div ref="target" class="relative flex-none overflow-hidden bg-slate-900" style="height: 480px" />
  </PreviewFrame>
</template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { loadPluginBytes } from '../runtime';
import { assertVtkInput, safeText } from './data';
import PreviewFrame from './PreviewFrame.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref('');
const slice = ref(0), minSlice = ref(0), maxSlice = ref(0);
let updateSlice = () => {};
watch(() => [props.file.file_id, props.plugin.id], async () => {
  const load = scope.begin(); loading.value = true; error.value = ''; maxSlice.value = 0;
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, load.signal); load.assertCurrent();
    const extension = props.file.filename.split('.').pop()?.toLowerCase() ?? '';
    assertVtkInput(bytes, extension);
    const [{ default: Window }] = await Promise.all([import('@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'), import('@kitware/vtk.js/Rendering/Profiles/Geometry'), import('@kitware/vtk.js/Rendering/Profiles/Volume')]);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    const view = Window.newInstance({ background: [0.06, 0.09, 0.13], listenWindowResize: false });
    load.onDispose(() => { updateSlice = () => {}; view.delete(); });
    view.setContainer(target.value!); view.resize();
    if (extension === 'vti') {
      const [{ default: Reader }, { default: Mapper }, { default: Actor }] = await Promise.all([import('@kitware/vtk.js/IO/XML/XMLImageDataReader'), import('@kitware/vtk.js/Rendering/Core/ImageMapper'), import('@kitware/vtk.js/Rendering/Core/ImageSlice')]);
      load.assertCurrent(); const reader = Reader.newInstance(), mapper = Mapper.newInstance(), actor = Actor.newInstance();
      load.onDispose(() => { actor.delete(); mapper.delete(); reader.delete(); });
      reader.parseAsArrayBuffer(bytes); const output = reader.getOutputData();
      if (!output?.getPointData()?.getScalars()) throw new Error('VTI 没有可显示的点标量。');
      mapper.setInputData(output); const extent = output.getExtent(); minSlice.value = extent[4]; maxSlice.value = extent[5]; slice.value = Math.floor((extent[4] + extent[5]) / 2);
      mapper.setSlicingMode(2); mapper.setSlice(slice.value); actor.setMapper(mapper); view.getRenderer().addActor(actor);
      const range = output.getPointData().getScalars().getRange(); actor.getProperty().setColorWindow(range[1] - range[0] || 1); actor.getProperty().setColorLevel((range[0] + range[1]) / 2);
      updateSlice = () => { if (load.isCurrent()) { mapper.setSlice(slice.value); view.getRenderWindow().render(); } };
    } else {
      const [{ default: Mapper }, { default: Actor }] = await Promise.all([import('@kitware/vtk.js/Rendering/Core/Mapper'), import('@kitware/vtk.js/Rendering/Core/Actor')]);
      const module = extension === 'vtp' ? await import('@kitware/vtk.js/IO/XML/XMLPolyDataReader') : extension === 'obj' ? await import('@kitware/vtk.js/IO/Misc/OBJReader') : await import('@kitware/vtk.js/IO/Geometry/STLReader');
      load.assertCurrent(); const reader = module.default.newInstance(), mapper = Mapper.newInstance(), actor = Actor.newInstance();
      load.onDispose(() => { actor.delete(); mapper.delete(); reader.delete(); });
      if (extension === 'obj') (reader as import('@kitware/vtk.js/IO/Misc/OBJReader').vtkOBJReader).parseAsText(safeText(bytes, 8 * 1024 * 1024));
      else (reader as import('@kitware/vtk.js/IO/Geometry/STLReader').vtkSTLReader).parseAsArrayBuffer(bytes);
      const output = reader.getOutputData(); if (!output || output.getNumberOfPoints() > 300000) throw new Error('网格为空或超过点数预算。');
      mapper.setInputData(output); actor.setMapper(mapper); actor.getProperty().setColor(0.35, 0.8, 0.7); view.getRenderer().addActor(actor);
    }
    view.getRenderer().resetCamera(); view.getRenderWindow().render();
    const resize = new ResizeObserver(() => { if (load.isCurrent()) view.resize(); }); resize.observe(target.value!); load.onDispose(() => resize.disconnect());
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '三维预览失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}, { immediate: true });
</script>
