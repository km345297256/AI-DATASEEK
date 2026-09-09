<template>
    <div class="flex flex-col items-center justify-center gap-6 flex-1 w-full min-h-0">
        <img :src="imageUrl" alt="Image" class="w-full h-full object-contain" />
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { loadPluginBytes } from '../../visualizations/runtime';
import type { VisualizationPlugin } from '../../visualizations/contract';
import type { FileInfo } from '../../api/file';
import { usePreviewLoad } from '../../composables/usePreviewLoad';

const props = defineProps<{
    file: FileInfo;
    plugin: VisualizationPlugin;
}>();

const imageUrl = ref('');
const loads = usePreviewLoad();

watch(() => props.file, async (file) => {
    const load = loads.begin();
    imageUrl.value = '';
    if (!file) return;
    try {
        const bytes = await loadPluginBytes(file, props.plugin, load.signal);
        load.assertCurrent();
        const url = URL.createObjectURL(new Blob([bytes], { type: file.content_type || 'application/octet-stream' }));
        load.onDispose(() => URL.revokeObjectURL(url));
        if (load.isCurrent()) imageUrl.value = url;
    } catch (error) {
        if (load.isCurrent()) console.error('Failed to load image preview:', error);
    }
}, { immediate: true });
</script>
