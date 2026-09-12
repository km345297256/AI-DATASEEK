<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestPreview } from './runtime';
import { assertInstrumentImageBinding, parseInstrumentImageData, validateInstrumentSelection, type InstrumentImageData } from './instrumentImageData';
import { displayError } from './domains/lifecycle';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), busy = ref(false), error = ref(''), details = ref<InstrumentImageData>();
const frame = ref(0), roi = ref([0, 0, 1, 1]), imageUrl = ref(''), imageElement = ref<HTMLImageElement>();
const displayed = ref<InstrumentImageData>();
async function load(kind: 'tree' | 'image') {
  const previous = details.value, chosenFrame = frame.value, chosenRoi = [...roi.value];
  if (kind === 'image') {
    if (busy.value || !previous) return;
    try { validateInstrumentSelection(chosenFrame, chosenRoi, previous.frames, previous.shape); }
    catch (reason) { error.value = displayError(reason); return; }
  }
  const fileIdentity = filePreviewIdentity(props.file), pluginIdentity = pluginPreviewIdentity(props.plugin);
  const task = loads.begin(); busy.value = true; error.value = ''; imageUrl.value = ''; displayed.value = undefined;
  const element = imageElement.value; task.onDispose(() => element?.removeAttribute('src'));
  try {
    if (!props.plugin.enabled) return;
    const response = await requestPreview(props.file, props.plugin, kind === 'tree' ? { kind } : { kind, version: previous!.version, frame: chosenFrame, roi: chosenRoi }, task.signal);
    task.assertCurrent();
    if (filePreviewIdentity(props.file) !== fileIdentity || pluginPreviewIdentity(props.plugin) !== pluginIdentity) throw new Error('文件或插件配置已变化，请重新打开预览。');
    const data = parseInstrumentImageData(response);
    const expectedFormat = props.file.filename?.toLowerCase().endsWith('.edf') ? 'esrf-edf' : props.file.filename?.toLowerCase().endsWith('.spe') ? 'princeton-spe' : null;
    if (data.kind !== kind || data.format !== expectedFormat) throw new Error('仪器图像响应与文件格式或请求视图不一致。');
    if (kind === 'image') assertInstrumentImageBinding(data, previous!, chosenFrame, chosenRoi);
    details.value = data; frame.value = data.frame; roi.value = [...data.roi];
    if (data.png) {
      const url = URL.createObjectURL(new Blob([data.png.slice().buffer as ArrayBuffer], { type: 'image/png' }));
      task.onDispose(() => URL.revokeObjectURL(url)); task.assertCurrent(); imageUrl.value = url; displayed.value = data;
    }
  } catch (reason) { if (task.isCurrent()) error.value = displayError(reason); }
  finally { if (task.isCurrent()) busy.value = false; }
}
function cancel() { loads.begin(); busy.value = false; imageUrl.value = ''; displayed.value = undefined; error.value = '读取已取消。'; }
function reset() { details.value = undefined; frame.value = 0; roi.value = [0, 0, 1, 1]; void load('tree'); }
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], reset, { flush: 'sync' });
onMounted(() => { void load('tree'); });
</script>
<template><section class="instrument-image-preview">
  <p class="notice">ESRF EDF 衍射图像 / Princeton SPE 2.x · 先检查结构，再读取显式选择的帧和像素窗口。生理 EDF 请使用“EDF/BDF 信号”插件。</p>
  <p v-if="busy" role="status">正在隔离环境中读取… <button type="button" @click="cancel">取消读取</button></p>
  <p v-if="error" role="alert">{{ error }}</p>
  <button v-if="!details && !busy && plugin.enabled" type="button" class="retry" @click="reset">重新检查结构</button>
  <form v-if="details" class="controls" @submit.prevent="load('image')">
    <label>帧索引<input v-model.number="frame" type="number" min="0" :max="details.frames - 1" step="1" :disabled="busy || !plugin.enabled" required/></label>
    <label v-for="(label, index) in ['ROI X', 'ROI Y', '宽度', '高度']" :key="label">{{ label }}<input v-model.number="roi[index]" type="number" :min="index < 2 ? 0 : 1" :max="index < 2 ? details.shape[1 - index]! - 1 : 1024" step="1" :disabled="busy || !plugin.enabled" required/></label>
    <button :disabled="busy || !plugin.enabled">读取所选 ROI</button>
    <button type="button" :disabled="busy || !plugin.enabled" @click="reset">重新检查结构</button>
  </form>
  <p v-if="details" class="notice">{{ details.format }} · {{ details.frames }} 帧 · 每帧 {{ details.shape[1] }} × {{ details.shape[0] }} · {{ details.dtype }} / {{ details.byteOrder }} endian。源大小 {{ details.sourceBytes.toLocaleString() }} 字节，本次只读 {{ details.readBytes.toLocaleString() }} 字节（{{ details.reads }} 个范围）。</p>
  <p v-if="details && !imageUrl && !busy" class="notice">尚未显示像素；帧从 0 开始，ROI 原点在帧左上角，向右 X、向下 Y。单次 ROI 宽高各不超过 1024，读取仍受总量、次数与时限约束。</p>
  <img ref="imageElement" v-show="imageUrl" :src="imageUrl || undefined" alt="所选仪器数据帧和 ROI 的只读灰度显示" class="image"/>
  <p v-if="displayed" class="notice">当前图像：帧 {{ displayed.frame }}，ROI {{ displayed.roi }}。灰度范围 {{ displayed.range }}（原始探测器值，未校准），非有限像素 {{ displayed.invalid }} 个以黑色显示。仅作显示映射，不拟合、不重采样、不修改源数据。</p>
</section></template>
<style scoped>.instrument-image-preview{display:flex;flex-direction:column;gap:8px;height:100%;overflow:auto;padding:12px}.notice{font-size:12px;line-height:1.7;color:#667085}.controls{display:flex;flex-wrap:wrap;align-items:end;gap:12px;font-size:12px}.controls label{display:flex;flex-direction:column;gap:4px}input,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:90px}button:disabled{opacity:.5}.image{object-fit:contain;align-self:flex-start;flex-shrink:0;width:100%;max-width:800px;max-height:70vh;background:#101828;image-rendering:pixelated}[role=alert]{color:#b42318}</style>
