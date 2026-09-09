<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div v-if="metadata" class="flex shrink-0 items-center justify-between border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <span>{{ metadata }}</span>
      <span>{{ t('TIFF Preview') }}</span>
    </div>
    <div class="flex min-h-0 flex-1 items-center justify-center overflow-auto p-4">
      <canvas
        ref="canvasRef"
        class="max-h-full max-w-full rounded-lg bg-white shadow-[0px_8px_32px_0px_rgba(0,0,0,0.08)]"
      />
      <div v-if="status" class="rounded-lg bg-[var(--fill-tsp-gray-main)] px-3 py-2 text-sm text-[var(--text-secondary)]">
        {{ status }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { getFileDownloadUrl } from '../../api/file';
import type { FileInfo } from '../../api/file';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { readBoundedBinary, validateTiffDimensions } from '../../visualizations/boundedBinary';

interface TiffImageDirectory {
  width: number;
  height: number;
  t256?: number[];
  t257?: number[];
  t258?: number[];
  t277?: number[];
}

interface UTIFRuntime {
  decode: (buffer: ArrayBuffer) => TiffImageDirectory[];
  decodeImage: (buffer: ArrayBuffer, ifd: TiffImageDirectory, ifds?: TiffImageDirectory[]) => void;
  toRGBA8: (ifd: TiffImageDirectory) => Uint8Array;
}

declare global {
  interface Window {
    UTIF?: UTIFRuntime;
  }
}

const props = defineProps<{
  file: FileInfo;
}>();

const { t } = useI18n();
const canvasRef = ref<HTMLCanvasElement | null>(null);
const status = ref('');
const metadata = ref('');
const loads = usePreviewLoad();
let scriptPromise: Promise<UTIFRuntime> | null = null;

const loadUTIF = () => {
  if (window.UTIF) return Promise.resolve(window.UTIF);
  if (scriptPromise) return scriptPromise;

  scriptPromise = new Promise<UTIFRuntime>((resolve, reject) => {
    const existingScript = document.querySelector<HTMLScriptElement>('script[data-utif="true"]');
    if (existingScript) {
      existingScript.addEventListener('load', () => {
        window.UTIF ? resolve(window.UTIF) : reject(new Error('UTIF runtime not available'));
      }, { once: true });
      existingScript.addEventListener('error', () => reject(new Error('Failed to load UTIF')), { once: true });
      return;
    }

    const script = document.createElement('script');
    script.src = '/vendor/utif.min.js';
    script.async = true;
    script.dataset.utif = 'true';
    script.onload = () => {
      window.UTIF ? resolve(window.UTIF) : reject(new Error('UTIF runtime not available'));
    };
    script.onerror = () => reject(new Error('Failed to load UTIF'));
    document.head.appendChild(script);
  });

  return scriptPromise;
};

const renderTiff = async (file: FileInfo) => {
  const load = loads.begin();
  status.value = '';
  metadata.value = '';

  const canvas = canvasRef.value;
  if (canvas) {
    canvas.width = 0;
    canvas.height = 0;
  }
  if (!file?.file_id) return;

  status.value = t('Loading TIFF image...');
  try {
    const url = await getFileDownloadUrl(file);
    load.assertCurrent();
    const response = await fetch(url, { signal: load.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const buffer = await readBoundedBinary(response, 64 * 1024 * 1024, load.signal);
    if (!load.isCurrent()) return;

    const UTIF = await loadUTIF();
    if (!load.isCurrent()) return;

    const ifds = UTIF.decode(buffer);
    if (!ifds.length) throw new Error('No image frames found');
    if (ifds.length > 128) throw new Error('TIFF 页数超过安全预览上限（128 页），请下载后分析。');
    // UTIF sets width/height during decodeImage; inspect raw directory tags
    // before it allocates decompression buffers or any RGBA conversion.
    const { width, height } = validateTiffDimensions(ifds[0]);
    UTIF.decodeImage(buffer, ifds[0], ifds);
    const rgba = UTIF.toRGBA8(ifds[0]);
    if (rgba.length !== width * height * 4) throw new Error('Invalid TIFF pixel buffer');

    const targetCanvas = canvasRef.value;
    const context = targetCanvas?.getContext('2d');
    if (!targetCanvas || !context || !load.isCurrent()) return;

    targetCanvas.width = width;
    targetCanvas.height = height;
    context.putImageData(new ImageData(new Uint8ClampedArray(rgba), width, height), 0, 0);
    metadata.value = `${width} x ${height}${ifds.length > 1 ? ` · ${ifds.length} pages` : ''}`;
    status.value = '';
  } catch (error) {
    if (!load.isCurrent()) return;
    status.value = error instanceof Error && /安全|浏览器/.test(error.message) ? error.message : t('Failed to render TIFF image');
  }
};

watch(() => props.file, renderTiff, { immediate: true });
</script>
