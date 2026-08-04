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
import { getFileDownloadUrl } from '../../api/file';
import { getSessionFiles, getSharedSessionFiles } from '../../api/agent';
import { useFilePanel } from '../../composables/useFilePanel';
import { useSessionFileList } from '../../composables/useSessionFileList';
import {
    findRelatedFile,
    isRelativeResourceUrl,
    splitResourceUrl,
} from '../../utils/relativeFileResources';

const renderedContent = ref('');

const props = defineProps<{
    file: FileInfo;
}>();
const route = useRoute();
const { relatedFiles } = useFilePanel();
const { shared } = useSessionFileList();
let loadVersion = 0;

// Configure marked options
marked.setOptions({
    breaks: true,
    gfm: true,
});

const ensureRelatedFiles = async (force = false) => {
    if (!force && relatedFiles.value.length > 1) return;
    const sessionId = route.params.sessionId as string;
    if (!sessionId) return;
    try {
        const sessionFiles = shared.value || route.path.startsWith('/share/')
            ? await getSharedSessionFiles(sessionId)
            : await getSessionFiles(sessionId);
        const filesById = new Map(
            [...relatedFiles.value, ...sessionFiles].map((file) => [file.file_id, file]),
        );
        relatedFiles.value = Array.from(filesById.values());
    } catch (error) {
        console.warn('Failed to load related Markdown files:', error);
    }
};

const rewriteRelativeResources = async (html: string) => {
    const document = new DOMParser().parseFromString(html, 'text/html');
    const elements = Array.from(document.body.querySelectorAll<HTMLElement>('[src], [href]'));
    const relativeElements = elements.filter((element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        return isRelativeResourceUrl(element.getAttribute(attribute) || '');
    });

    const hasMissingFile = relativeElements.some((element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        return !findRelatedFile(
            props.file,
            relatedFiles.value,
            element.getAttribute(attribute) || '',
        );
    });
    if (hasMissingFile) await ensureRelatedFiles(true);

    await Promise.all(relativeElements.map(async (element) => {
        const attribute = element.hasAttribute('src') ? 'src' : 'href';
        const value = element.getAttribute(attribute) || '';
        const relatedFile = findRelatedFile(props.file, relatedFiles.value, value);
        if (!relatedFile) return;

        const { suffix } = splitResourceUrl(value);
        element.setAttribute(attribute, `${await getFileDownloadUrl(relatedFile)}${suffix}`);
    }));

    return DOMPurify.sanitize(document.body.innerHTML);
};

const loadMarkdown = async () => {
    const currentVersion = ++loadVersion;
    renderedContent.value = '';

    try {
        const url = await getFileDownloadUrl(props.file);
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const content = await response.text();
        await ensureRelatedFiles();
        if (currentVersion !== loadVersion) return;

        const html = DOMPurify.sanitize(marked.parse(content) as string);
        const rewritten = await rewriteRelativeResources(html);
        if (currentVersion !== loadVersion) return;
        renderedContent.value = rewritten;
    } catch (error) {
        console.error('Failed to render markdown:', error);
        renderedContent.value = '<pre class="text-sm text-red-500">Failed to render markdown content</pre>';
    }
};

watch(() => props.file, loadMarkdown, { immediate: true, deep: false });
</script>
