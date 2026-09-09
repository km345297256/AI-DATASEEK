<template>
  <PreviewFrame :loading="loading" :error="error" note="Mol* · PDB/mmCIF 大分子本地只读视图；没有数据库查询、结构下载或远程状态导入。">
    <div ref="target" class="relative min-h-[480px] flex-1 overflow-hidden bg-white" />
  </PreviewFrame>
</template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { loadPluginBytes } from '../runtime';
import { assertMolecularText, safeText } from './data';
import PreviewFrame from './PreviewFrame.vue';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref('');
watch(() => [props.file.file_id, props.plugin.id], async () => {
  const load = scope.begin(); loading.value = true; error.value = '';
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, load.signal), format = props.file.filename.toLowerCase().endsWith('.pdb') ? 'pdb' : 'mmcif';
    load.assertCurrent();
    const text = safeText(bytes, 8 * 1024 * 1024); assertMolecularText(text, format);
    const [{ PluginContext }] = await Promise.all([import('molstar/lib/mol-plugin/context')]);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    // Deliberately no DefaultPluginSpec: its server/download actions and custom
    // property auto-fetch behaviors are not part of the file:read contract.
    const viewer = new PluginContext({ actions: [], behaviors: [], animations: [] });
    load.onDispose(() => viewer.dispose()); await viewer.init(); load.assertCurrent();
    if (!await viewer.mountAsync(target.value!)) throw new Error('浏览器无法创建 Mol* WebGL 视图。');
    load.assertCurrent();
    const data = await viewer.builders.data.rawData({ data: text, label: 'Local structure' }); load.assertCurrent();
    const trajectory = await viewer.builders.structure.parseTrajectory(data, format); load.assertCurrent();
    // Do not expand file-declared biological assemblies: their operator count
    // can multiply a small source structure beyond the interactive budget.
    await viewer.builders.structure.hierarchy.applyPreset(trajectory, 'default', { structure: { name: 'model', params: {} }, showUnitcell: false }); load.assertCurrent();
    const resize = new ResizeObserver(() => { if (load.isCurrent()) viewer.handleResize(); }); resize.observe(target.value!); load.onDispose(() => resize.disconnect());
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '大分子预览失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}, { immediate: true });
</script>
