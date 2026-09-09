<template>
    <div class="relative overflow-auto flex-1 min-h-0 p-5">
        <div class="relative w-full max-w-[768px] mx-auto" style="min-height: calc(-200px + 100vh);">
            <div class="prose prose-gray max-w-none dark:prose-invert [&_img]:max-w-full [&_img]:h-auto"
                 v-html="renderedContent">
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import type { FileInfo } from '../../api/file';
import { loadPluginBytes, loadPluginText } from '../../visualizations/runtime';
import type { VisualizationPlugin } from '../../visualizations/contract';
import { getSessionFiles, getSharedSessionFiles } from '../../api/agent';
import { useFilePanel } from '../../composables/useFilePanel';
import { useSessionFileList } from '../../composables/useSessionFileList';
import { usePreviewLoad, type PreviewLoad } from '../../composables/usePreviewLoad';
import {
    findRelatedFile,
    isRelativeResourceUrl,
    splitResourceUrl,
} from '../../utils/relativeFileResources';

const renderedContent = ref('');

const props = defineProps<{
    file: FileInfo;
    plugin: VisualizationPlugin;
}>();
const route = useRoute();
const { relatedFiles } = useFilePanel();
const { shared } = useSessionFileList();
const loads = usePreviewLoad();

// Configure marked options
marked.setOptions({
    breaks: true,
    gfm: true,
});

const ensureRelatedFiles = async (load: PreviewLoad, force = false) => {
    load.assertCurrent();
    if (!force && relatedFiles.value.length > 1) return;
    const sessionId = route.params.sessionId as string;
    if (!sessionId) return;
    try {
        const sessionFiles = shared.value || route.path.startsWith('/share/')
            ? await getSharedSessionFiles(sessionId)
            : await getSessionFiles(sessionId);
        load.assertCurrent();
        const filesById = new Map(
            [...relatedFiles.value, ...sessionFiles].map((file) => [file.file_id, file]),
        );
        relatedFiles.value = Array.from(filesById.values());
    } catch (error) {
        if (load.isCurrent()) console.warn('Failed to load related Markdown files:', error);
    }
};

const rewriteRelativeResources = async (html: string, file: FileInfo, load: PreviewLoad) => {
    const document = new DOMParser().parseFromString(html, 'text/html');
    const elements = Array.from(document.body.querySelectorAll<HTMLElement>('[src], [href]'));
    const relativeElements = elements.filter((element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        return isRelativeResourceUrl(element.getAttribute(attribute) || '');
    });

    const hasMissingFile = relativeElements.some((element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        return !findRelatedFile(
            file,
            relatedFiles.value,
            element.getAttribute(attribute) || '',
        );
    });
    if (hasMissingFile) await ensureRelatedFiles(load, true);
    load.assertCurrent();

    await Promise.all(relativeElements.map(async (element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        const value = element.getAttribute(attribute) || '';
        const relatedFile = findRelatedFile(file, relatedFiles.value, value);
        if (!relatedFile) return;

        const { suffix } = splitResourceUrl(value);
        const bytes = await loadPluginBytes(file, props.plugin, load.signal, relatedFile);
        load.assertCurrent();
        const url = URL.createObjectURL(new Blob([bytes], { type: relatedFile.content_type || 'application/octet-stream' }));
        load.onDispose(() => URL.revokeObjectURL(url));
        element.setAttribute(attribute, `${url}${suffix}`);
    }));

    return DOMPurify.sanitize(document.body.innerHTML);
};

const loadMarkdown = async (file: FileInfo) => {
    const load = loads.begin();
    renderedContent.value = '';

    try {
        const content = await loadPluginText(file, props.plugin, load.signal);
        load.assertCurrent();
        await ensureRelatedFiles(load);
        if (!load.isCurrent()) return;

        const html = DOMPurify.sanitize(marked.parse(content) as string);
        const rewritten = await rewriteRelativeResources(html, file, load);
        if (!load.isCurrent()) return;
        renderedContent.value = rewritten;
    } catch (error) {
        if (!load.isCurrent()) return;
        console.error('Failed to render markdown:', error);
        renderedContent.value = '<pre class="text-sm text-red-500">Failed to render markdown content</pre>';
    }
};

watch(() => props.file, loadMarkdown, { immediate: true, deep: false });
</script>
