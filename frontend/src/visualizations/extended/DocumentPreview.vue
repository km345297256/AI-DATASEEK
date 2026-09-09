<template>
  <section class="document-preview relative flex h-full min-h-0 flex-1 flex-col bg-white" :class="{ 'document-preview-expanded': expanded }">
    <div class="flex shrink-0 flex-wrap items-center gap-3 border-b p-3 text-sm">
      <button :disabled="page <= 1 || !document" @click="page--">上一页</button>
      <span>{{ page }} / {{ pages || '—' }}</span>
      <button :disabled="page >= pages || !document" @click="page++">下一页</button>
      <label>缩放 <select v-model="zoom">
        <option value="fit-width">适应宽度</option><option value="fit-page">适应页面</option>
        <option v-for="value in [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4]" :key="value" :value="String(value)">{{ value * 100 }}%</option>
      </select></label>
      <button :aria-pressed="expanded" @click="expanded = !expanded">{{ expanded ? '退出全屏' : '全屏阅读' }}</button>
      <span class="text-xs text-gray-500">只读静态预览 · 不执行脚本、宏或表单动作</span>
    </div>
    <p v-if="busy" role="status" class="shrink-0 p-4">{{ plugin.reader === 'office' ? '正在隔离环境中转换文档，最多约 50 秒…' : '正在读取 PDF…' }}</p>
    <p v-if="error" role="alert" class="shrink-0 p-4 text-amber-700">{{ error }}</p>
    <p v-if="plugin.reader === 'office'" class="shrink-0 px-4 py-2 text-xs text-gray-500">字体与原件可能存在差异；不播放动画、音视频，不显示演讲者备注。</p>
    <details v-if="conversionWarnings.length" class="shrink-0 px-4 pb-2 text-xs text-gray-500">
      <summary class="cursor-pointer">转换说明</summary>
      <ul class="list-inside list-disc pt-1"><li v-for="(warning, index) in conversionWarnings" :key="index">{{ warning }}</li></ul>
    </details>
    <div ref="container" class="document-page-container min-h-0 flex-1 overflow-auto bg-gray-100 p-4" :aria-busy="rendering">
      <canvas ref="canvas" class="document-page mx-auto bg-white shadow" aria-label="文档当前页" />
    </div>
    <p v-if="budgetLimited" role="status" class="absolute bottom-2 left-4 right-4 rounded bg-white/95 px-4 py-2 text-xs text-gray-500">当前页已达到高清画布预算；缩放比例保持不变。可使用适应页面，或下载原件查看超大页面细节。</p>
  </section>
</template>
<script setup lang="ts">
import { onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue';
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type RenderTask } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { loadPluginBytes, requestPreview } from './runtime';
import { documentGeometry, type DocumentZoom } from './documentGeometry';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const canvas = ref<HTMLCanvasElement>(), container = ref<HTMLDivElement>();
const document = shallowRef<PDFDocumentProxy>();
const page = ref(1), pages = ref(0), zoom = ref('1'), busy = ref(false), error = ref('');
const expanded = ref(false), rendering = ref(false), budgetLimited = ref(false);
const conversionWarnings = ref<string[]>([]);
const availableWidth = ref(1), availableHeight = ref(1), pixelRatio = ref(1);
const files = usePreviewLoad(), renders = usePreviewLoad();
let requestedRender = '';
GlobalWorkerOptions.workerSrc = workerUrl;

async function render() {
  if (!document.value || !canvas.value) return;
  const fit = zoom.value.startsWith('fit-');
  const key = [page.value, zoom.value, pixelRatio.value, fit ? availableWidth.value : '', fit ? availableHeight.value : ''].join(':');
  if (key === requestedRender) return;
  requestedRender = key;
  const load = renders.begin(); rendering.value = true; error.value = '';
  let cleanupPage: (() => void) | undefined;
  try {
    const pdfPage = await document.value.getPage(page.value);
    load.assertCurrent(); cleanupPage = () => { pdfPage.cleanup(); };
    const natural = pdfPage.getViewport({ scale: 1 });
    const geometry = documentGeometry({ pageWidth: natural.width, pageHeight: natural.height,
      zoom: fit ? zoom.value as DocumentZoom : Number(zoom.value), availableWidth: availableWidth.value,
      availableHeight: availableHeight.value, devicePixelRatio: pixelRatio.value });
    const viewport = pdfPage.getViewport({ scale: geometry.viewportScale });
    // A fresh surface prevents cancelled PDF renders from painting into a newer page.
    const scratch = window.document.createElement('canvas');
    scratch.width = geometry.pixelWidth; scratch.height = geometry.pixelHeight;
    let task: RenderTask | undefined;
    let released = false;
    const release = () => { if (released) return; released = true; task?.cancel(); scratch.width = 0; scratch.height = 0; };
    load.onDispose(release);
    try {
      const context = scratch.getContext('2d');
      if (!context) throw new Error('浏览器无法创建文档画布。');
      task = pdfPage.render({ canvas: scratch, canvasContext: context, viewport,
        transform: [geometry.scaleX, 0, 0, geometry.scaleY, 0, 0] });
      await task.promise; load.assertCurrent();
      const target = canvas.value;
      target.width = geometry.pixelWidth; target.height = geometry.pixelHeight;
      target.style.width = `${geometry.cssWidth}px`; target.style.height = `${geometry.cssHeight}px`;
      target.getContext('2d')!.drawImage(scratch, 0, 0);
      budgetLimited.value = geometry.budgetLimited;
    } finally { release(); }
  } catch (e) {
    if (load.isCurrent()) { requestedRender = ''; error.value = e instanceof Error ? e.message : '页面无法安全渲染，请下载原件查看。'; }
  } finally {
    cleanupPage?.();
    if (load.isCurrent()) rendering.value = false;
  }
}

