<template>
  <section class="flex h-full min-h-0 flex-1 flex-col bg-white" :class="{ 'office-expanded': expanded }">
    <div class="flex shrink-0 flex-wrap items-center gap-3 border-b p-3 text-sm">
      <span>ONLYOFFICE 本机阅读</span>
      <button :disabled="busy" @click="openViewer">重新打开</button>
      <button :aria-pressed="expanded" @click="expanded = !expanded">{{ expanded ? '退出全屏' : '全屏阅读' }}</button>
      <span class="text-xs text-gray-500">隔离只读 · 不回写原件 · 15 分钟后关闭</span>
    </div>
    <p v-if="busy" role="status" class="shrink-0 p-4">正在准备本机办公阅读，首次打开可能需要稍等…</p>
    <p v-if="error" role="alert" class="shrink-0 p-4 text-amber-700">{{ error }} 可切换到其他预览方式。</p>
    <iframe v-if="frameUrl" ref="frame" :src="frameUrl" title="ONLYOFFICE 本机只读文档"
      class="min-h-0 w-full flex-1 border-0" sandbox="allow-scripts allow-same-origin"
      referrerpolicy="no-referrer" allow="camera 'none'; microphone 'none'; geolocation 'none'; payment 'none'" />
  </section>
</template>
<script setup lang="ts">
import { onMounted, onScopeDispose, ref } from 'vue';
import type { FileInfo } from '../../api/file';
import { BASE_URL } from '../../api/client';
import type { VisualizationPlugin } from '../contract';
import { requestVisualization } from '../runtime';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { parseOfficeResource } from './onlyOfficeResource';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const frame = ref<HTMLIFrameElement>(), frameUrl = ref('');
const busy = ref(false), error = ref(''), expanded = ref(false);
const loads = usePreviewLoad();
async function revoke(lease: string) {
  try {
    await fetch(`${BASE_URL}/office-viewer/leases/${encodeURIComponent(lease)}/revoke`, {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'X-Visualization-Action': 'revoke' },
    });
  } catch { /* Server-side expiry remains authoritative when leaving offline. */ }
}
async function openViewer() {
  const load = loads.begin(); busy.value = true; error.value = ''; frameUrl.value = '';
  try {
    const result = await requestVisualization(props.file, props.plugin, 'prepare', {}, load.signal);
    const resource = parseOfficeResource(result, window.location.href);
    if (!load.isCurrent()) { void revoke(resource.lease); return; }
    frameUrl.value = resource.frameUrl;
    const expired = () => {
      if (!load.isCurrent()) return;
      frameUrl.value = ''; busy.value = false; error.value = '阅读授权已结束，请重新打开。';
      void revoke(resource.lease);
    };
    const expiryTimer = window.setTimeout(expired, Math.max(0, resource.expiresAt * 1000 - Date.now()));
    const startupTimer = window.setTimeout(() => {
      if (load.isCurrent() && busy.value) { busy.value = false; error.value = '本机文档服务尚未完成加载，请稍后重新打开。'; }
    }, 90_000);
    const receive = (event: MessageEvent) => {
      if (!load.isCurrent() || event.origin !== resource.origin || event.source !== frame.value?.contentWindow) return;
      if (event.data?.type === 'dataseek-office-ready') { busy.value = false; error.value = ''; window.clearTimeout(startupTimer); }
      else if (event.data?.type === 'dataseek-office-error') { busy.value = false; error.value = '文档无法在本机阅读器中打开。'; }
      else if (event.data?.type === 'dataseek-office-expired') expired();
    };
    window.addEventListener('message', receive);
    load.onDispose(() => {
      window.clearTimeout(expiryTimer); window.clearTimeout(startupTimer);
      window.removeEventListener('message', receive); frameUrl.value = ''; void revoke(resource.lease);
    });
  } catch (cause) {
    if (load.isCurrent()) { busy.value = false; error.value = cause instanceof Error ? cause.message : '本机办公阅读不可用。'; }
  }
}
const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') expanded.value = false; };
onMounted(() => { window.addEventListener('keydown', escape); void openViewer(); });
onScopeDispose(() => { window.removeEventListener('keydown', escape); frameUrl.value = ''; });
</script>
<style scoped>
.office-expanded { position: fixed; inset: 0; z-index: 10000; }
</style>
