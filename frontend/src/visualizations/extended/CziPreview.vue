<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestPreview } from './runtime';
import { parseCziData, validateCziSelection, type CziData } from './cziData';
import { displayError } from './domains/lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), busy = ref(false), error = ref(''), details = ref<CziData>(), version = ref<string>();
const indices = ref([0, 0, 0]), roi = ref([0, 0, 1, 1]), imageUrl = ref(''), imageElement = ref<HTMLImageElement>();
const warnings = ref<string[]>([]), displayed = ref<{ indices: number[]; roi: number[]; range: unknown; normalization: unknown }>();
async function load(kind: 'tree' | 'image') {
  if (kind === 'image') {
    if (busy.value || !details.value || !version.value) return;
    try { validateCziSelection(indices.value, roi.value, details.value.dimensions, details.value.shape); }
    catch (reason) { error.value = displayError(reason); return; }
  }
  const pinned = version.value, requestedIndices = [...indices.value], requestedRoi = [...roi.value];
  const task = loads.begin(); busy.value = true; error.value = ''; imageUrl.value = ''; displayed.value = undefined;
  const ownImage = imageElement.value; task.onDispose(() => ownImage?.removeAttribute('src'));
  try {
    if (!props.plugin.enabled) return;
    const response = await requestPreview(props.file, props.plugin, kind === 'tree' ? { kind: 'tree' } : { kind: 'image', version: pinned, indices: requestedIndices, roi: requestedRoi }, task.signal);
    task.assertCurrent(); const data = parseCziData(response);
    if ((kind === 'image') !== !!data.png) throw new Error('CZI 响应与请求的视图类型不一致。');
    if (kind === 'image' && response.version !== pinned) throw new Error('CZI 文件版本变化，请重新打开预览。');
    if (kind === 'image' && (JSON.stringify(data.indices) !== JSON.stringify(requestedIndices) || JSON.stringify(data.roi) !== JSON.stringify(requestedRoi))) throw new Error('CZI 响应选择与请求的通道、层面、时间或 ROI 不一致。');
    details.value = data; version.value = response.version as string;
    indices.value = [...data.indices]; roi.value = [...data.roi]; warnings.value = response.warnings as string[];
    if (data.png) {
      const url = URL.createObjectURL(new Blob([data.png], { type: 'image/png' })); task.onDispose(() => URL.revokeObjectURL(url)); imageUrl.value = url;
      displayed.value = { indices: [...data.indices], roi: [...data.roi], range: data.metadata.display_range, normalization: data.metadata.normalization };
    }
  } catch (reason) { if (task.isCurrent()) error.value = displayError(reason); }
  finally { if (task.isCurrent()) busy.value = false; }
}
function reset() { details.value = undefined; version.value = undefined; warnings.value = []; void load('tree'); }
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version, () => props.plugin.enabled], reset);
onMounted(() => { void load('tree'); });
</script>
<template><section class="czi-preview">
  <p class="notice">Zeiss CZI 单场景试点 · 先检查结构，再显式读取通道 / 层面 / 时间点。整文件上限 64 MiB，不是 range 传输。</p>
  <p v-if="busy" role="status">正在隔离环境中读取…</p><p v-if="error" role="alert">{{ error }}</p>
  <form v-if="details" class="controls" @submit.prevent="load('image')">
    <label>通道 C<select v-model.number="indices[0]" :disabled="busy"><option v-for="c in details.channels" :key="c" :value="c">{{ c }} · {{ (details.metadata.pixel_types as string[])[c] }}</option></select></label>
    <label>Z 层<input v-model.number="indices[1]" type="number" min="0" :max="details.dimensions.Z - 1" step="1" :disabled="busy" required/></label>
    <label>时间 T<input v-model.number="indices[2]" type="number" min="0" :max="details.dimensions.T - 1" step="1" :disabled="busy" required/></label>
    <label v-for="(label, i) in ['ROI X', 'ROI Y', '宽度', '高度']" :key="label">{{ label }}<input v-model.number="roi[i]" type="number" :min="i < 2 ? 0 : 1" :max="i < 2 ? details.shape[1 - i]! - 1 : 1024" step="1" :disabled="busy" required/></label>
    <button :disabled="busy">读取所选平面</button>
  </form>
  <p v-if="details" class="notice">场景 0：{{ details.shape[1] }} × {{ details.shape[0] }} 像素；C={{ details.dimensions.C }}，Z={{ details.dimensions.Z }}，T={{ details.dimensions.T }}。索引从 0 开始；ROI 原点在场景左上角，向右 X、向下 Y。</p>
  <p v-if="details && !imageUrl && !busy" class="notice">结构已读取；图像像素尚未读取。确认选择后点击“读取所选平面”。</p>
  <img ref="imageElement" v-show="imageUrl" :src="imageUrl || undefined" alt="所选 CZI 通道、层面、时间和 ROI 的只读图像" class="image"/>
  <p v-if="displayed" class="notice">当前图像 C/Z/T={{ displayed.indices }}，ROI={{ displayed.roi }}，显示范围={{ displayed.range }}。图像适配容器显示，原始 ROI 像素未改变。{{ displayed.normalization }}</p>
  <p v-for="warning in warnings" :key="warning" class="notice">{{ warning }}</p>
</section></template>
<style scoped>.czi-preview{display:flex;flex-direction:column;gap:8px;overflow:auto;height:100%;padding:12px}.controls{display:flex;flex-wrap:wrap;align-items:end;gap:12px;font-size:12px}.controls label{display:flex;flex-direction:column;gap:4px}input,select,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:80px}button:disabled{opacity:.5}.notice{font-size:12px;color:#667085;line-height:1.7}.image{object-fit:contain;align-self:flex-start;flex-shrink:0;width:100%;max-width:720px;max-height:70vh;background:#101828;image-rendering:pixelated}[role=alert]{color:#b42318}</style>