onMounted(async () => {
  const load = files.begin(); busy.value = true;
  let resizeFrame = 0;
  let densityQuery: MediaQueryList | undefined;
  const measure = () => {
    if (!load.isCurrent() || !container.value) return;
    const style = getComputedStyle(container.value);
    availableWidth.value = Math.max(1, container.value.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight));
    availableHeight.value = Math.max(1, container.value.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom));
    pixelRatio.value = window.devicePixelRatio || 1;
  };
  const resized = () => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(measure); };
  const densityChanged = () => { measure(); observeDensity(); };
  const observeDensity = () => {
    densityQuery?.removeEventListener('change', densityChanged);
    densityQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    densityQuery.addEventListener('change', densityChanged);
  };
  const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') expanded.value = false; };
  const observer = new ResizeObserver(resized);
  if (container.value) observer.observe(container.value);
  window.addEventListener('resize', resized); window.addEventListener('keydown', escape);
  observeDensity(); measure();
  load.onDispose(() => {
    observer.disconnect(); cancelAnimationFrame(resizeFrame);
    densityQuery?.removeEventListener('change', densityChanged);
    window.removeEventListener('resize', resized); window.removeEventListener('keydown', escape);
  });
  try {
    let bytes: Uint8Array;
    if (props.plugin.reader === 'office') {
      const result = await requestPreview(props.file, props.plugin, {}, load.signal);
      if (result.media_type !== 'application/pdf' || typeof result.data_base64 !== 'string' || result.data_base64.length > 8 * 1024 * 1024) throw new Error('文档转换结果不符合协议。');
      load.assertCurrent();
      conversionWarnings.value = Array.isArray(result.warnings) ? result.warnings.filter((warning): warning is string => typeof warning === 'string' && Boolean(warning.trim())).slice(0, 5).map(warning => warning.trim().slice(0, 300)) : [];
      bytes = Uint8Array.from(atob(result.data_base64), c => c.charCodeAt(0));
    } else bytes = new Uint8Array(await loadPluginBytes(props.file, props.plugin, load.signal));
    load.assertCurrent();
    // Keep fonts local/path-rendered. No scripting, XFA, annotation or link layer is mounted.
    const task = getDocument({ data: bytes, enableXfa: false, disableFontFace: true,
      useSystemFonts: false, stopAtErrors: true, maxImageSize: 16 * 1024 * 1024,
      canvasMaxAreaInBytes: 64 * 1024 * 1024, disableAutoFetch: true,
      cMapUrl: '/visualization-assets/pdfjs/cmaps/', cMapPacked: true,
      standardFontDataUrl: '/visualization-assets/pdfjs/standard_fonts/', wasmUrl: '/visualization-assets/pdfjs/wasm/' });
    load.onDispose(() => { renders.dispose(); void task.destroy(); });
    const pdf = await task.promise; load.assertCurrent();
    if (pdf.numPages > 1000) throw new Error('PDF 页数超过交互式预览上限（1000页）。');
    document.value = pdf; pages.value = pdf.numPages; await render();
  } catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '文档无法预览。'; }
  finally { if (load.isCurrent()) busy.value = false; }
});
watch([page, zoom, availableWidth, availableHeight, pixelRatio], render);
onScopeDispose(() => { if (canvas.value) { canvas.value.width = 0; canvas.value.height = 0; } document.value = undefined; });
</script>
<style scoped>
.document-preview-expanded { position: fixed; inset: 0; z-index: 100; height: 100dvh; }
.document-page-container { scrollbar-gutter: stable; }
.document-page { display: block; max-width: none; }
</style>
