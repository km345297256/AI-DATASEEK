<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <p class="mb-3 text-xs text-gray-500">本地只读播放 · 不自动播放或联网 · {{ video ? '视频上限 64 MiB' : '音频上限 16 MiB' }}。能否播放取决于浏览器支持的编码。</p>
    <p v-if="busy" role="status">正在读取媒体文件…</p>
    <p v-if="error" role="alert" class="mb-3 text-amber-700">{{ error }}</p>
    <video v-if="video" ref="videoElement" class="max-h-[65vh] w-full bg-black" controls preload="metadata" playsinline :src="source || undefined" @error="playbackError" @loadedmetadata="metadataLoaded" />
    <audio v-else ref="audioElement" class="w-full" controls preload="metadata" :src="source || undefined" @error="playbackError" @loadedmetadata="metadataLoaded" />
    <p v-if="duration !== undefined" class="mt-2 text-xs text-gray-500">时长 {{ duration.toFixed(2) }} 秒{{ videoSize ? ` · ${videoSize}` : '' }}</p>
    <template v-if="!video">
      <p class="mt-4 text-xs text-gray-500">仅对受限未压缩 WAV 绘制逐通道峰值包络；不是完整频谱或信号分析，不执行滤波或重采样。</p>
      <p v-if="waveNotice" role="status" class="mt-2 text-sm text-gray-500">{{ waveNotice }}</p>
      <p v-if="wave" class="mt-2 text-xs text-gray-500">{{ wave.channels }} 声道 · {{ wave.sampleRate }} Hz · {{ wave.frames }} 帧 · 纵轴为归一化振幅，非物理声压单位</p>
      <canvas v-show="wave" ref="canvas" class="mt-3 w-full rounded border" aria-label="音频逐通道峰值包络，横轴为从零到总时长的时间" role="img" />
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { loadPluginBytes } from './runtime';
import { mediaFormat, pcmWaveform, type Waveform } from './mediaData';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const video = computed(() => String(props.plugin.adapter) === 'video-player');
const videoElement = ref<HTMLVideoElement>(), audioElement = ref<HTMLAudioElement>(), canvas = ref<HTMLCanvasElement>();
const source = ref(''), busy = ref(false), error = ref(''), waveNotice = ref('');
const duration = ref<number>(), videoSize = ref(''), wave = shallowRef<Waveform>();
const loads = usePreviewLoad();
let stopped = false;
const media = () => videoElement.value ?? audioElement.value;
function releaseMedia(elements: Array<HTMLMediaElement | undefined> = [videoElement.value, audioElement.value]) {
  for (const element of elements) {
    if (!element) continue;
    // A detached element must not prevent other resources being released.
    try { element.pause(); element.removeAttribute('src'); element.load(); } catch { /* already detached */ }
  }
}
function currentMedia(event?: Event) {
  const element = media();
  if (stopped || !source.value || !element || (event && event.target !== element)
    || (element.currentSrc && element.currentSrc !== source.value)) return undefined;
  return element;
}
function playbackError(event?: Event) {
  if (currentMedia(event)?.error) error.value = '浏览器无法播放此媒体编码或文件已损坏；本插件不会自动转码或下载编解码器。';
}
function metadataLoaded(event?: Event) {
  const element = currentMedia(event);
  if (!element) return;
  if (!Number.isFinite(element.duration) || element.duration < 0) {
    error.value = '无法确定媒体时长，请下载原件查看。'; releaseMedia(); return;
  }
  duration.value = element.duration;
  const target = videoElement.value;
  if (target && (target.videoWidth > 8192 || target.videoHeight > 8192 || target.videoWidth * target.videoHeight > 16777216)) {
    error.value = '视频分辨率超过交互式预览预算（最多 1600 万像素）。'; releaseMedia(); return;
  }
  videoSize.value = target ? `${target.videoWidth} × ${target.videoHeight}` : '';
}
function drawWave() {
  const target = canvas.value, value = wave.value;
  if (!target || !value) return;
  target.width = 1024; target.height = value.channels * 150;
  const context = target.getContext('2d');
  if (!context) { waveNotice.value = '浏览器无法创建波形画布；音频仍可尝试播放。'; return; }
  context.fillStyle = '#ffffff'; context.fillRect(0, 0, target.width, target.height);
  const extent = Math.max(1, ...value.peaks.flatMap(channel => channel.flatMap(bin => [Math.abs(bin.min), Math.abs(bin.max)])));
  for (let index = 0; index < value.channels; index++) {
    const bins = value.peaks[index]!, middle = index * 150 + 65;
    context.strokeStyle = '#d1d5db'; context.beginPath(); context.moveTo(0, middle); context.lineTo(1024, middle); context.stroke();
    context.strokeStyle = index === 0 ? '#267e64' : '#4675b9'; context.beginPath();
    for (let i = 0; i < bins.length; i++) {
      const bin = bins[i]!, x = i * 1024 / bins.length;
      context.moveTo(x, middle - bin.max / extent * 52); context.lineTo(x, middle - bin.min / extent * 52);
    }
    context.stroke(); context.fillStyle = '#4b5563'; context.font = '12px sans-serif';
    context.fillText(`声道 ${index + 1} · ±${extent.toPrecision(3)}`, 8, index * 150 + 15);
    context.fillText('0 s', 8, index * 150 + 143); context.fillText(`${value.duration.toFixed(2)} s`, 944, index * 150 + 143);
  }
}
async function loadMedia() {
  const load = loads.begin(); busy.value = true; error.value = ''; waveNotice.value = '';
  // Vue may clear template refs before effect-scope disposal. The load owns the
  // actual DOM resources, not a later lookup through possibly-cleared refs.
  const ownedElements = [videoElement.value, audioElement.value], ownedCanvas = canvas.value;
  load.onDispose(() => { releaseMedia(ownedElements); if (ownedCanvas) { ownedCanvas.width = 0; ownedCanvas.height = 0; } });
  source.value = ''; duration.value = undefined; videoSize.value = ''; wave.value = undefined;
  if (canvas.value) { canvas.value.width = 0; canvas.value.height = 0; }
  try {
    const file = props.file, plugin = props.plugin;
    const format = mediaFormat(file.filename, video.value ? 'video' : 'audio');
    const limitedPlugin = { ...plugin, limits: { ...plugin.limits, max_input_bytes: Math.min(plugin.limits.max_input_bytes, format.maxBytes) } };
    const bytes = await loadPluginBytes(file, limitedPlugin, load.signal);
    load.assertCurrent();
    if (bytes.byteLength === 0 || bytes.byteLength > format.maxBytes) throw new Error('媒体文件为空或超过交互式预览预算。');
    const url = URL.createObjectURL(new Blob([bytes], { type: format.mime }));
    load.onDispose(() => { URL.revokeObjectURL(url); });
    source.value = url;
    if (format.kind === 'audio') {
      if (format.extension === 'wav') {
        try { wave.value = pcmWaveform(bytes); }
        catch (reason) { waveNotice.value = reason instanceof Error ? reason.message : '此文件无法生成有界 WAV 波形。'; }
      } else waveNotice.value = '此压缩音频仅提供原生播放；波形功能目前仅支持受限未压缩 WAV。';
    }
    await nextTick(); load.assertCurrent(); drawWave();
  } catch (reason) {
    if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '媒体无法安全预览。';
  } finally { if (load.isCurrent()) busy.value = false; }
}
onMounted(loadMedia);
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version, () => props.plugin.enabled], loadMedia);
onScopeDispose(() => { stopped = true; source.value = ''; wave.value = undefined; if (canvas.value) { canvas.value.width = 0; canvas.value.height = 0; } });
</script>
