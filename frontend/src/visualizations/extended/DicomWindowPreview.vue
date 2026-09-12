<script setup lang="ts">
import { onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { displayError } from './domains/lifecycle';
import { DICOM_WARNING, defaultDicomWindow, dicomRgba, parseDicomWindow, validateDicomOptions, type DicomData, type DicomOptions } from './dicomWindowData';
const props = defineProps<{file:FileInfo;plugin:VisualizationPlugin}>();
const loads = usePreviewLoad(), plots = usePreviewLoad(), catalog = shallowRef<DicomData>(), displayed = shallowRef<DicomData>(), target = ref<HTMLDivElement>();
const busy = ref(false), error = ref(''), frame = ref(0), x = ref(0), y = ref(0), width = ref(1), height = ref(1), confirmed = ref(false), center = ref(0), windowWidth = ref(1), windowSource = ref('');
let version: string | undefined;
function clearWindow() { loads.begin(); plots.begin(); displayed.value = undefined; busy.value = false; error.value = ''; windowSource.value = ''; }
function draw() {
  const data = displayed.value, task = plots.begin(); if(!data || !target.value) return;
  try {
    const rgba = dicomRgba(data,center.value,windowWidth.value), element = document.createElement('canvas');
    element.dataset.testid = 'dicom-canvas'; element.width = data.shape![1]!; element.height = data.shape![0]!;
    element.style.cssText = 'max-width:100%;width:560px;max-height:420px;object-fit:contain;image-rendering:pixelated;';
    task.onDispose(() => { element.width = element.height = 0; element.remove(); });
    const context = element.getContext('2d'); if(!context) throw new Error('浏览器无法创建灰度画布。');
    context.putImageData(new ImageData(rgba,element.width,element.height),0,0); task.assertCurrent(); target.value.replaceChildren(element); error.value = '';
  } catch(reason) { if(task.isCurrent()) { plots.begin(); error.value = displayError(reason); } }
}
async function inspect() {
  const task = loads.begin(); plots.begin(); catalog.value = displayed.value = undefined; version = undefined; busy.value = false; error.value = ''; confirmed.value = false; windowSource.value = '';
  if(!props.plugin.enabled) return; busy.value = true;
  try {
    const response = await requestVisualization(props.file,props.plugin,'preview',{kind:'tree'},task.signal); task.assertCurrent();
    if(response.kind !== 'tree' || !/^[0-9a-f]{64}$/.test(response.version)) throw new Error('DICOM 结构版本无效。');
    const data = parseDicomWindow('tree',response.payload,response.metadata,{});
    if(data.sourceBytes !== props.file.size) throw new Error('DICOM 文件大小发生变化。');
    frame.value = x.value = y.value = 0; width.value = Math.min(128,data.image.columns); height.value = Math.min(128,data.image.rows);
    version = response.version; catalog.value = data;
  } catch(reason) { if(task.isCurrent()) error.value = displayError(reason); }
  finally { if(task.isCurrent()) busy.value = false; }
}
async function loadWindow() {
  if(busy.value) return;
  const task = loads.begin(); plots.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    const initial = catalog.value, pinned = version;
    if(!props.plugin.enabled || !initial || !pinned) throw new Error('请先检查 DICOM 结构。');
    const options = validateDicomOptions('image',{frame:frame.value,roi:[x.value,y.value,width.value,height.value],confirm_deidentified:confirmed.value},initial.image) as DicomOptions;
    const response = await requestVisualization(props.file,props.plugin,'preview',{kind:'image',version:pinned,...options},task.signal); task.assertCurrent();
    if(response.kind !== 'array' || response.version !== pinned) throw new Error('DICOM 文件版本变化，请重新检查结构。');
    const data = parseDicomWindow('image',response.payload,response.metadata,options);
    if(data.sourceBytes !== initial.sourceBytes || data.headerBytes !== initial.headerBytes || JSON.stringify(data.image) !== JSON.stringify(initial.image)) throw new Error('DICOM 图像与初始结构不一致。');
    const settings = defaultDicomWindow(data); center.value = settings.center; windowWidth.value = settings.width; windowSource.value = settings.source;
    displayed.value = data; draw();
  } catch(reason) { if(task.isCurrent()) error.value = displayError(reason); }
  finally { if(task.isCurrent()) busy.value = false; }
}
function resetWindow() { if(!displayed.value) return; const settings = defaultDicomWindow(displayed.value); center.value = settings.center; windowWidth.value = settings.width; windowSource.value = settings.source; draw(); }
function cancel() { clearWindow(); error.value = '读取已取消。'; }
watch([frame,x,y,width,height,confirmed],() => { if(catalog.value) clearWindow(); },{flush:'sync'});
watch([center,windowWidth],() => { if(displayed.value) { windowSource.value = '本地显示调整'; draw(); } },{flush:'sync'});
watch([() => filePreviewIdentity(props.file),() => pluginPreviewIdentity(props.plugin)],() => { void inspect(); },{immediate:true,flush:'sync'});
onScopeDispose(() => { catalog.value = displayed.value = undefined; version = undefined; });
</script>
<template><section class="dicom-preview">
  <p role="note" class="warning">{{ DICOM_WARNING }}</p>
  <p class="notice">仅 Part 10、未压缩小端 MONOCHROME1/2 灰度；不支持压缩、彩色、增强多帧、LUT、完整 series 或临床阅片。坐标为源图像行列，不推断患者方向与空间间距。</p>
  <button :disabled="busy || !plugin.enabled" @click="inspect">重新检查 DICOM 结构</button>
  <form v-if="catalog" @submit.prevent="loadWindow">
    <p>{{ catalog.image.columns }} 列 × {{ catalog.image.rows }} 行 · {{ catalog.image.frames }} 帧 · {{ catalog.image.bits_stored }}/{{ catalog.image.bits_allocated }} 位 · {{ catalog.image.photometric }}</p>
    <label class="confirm"><input v-model="confirmed" type="checkbox" aria-label="确认 DICOM 已脱敏" />我已核实该科学图像及像素内容已脱敏，理解此工具不能验证脱敏</label>
    <div class="fields"><label>帧<input v-model.number="frame" aria-label="DICOM 帧" type="number" min="0" :max="catalog.image.frames-1" /></label><label>列<input v-model.number="x" aria-label="DICOM 列" type="number" min="0" /></label><label>行<input v-model.number="y" aria-label="DICOM 行" type="number" min="0" /></label><label>宽<input v-model.number="width" aria-label="DICOM 宽" type="number" min="1" max="128" /></label><label>高<input v-model.number="height" aria-label="DICOM 高" type="number" min="1" max="128" /></label></div>
    <button :disabled="busy || !confirmed || !plugin.enabled" type="submit">读取 DICOM 区域</button>
  </form>
  <p v-if="busy" role="status">正在读取受控范围… <button @click="cancel">取消读取</button></p><p v-if="error" role="alert">{{ error }}</p>
  <div v-if="displayed" class="contrast"><label>窗位<input v-model.number="center" aria-label="DICOM 窗位" type="number" step="any" /></label><label>窗宽<input v-model.number="windowWidth" aria-label="DICOM 窗宽" type="number" min="1" step="any" /></label><button @click="resetWindow">恢复初始窗设置</button>
    <p class="notice">{{ windowSource }}；LINEAR 显示，复用当前 ROI 缓存，不重新读取文件。源整数不修改。{{ displayed.paddingPixels }} 个 padding 像素透明。</p>
    <p class="notice">{{ displayed.image.rescale.declared ? `显示使用声明变换：存储值 × ${displayed.image.rescale.slope} + ${displayed.image.rescale.intercept}，单位 ${displayed.image.rescale.unit}` : '未声明 rescale：按存储值显示，单位不推断' }}；{{ displayed.image.photometric === 'MONOCHROME1' ? 'MONOCHROME1 显示反转' : 'MONOCHROME2' }}。</p>
  </div>
  <p v-if="displayed || catalog" class="notice" data-testid="dicom-stats">本次读取 {{ (displayed || catalog)!.readBytes }} / {{ (displayed || catalog)!.sourceBytes }} 字节，{{ (displayed || catalog)!.reads }} 次范围请求。首次结构检查不读取像素；单区域最多 128 × 128。</p>
  <div ref="target" class="canvas" aria-label="DICOM 灰度区域" />
</section></template>
<style scoped>
.dicom-preview{padding:16px;display:flex;flex:1;min-height:0;overflow:auto;flex-direction:column;gap:12px;font-size:13px}p{margin:0;line-height:1.7}.warning{padding:10px;background:#fffaeb;border:1px solid #fedf89;border-radius:5px;color:#92400e}.notice{color:#667085;font-size:12px}form,.contrast{padding:12px;border:1px solid #d0d5dd;border-radius:6px;display:flex;flex-direction:column;gap:10px}.fields{display:flex;flex-wrap:wrap;gap:12px}label{display:flex;align-items:center;gap:6px}input[type=number]{width:90px}.confirm{line-height:1.6}input,button{padding:5px 7px;border:1px solid #ccd3db;border-radius:4px}button{align-self:flex-start}button:disabled{opacity:.5}[role=alert]{color:#b42318}.canvas{display:flex;justify-content:center;flex-shrink:0;background:repeating-conic-gradient(#fafafa 0% 25%,#ededed 0% 50%) 50%/16px 16px;overflow:hidden}.canvas:empty{display:none}
</style>
