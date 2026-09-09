<template>
  <PreviewFrame :loading="loading" :error="error" note="DOCX 清晰阅读 · 可选择文字、50%–300% 缩放。仅本地只读；复杂分页和图表可切换 Word 分页预览，嵌入字体不加载。">
    <div class="mb-2 flex items-center gap-3 text-sm">
      <label>缩放 <select v-model.number="zoom" aria-label="DOCX 缩放" class="rounded border px-2 py-1">
        <option v-for="value in [0.5, 0.75, 1, 1.25, 1.5, 2, 3]" :key="value" :value="value">{{ value * 100 }}%</option>
      </select></label>
      <span v-if="pages">{{ pages }} 个排版区段（不等同原文页数）</span>
    </div>
    <div ref="target" class="min-h-[420px] flex-1" />
  </PreviewFrame>
</template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { loadPluginBytes } from '../runtime';
import { loadDocxBootstrap } from './docxBootstrap';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import PreviewFrame from './scientific/PreviewFrame.vue';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), loading = ref(false), error = ref(''), pages = ref(0), zoom = ref(1);
let frame: HTMLIFrameElement | undefined, channel = '';
const post = (data: Record<string, unknown>, transfer: Transferable[] = []) => frame?.contentWindow?.postMessage({ ...data, channel }, '*', transfer);
watch(zoom, value => post({ type: 'zoom', zoom: value }));
watch(() => [props.file.file_id, props.plugin.id], async () => {
  const load = scope.begin(); loading.value = true; error.value = ''; pages.value = 0;
  try {
    const [bytes, library] = await Promise.all([
      loadPluginBytes(props.file, props.plugin, load.signal),
      loadDocxBootstrap(load.signal),
    ]);
    load.assertCurrent(); await nextTick(); load.assertCurrent();
    channel = crypto.randomUUID();
    const currentChannel = channel, currentFrame = document.createElement('iframe');
    frame = currentFrame;
    currentFrame.title = 'DOCX 隔离只读阅读'; currentFrame.setAttribute('sandbox', 'allow-scripts');
    currentFrame.setAttribute('referrerpolicy', 'no-referrer');
    currentFrame.style.cssText = 'width:100%;height:100%;min-height:420px;border:0;background:#ececec';
    const nonce = crypto.randomUUID().replace(/-/g, ''), origin = window.location.origin;
    // Only trusted bundled code receives a nonce. The document cannot access the
    // parent origin, navigate it, create popups, load remote resources or run scripts.
    // Center narrow pages, but keep enlarged pages inside the reachable scroll
    // range: ordinary flex centering otherwise clips the document's left side.
    currentFrame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src blob:; font-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><meta name="channel" content="${currentChannel}"><meta name="parent-origin" content="${origin}"><style>html,body{margin:0;background:#ececec;font-family:Arial,'Noto Sans CJK SC',sans-serif}*{box-sizing:border-box}a{pointer-events:none;color:inherit}#content{min-height:100vh}#content .docx-wrapper{align-items:safe center}</style></head><body><div id="content"></div><script nonce="${nonce}">${library}<\/script></body></html>`;
    const timeout = window.setTimeout(() => {
      if (load.isCurrent()) { error.value = 'DOCX 阅读超时，请切换 Word 分页预览。'; loading.value = false; currentFrame.remove(); }
    }, 30000);
    const listener = (event: MessageEvent) => {
      if (!load.isCurrent() || event.source !== currentFrame.contentWindow || event.origin !== 'null' || event.data?.channel !== currentChannel) return;
      if (event.data.type === 'ready') {
        const buffer = bytes.slice(0);
        post({ type: 'render', bytes: buffer }, [buffer]); post({ type: 'zoom', zoom: zoom.value });
      } else if (event.data.type === 'rendered') {
        window.clearTimeout(timeout); loading.value = false;
        pages.value = Number.isInteger(event.data.pages) ? event.data.pages : 0;
      } else if (event.data.type === 'error') {
        window.clearTimeout(timeout); loading.value = false;
        error.value = typeof event.data.message === 'string' ? event.data.message : 'DOCX 阅读失败。';
      }
    };
    window.addEventListener('message', listener);
    load.onDispose(() => { window.clearTimeout(timeout); window.removeEventListener('message', listener); currentFrame.remove(); if (frame === currentFrame) frame = undefined; });
    target.value!.replaceChildren(currentFrame);
  } catch (reason) { if (load.isCurrent()) { error.value = reason instanceof Error ? reason.message : 'DOCX 阅读失败。'; loading.value = false; } }
}, { immediate: true });
</script>
