<template>
    <div class="flex flex-col items-center justify-center gap-6 flex-1 w-full min-h-0">
        <img :src="imageUrl" alt="Image" class="w-full h-full object-contain" />
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { getFileDownloadUrl } from '../../api/file';
import type { FileInfo } from '../../api/file';
import { usePreviewLoad } from '../../composables/usePreviewLoad';

const props = defineProps<{
    file: FileInfo;
}>();

const imageUrl = ref('');
const loads = usePreviewLoad();

watch(() => props.file, async (file) => {
    const load = loads.begin();
    imageUrl.value = '';
    if (!file) return;
    try {
        const url = await getFileDownloadUrl(file);
        if (load.isCurrent()) imageUrl.value = url;
    } catch (error) {
        if (load.isCurrent()) console.error('Failed to load image preview:', error);
    }
}, { immediate: true });
</script>
