<template>
  <div class="relative flex min-h-0 flex-1 flex-col bg-white">
    <div ref="viewerContainer" class="min-h-0 flex-1" />
    <div v-if="status" class="absolute left-4 top-4 rounded-lg bg-black/70 px-3 py-2 text-xs text-white">
      {{ status }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { getFileDownloadUrl } from '../../api/file';
import type { FileInfo } from '../../api/file';

declare global {
  interface Window {
    OV?: {
      RGBColor: new (r: number, g: number, b: number) => unknown;
      RGBAColor: new (r: number, g: number, b: number, a: number) => unknown;
      EmbeddedViewer: new (
        element: HTMLElement,
        parameters?: Record<string, unknown>,
      ) => {
        LoadModelFromUrlList: (urls: string[]) => void;
        Resize: () => void;
        Destroy: () => void;
      };
    };
  }
}

const props = defineProps<{
  file: FileInfo;
}>();

const viewerContainer = ref<HTMLElement | null>(null);
const status = ref('');
let viewer: InstanceType<NonNullable<typeof window.OV>['EmbeddedViewer']> | null = null;
let resizeObserver: ResizeObserver | null = null;
let loadVersion = 0;
let scriptPromise: Promise<void> | null = null;

const loadOnline3DViewer = () => {
  if (window.OV?.EmbeddedViewer) return Promise.resolve();
  if (scriptPromise) return scriptPromise;

  scriptPromise = new Promise<void>((resolve, reject) => {
    const existingScript = document.querySelector<HTMLScriptElement>('script[data-o3dv="true"]');
    if (existingScript) {
      existingScript.addEventListener('load', () => resolve(), { once: true });
      existingScript.addEventListener('error', () => reject(new Error('Failed to load Online3DViewer')), { once: true });
      return;
    }

    const script = document.createElement('script');
    script.src = '/vendor/o3dv.min.js';
    script.async = true;
    script.dataset.o3dv = 'true';
    script.onload = () => resolve();
    script.onerror = () => reject(new Error('Failed to load Online3DViewer'));
    document.head.appendChild(script);
  });

  return scriptPromise;
};

const destroyViewer = () => {
  resizeObserver?.disconnect();
  resizeObserver = null;
  viewer?.Destroy();
  viewer = null;
  if (viewerContainer.value) {
    viewerContainer.value.innerHTML = '';
  }
};

const renderObj = async (file: FileInfo) => {
  const currentVersion = ++loadVersion;
  destroyViewer();
  if (!file?.file_id || !viewerContainer.value) return;

  status.value = 'Loading 3D model...';
  try {
    await loadOnline3DViewer();
    await nextTick();
    if (currentVersion !== loadVersion || !viewerContainer.value || !window.OV?.EmbeddedViewer) return;

    const url = await getFileDownloadUrl(file);
    if (currentVersion !== loadVersion || !viewerContainer.value) return;

    viewer = new window.OV.EmbeddedViewer(viewerContainer.value, {
      backgroundColor: new window.OV.RGBAColor(255, 255, 255, 255),
      defaultColor: new window.OV.RGBColor(160, 160, 160),
      onModelLoaded: () => {
        status.value = '';
      },
      onModelLoadFailed: () => {
        status.value = 'Failed to load OBJ model';
      },
    });
    viewer.LoadModelFromUrlList([url]);

    resizeObserver = new ResizeObserver(() => viewer?.Resize());
    resizeObserver.observe(viewerContainer.value);
  } catch (error) {
    console.error('Failed to render OBJ file:', error);
    status.value = 'Failed to initialize 3D viewer';
  }
};

watch(() => props.file, renderObj, { immediate: true });

onBeforeUnmount(() => {
  loadVersion++;
  destroyViewer();
});
</script>
