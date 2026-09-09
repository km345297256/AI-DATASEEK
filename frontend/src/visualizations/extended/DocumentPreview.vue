<template>
  <section class="flex min-h-0 flex-1 flex-col">
    <div class="flex flex-wrap items-center gap-3 border-b p-3 text-sm">
      <button :disabled="page <= 1 || !document" @click="page--">上一页</button>
      <span>{{ page }} / {{ pages || '—' }}</span>
      <button :disabled="page >= pages || !document" @click="page++">下一页</button>
      <label>缩放 <select v-model.number="zoom"><option :value="0.75">75%</option><option :value="1">100%</option><option :value="1.5">150%</option></select></label>
      <span class="text-xs text-gray-500">只读静态预览 · 不执行脚本、宏或表单动作</span>
    </div>
    <p v-if="busy" role="status" class="p-4">{{ plugin.reader === 'office' ? '正在隔离环境中转换文档，最多约 50 秒…' : '正在读取 PDF…' }}</p>
    <p v-if="error" role="alert" class="p-4 text-amber-700">{{ error }}</p>
    <p v-if="plugin.reader === 'office'" class="px-4 py-2 text-xs text-gray-500">字体与原件可能存在差异；不播放动画、音视频，不显示演讲者备注。</p>
    <div class="min-h-0 flex-1 overflow-auto bg-gray-100 p-4"><canvas ref="canvas" class="mx-auto bg-white shadow" aria-label="文档当前页" /></div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref, shallowRef, watch } from 'vue';
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { loadPluginBytes, requestPreview } from './runtime';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const canvas = ref<HTMLCanvasElement>();
const document = shallowRef<PDFDocumentProxy>();
const page = ref(1), pages = ref(0), zoom = ref(1), busy = ref(false), error = ref('');
const files = usePreviewLoad(), renders = usePreviewLoad();
GlobalWorkerOptions.workerSrc = workerUrl;
async function render() {
  const load = renders.begin();
  if (!document.value || !canvas.value) return;
  try {
    const pdfPage = await document.value.getPage(page.value);
    load.assertCurrent();
    const normal = pdfPage.getViewport({ scale: zoom.value });
    const scale = Math.min(1, Math.sqrt(8 * 1024 * 1024 / (normal.width * normal.height)), 8192 / Math.max(normal.width, normal.height));
    const viewport = pdfPage.getViewport({ scale: zoom.value * scale });
    if (!Number.isFinite(viewport.width) || !Number.isFinite(viewport.height) || viewport.width <= 0 || viewport.height <= 0) throw new Error('PDF 页面尺寸无效。');
    // A fresh canvas prevents overlapping cancelled PDF renders on the same canvas.
    const scratch = window.document.createElement('canvas');
    scratch.width = Math.ceil(viewport.width); scratch.height = Math.ceil(viewport.height);
    const task = pdfPage.render({ canvas: scratch, canvasContext: scratch.getContext('2d')!, viewport });
    load.onDispose(() => { task.cancel(); scratch.width = 0; scratch.height = 0; });
    await task.promise; load.assertCurrent();
    canvas.value.width = scratch.width; canvas.value.height = scratch.height;
    canvas.value.getContext('2d')!.drawImage(scratch, 0, 0);
    pdfPage.cleanup();
  } catch {
    if (load.isCurrent()) error.value = '页面无法安全渲染，请下载原件查看。';
  }
}
onMounted(async () => {
  const load = files.begin(); busy.value = true;
  try {
    let bytes: Uint8Array;
    if (props.plugin.reader === 'office') {
      const result = await requestPreview(props.file, props.plugin, {}, load.signal);
      if (result.media_type !== 'application/pdf' || typeof result.data_base64 !== 'string' || result.data_base64.length > 8 * 1024 * 1024) throw new Error('文档转换结果不符合协议。');
      bytes = Uint8Array.from(atob(result.data_base64), c => c.charCodeAt(0));
    } else bytes = new Uint8Array(await loadPluginBytes(props.file, props.plugin, load.signal));
    load.assertCurrent();
    // PDF.js 6 removed eval-based font compilation; no scripting/annotation layer is mounted.
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
watch([page, zoom], render);
</script>
