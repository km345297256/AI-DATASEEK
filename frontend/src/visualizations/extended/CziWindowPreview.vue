<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import type { CziData } from './cziData';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestPreview } from './runtime';
import { parseCziWindowData, validateCziSelection } from './cziWindowData';
import { displayError } from './domains/lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), busy = ref(false), error = ref(''), details = ref<CziData>(), version = ref<string>();
const indices = ref([0, 0, 0]), roi = ref([0, 0, 1, 1]), imageUrl = ref(''), imageElement = ref<HTMLImageElement>();
const warnings = ref<string[]>([]), displayed = ref<CziData>();
async function load(kind: 'tree' | 'image') {
  if (kind === 'image') {
    if (busy.value || !details.value || !version.value) return;
    try { validateCziSelection(indices.value, roi.value, details.value.dimensions, details.value.shape); }
    catch (reason) { error.value = displayError(reason); return; }
  }
  const pinned = version.value, requestedIndices = [...indices.value], requestedRoi = [...roi.value];
  const fileIdentity = filePreviewIdentity(props.file), pluginIdentity = pluginPreviewIdentity(props.plugin);
  const task = loads.begin(); busy.value = true; error.value = ''; imageUrl.value = ''; displayed.value = undefined;
  const ownImage = imageElement.value; task.onDispose(() => ownImage?.removeAttribute('src'));
  try {
    if (!props.plugin.enabled) return;
    const response = await requestPreview(props.file, props.plugin, kind === 'tree' ? { kind: 'tree' } : { kind: 'image', version: pinned, indices: requestedIndices, roi: requestedRoi }, task.signal);
    task.assertCurrent();
    if (filePreviewIdentity(props.file) !== fileIdentity || pluginPreviewIdentity(props.plugin) !== pluginIdentity) throw new Error('CZI 文件或插件配置已变化，请重新打开区域预览。');
    const data = parseCziWindowData(response);
    if (typeof response.version !== 'string' || !/^[a-f0-9]{64}$/.test(response.version) || kind === 'image' && response.version !== pinned) throw new Error('CZI 文件版本变化，请重新打开区域预览。');
    if (data.metadata.source_bytes !== props.file.size) throw new Error('CZI 响应源大小与当前文件不一致。');
    if ((kind === 'image') !== !!data.png || kind === 'image' && (JSON.stringify(data.indices) !== JSON.stringify(requestedIndices) || JSON.stringify(data.roi) !== JSON.stringify(requestedRoi))) throw new Error('CZI 区域响应与所选通道、Z/T 或 ROI 不一致。');
    details.value = data; version.value = response.version;
    indices.value = [...data.indices]; roi.value = [...data.roi]; warnings.value = response.warnings as string[];
    if (data.png) {
      const url = URL.createObjectURL(new Blob([data.png], { type: 'image/png' })); task.onDispose(() => URL.revokeObjectURL(url)); imageUrl.value = url;
      displayed.value = data;
    }
  } catch (reason) { if (task.isCurrent()) error.value = displayError(reason); }
  finally { if (task.isCurrent()) busy.value = false; }
}
function imageError() {
  if (!imageUrl.value) return;
  loads.begin(); imageUrl.value = ''; displayed.value = undefined;
  error.value = 'CZI 区域 PNG 无法解码，请重新选择区域。';
}
function imageLoaded() {
  if (!displayed.value || !imageElement.value) return;
  if (imageElement.value.naturalWidth !== displayed.value.roi[2] || imageElement.value.naturalHeight !== displayed.value.roi[3]) imageError();
}
function reset() { details.value = undefined; version.value = undefined; warnings.value = []; void load('tree'); }
function cancel() { loads.begin(); busy.value = false; imageUrl.value = ''; displayed.value = undefined; error.value = '读取已取消。'; }
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], reset, { flush: 'sync' });
onMounted(() => { void load('tree'); });
</script>
<template><section class="czi-window-preview">
  <p class="notice">未压缩 CZI 大文件区域 · 独立范围读取试点，非官方 Python 流接口。源文件最多 8 GiB，每次预览累计读取不超过 32 MiB。</p>
  <p v-if="busy" role="status">正在隔离环境中读取有界范围… <button type="button" @click="cancel">取消读取</button></p><p v-if="error" role="alert">{{ error }}</p>
  <button v-if="!busy && !details && plugin.enabled" type="button" @click="reset">重新检查目录</button>
  <form v-if="details" class="controls" @submit.prevent="load('image')">
    <label>通道 C<select v-model.number="indices[0]" :disabled="busy"><option v-for="c in details.channels" :key="c" :value="c">{{ c }} · {{ (details.metadata.pixel_types as string[])[c] }}</option></select></label>
    <label>Z 层<input v-model.number="indices[1]" type="number" min="0" :max="details.dimensions.Z - 1" step="1" :disabled="busy" required/></label>
    <label>时间 T<input v-model.number="indices[2]" type="number" min="0" :max="details.dimensions.T - 1" step="1" :disabled="busy" required/></label>
    <label v-for="(label, i) in ['ROI X', 'ROI Y', '宽度', '高度']" :key="label">{{ label }}<input v-model.number="roi[i]" type="number" :min="i < 2 ? 0 : 1" :max="i < 2 ? details.shape[1 - i]! - 1 : 1024" step="1" :disabled="busy" required/></label>
    <button :disabled="busy">读取所选区域</button>
  </form>
  <p v-if="details" class="notice">场景 0：{{ details.shape[1] }} × {{ details.shape[0] }} 像素；C={{ details.dimensions.C }}，Z={{ details.dimensions.Z }}，T={{ details.dimensions.T }}。ROI 原点为场景左上角，向右 X、向下 Y；不推测物理坐标。</p>
  <p v-if="details" class="notice" data-testid="czi-window-stats">最近完成请求读取 {{ details.metadata.read_bytes }} / 源文件 {{ details.metadata.source_bytes }} 字节，{{ details.metadata.read_requests }} 次范围请求。{{ displayed ? '所选 ROI 已验证完整且不重叠。' : '目录已读取，尚未显示像素。' }}</p>
  <img ref="imageElement" v-show="imageUrl" :src="imageUrl || undefined" @load="imageLoaded" @error="imageError" alt="所选 CZI 未压缩范围的只读图像" class="image"/>
  <p v-if="displayed" class="notice">当前图像 C/Z/T={{ displayed.indices }}，ROI={{ displayed.roi }}；显示范围={{ displayed.metadata.display_range }}。PNG 适配容器显示，原始 ROI 不变。{{ displayed.metadata.normalization }}</p>
  <p v-for="warning in warnings" :key="warning" class="notice">{{ warning }}</p>
  <p class="notice">小于 64 MiB 的压缩 CZI 可改选既有“CZI 单场景”官方解码插件；本试点不自动回退为整文件下载。</p>
</section></template>
<style scoped>.czi-window-preview{display:flex;flex-direction:column;gap:8px;overflow:auto;height:100%;padding:12px}.controls{display:flex;flex-wrap:wrap;align-items:end;gap:12px;font-size:12px}.controls label{display:flex;flex-direction:column;gap:4px}input,select,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:80px}button:disabled{opacity:.5}.notice{font-size:12px;color:#667085;line-height:1.7}.image{object-fit:contain;align-self:flex-start;flex-shrink:0;width:100%;max-width:720px;max-height:70vh;background:#101828;image-rendering:pixelated}[role=alert]{color:#b42318}</style>
