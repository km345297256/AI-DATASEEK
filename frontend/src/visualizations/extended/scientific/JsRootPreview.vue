<template>
  <PreviewFrame :loading="loading" :error="error" note="JSROOT · 仅隔离读取后的 TH1/TH2/TGraph 数值；不执行文件中的函数、绘制代码或对象脚本。">
    <form class="mb-2 flex gap-2 text-xs" @submit.prevent="loadObject"><label>对象 <select v-model="objectPath"><option v-for="path in objects" :key="path" :value="path">{{ path }}</option></select></label><label>图式 <select v-model="kind"><option value="series">TH1 / TGraph</option><option value="heatmap">TH2</option></select></label><button class="rounded border px-2" :disabled="loading">读取</button></form>
    <div ref="target" class="min-h-[450px] flex-1 bg-white" />
    <p v-if="sampled" class="text-xs text-amber-700">预览可能为有界降采样，不替代完整 ROOT 分析。</p>
    <p v-for="warning in warnings" :key="warning" class="text-xs text-amber-700">{{ warning }}</p>
  </PreviewFrame>
</template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { usePreviewLoad } from '../../../composables/usePreviewLoad';
import { requestPreview } from '../runtime';
import { buildRootNumericObject } from './rootData';
import PreviewFrame from './PreviewFrame.vue';
import { loadBrowserLibrary } from './browserLibraries';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), sampled = ref(false);
const objects = ref<string[]>([]), objectPath = ref(''), kind = ref('series'), warnings = ref<string[]>([]);
async function loadObject() {
  const load = scope.begin(); loading.value = true; error.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, { kind: kind.value, ...(objectPath.value ? { path: objectPath.value } : {}) }, load.signal);
    load.assertCurrent();
    const core = await loadBrowserLibrary('jsroot', load.signal), draw = core;
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    // Rebuild a new object from numeric values. Never spread ROOT/user metadata
    // into it: TFormula, TExec, fFunctions and arbitrary painters cannot enter.
    const { object: histogram, option } = buildRootNumericObject(result, core);
    const settings = { ContextMenu: core.settings.ContextMenu, ToolBar: core.settings.ToolBar, Latex: core.settings.Latex };
    core.settings.ContextMenu = false; core.settings.ToolBar = false; core.settings.Latex = 0;
    const element = target.value!; load.onDispose(() => { draw.cleanup(element); Object.assign(core.settings, settings); });
    await draw.draw(element, histogram, option); load.assertCurrent(); sampled.value = result.sampled === true;
    warnings.value = Array.isArray(result.warnings) ? result.warnings.filter((value): value is string => typeof value === 'string').slice(0, 10) : [];
    if (Array.isArray(result.tree)) objects.value = result.tree.filter((node): node is { path: string } => !!node && typeof node === 'object' && typeof node.path === 'string').map((node) => node.path).slice(0, 128);
    const selected = result.selected as Record<string, unknown> | undefined; if (typeof selected?.path === 'string') objectPath.value = selected.path;
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : 'ROOT 预览失败。'; }
  finally { if (load.isCurrent()) loading.value = false; }
}
watch([() => props.file.file_id, () => props.plugin.id], () => { objects.value = []; objectPath.value = ''; void loadObject(); }, { immediate: true });
</script>
