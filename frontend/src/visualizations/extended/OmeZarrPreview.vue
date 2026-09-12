<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestPreview } from './runtime';
import { parseOmeZarr, validateOmeSelection, omePixelGray, type OmeData } from './omeZarrData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const loads = usePreviewLoad(), drawing = usePreviewLoad();
const details = shallowRef<OmeData>(), displayed = shallowRef<OmeData>(), error = ref(''), busy = ref(false);
const level = ref(0), indices = ref<number[]>([]), roi = ref([0, 0, 1, 1]), target = ref<HTMLDivElement>(), hover = ref('');
const selectedLevel = computed(() => details.value?.levels[level.value]);
let version: string | undefined;
function resetSelection() {
  drawing.begin(); displayed.value = undefined; hover.value = '';
  if (!details.value || !selectedLevel.value) return;
  indices.value = Array(details.value.axes.length - 2).fill(0);
  roi.value = [0, 0, Math.min(128, selectedLevel.value.shape.slice(-1)[0]!), Math.min(128, selectedLevel.value.shape.slice(-2)[0]!)];
}
async function draw(data: OmeData) {
  const task = drawing.begin();
  await nextTick(); task.assertCurrent();
  if (!target.value || !data.values) return;
  const [x, y, width, height] = data.selected.roi, item = data.levels[data.selected.level]!;
  const canvas = document.createElement('canvas'); canvas.width = width!; canvas.height = height!;
  canvas.setAttribute('aria-label', '所选 OME-Zarr 分块像素'); canvas.dataset.testid = 'ome-zarr-canvas';
  // Keep the element's bounds identical to the image, so hover coordinates
  // cannot be shifted by object-fit letterboxing on very wide/tall ROIs.
  canvas.style.cssText = `display:block;width:min(100%,640px,calc(55vh * ${width! / height!}));height:auto;image-rendering:pixelated;`;
  const context = canvas.getContext('2d'); if (!context) throw new Error('浏览器无法绘制分块像素。');
  task.onDispose(() => { canvas.onmousemove = null; canvas.width = 0; canvas.height = 0; canvas.remove(); });
  const pixels = context.createImageData(width!, height!), range = data.metadata.value_range as number[] | null;
  data.values.forEach((value, i) => { const gray = omePixelGray(value, range); pixels.data.set([gray ?? 0, gray ?? 0, gray ?? 0, gray === null ? 0 : 255], i * 4); });
  context.putImageData(pixels, 0, 0);
  canvas.onmousemove = event => {
    if (!task.isCurrent()) return;
    const bounds = canvas.getBoundingClientRect();
    const column = Math.min(width! - 1, Math.max(0, Math.floor((event.clientX - bounds.left) / bounds.width * width!)));
    const row = Math.min(height! - 1, Math.max(0, Math.floor((event.clientY - bounds.top) / bounds.height * height!)));
    const px = x! + column, py = y! + row;
    hover.value = `索引 x=${px}, y=${py}；原值=${data.values![row * width! + column] ?? '非有限值'}；坐标 x=${px * item.scale.slice(-1)[0]! + item.translation.slice(-1)[0]!}, y=${py * item.scale.slice(-2)[0]! + item.translation.slice(-2)[0]!}`;
  };
  target.value.replaceChildren(canvas);
}
async function load(kind: 'tree' | 'image') {
  if (busy.value && kind === 'image') return;
  const task = loads.begin(); drawing.begin(); displayed.value = undefined; error.value = ''; hover.value = '';
  if (kind === 'tree') { details.value = undefined; version = undefined; level.value = 0; }
  busy.value = false;
  if (!props.plugin.enabled) return;
  busy.value = true;
  try {
    const pinned = version;
    const selection = kind === 'image' && details.value ? validateOmeSelection({ level: level.value, indices: [...indices.value], roi: [...roi.value] }, details.value) : undefined;
    if (kind === 'image' && (!pinned || !selection)) throw new Error('请先读取数据结构并确认选择。');
    const response = await requestPreview(props.file, props.plugin, kind === 'tree' ? { kind } : { kind, version: pinned, ...selection }, task.signal);
    task.assertCurrent();
    if (typeof response.version !== 'string' || !/^[0-9a-f]{64}$/.test(response.version) || (kind === 'image' && response.version !== pinned)) throw new Error('分块资源版本已变化，请重新读取结构。');
    const parsed = parseOmeZarr(response, kind, selection);
    if (kind === 'tree') { details.value = parsed; version = response.version; resetSelection(); }
    else {
      if (JSON.stringify(parsed.levels) !== JSON.stringify(details.value!.levels) || JSON.stringify(parsed.axes) !== JSON.stringify(details.value!.axes)) throw new Error('图像层级与已检查结构不一致。');
      displayed.value = parsed; await draw(parsed); task.assertCurrent();
    }
  } catch (reason) { if (task.isCurrent()) error.value = reason instanceof Error ? reason.message : 'OME-Zarr 预览失败。'; }
  finally { if (task.isCurrent()) busy.value = false; }
}
watch(level, () => { if (!busy.value) resetSelection(); });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void load('tree'); }, { immediate: true, flush: 'sync' });
</script>
<template><section class="ome-zarr-preview">
  <p class="notice">本地 OME-NGFF 0.4 / Zarr v2 试点 · 从已登记 .zarr 目录的 .zattrs 打开。只读指定层级和平面的数据块，不连接公网存储。</p>
  <button class="reload" :disabled="busy || !plugin.enabled" @click="load('tree')">重新读取结构</button>
  <p v-if="busy" role="status">正在读取授权的元信息或数据块…</p><p v-if="error" role="alert">{{ error }}</p>
  <form v-if="details && selectedLevel" @submit.prevent="load('image')">
    <label>金字塔层级<select v-model.number="level" :disabled="busy"><option v-for="item in details.levels" :key="item.level" :value="item.level">{{ item.level }} · {{ item.shape.join(' × ') }} · {{ item.dtype }}</option></select></label>
    <label v-for="(axis, index) in details.axes.slice(0, -2)" :key="axis.name">{{ axis.name.toUpperCase() }} 索引<input v-model.number="indices[index]" type="number" min="0" :max="selectedLevel.shape[index]! - 1" step="1" required :disabled="busy" /></label>
    <label v-for="(label, index) in ['ROI X', 'ROI Y', '宽度', '高度']" :key="label">{{ label }}<input v-model.number="roi[index]" type="number" :min="index < 2 ? 0 : 1" :max="index < 2 ? selectedLevel.shape.slice(index === 0 ? -1 : -2)[0]! - 1 : 128" step="1" required :disabled="busy" /></label>
    <button type="submit" :disabled="busy">读取所选分块区域</button>
  </form>
  <p v-if="details" class="notice">轴顺序 {{ details.axes.map(axis => axis.name).join(' / ') }}；单位 {{ details.axes.map(axis => axis.unit ?? '未声明').join(' / ') }}。索引从 0 开始；行向下、列向右。每次最多 128×128 像素、64 块；缺失块拒绝读取，不补零。</p>
  <p v-if="details && !displayed && !busy" class="notice">结构已就绪，尚未读取图像像素。请选择层级、平面和区域后点击读取。</p>
  <div ref="target" class="canvas-container" />
  <p v-if="displayed" class="notice" data-testid="ome-zarr-stats">当前层级 {{ displayed.selected.level }}，平面 {{ displayed.selected.indices }}，ROI {{ displayed.selected.roi }}；读取 {{ displayed.metadata.read_bytes }} / {{ displayed.metadata.source_bytes }} 字节，{{ displayed.metadata.loaded_chunks }} 个块。显示灰度范围 {{ displayed.metadata.value_range }}，仅屏幕显示缩放；原始数值未修改，有限填充值不掩膜，非有限值透明。</p>
  <p v-if="hover" class="notice">{{ hover }}</p>
</section></template>
<style scoped>.ome-zarr-preview{display:flex;flex-direction:column;gap:10px;padding:12px;overflow:auto;height:100%}form{display:flex;flex-wrap:wrap;gap:10px;align-items:end}label{display:flex;flex-direction:column;gap:5px;font-size:12px}input,select,button{border:1px solid #ccd3db;border-radius:4px;padding:6px 8px}input{width:86px}button:disabled{opacity:.5}.reload{align-self:flex-start}.notice{font-size:12px;line-height:1.7;color:#667085}.canvas-container{flex-shrink:0;background:repeating-conic-gradient(#e5e7eb 0% 25%,#fff 0% 50%) 50%/16px 16px}[role=alert]{color:#b42318}</style>
