<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-[var(--text-secondary)]">HTML 预览</span>
        <span v-if="resolvedCount">已解析 {{ resolvedCount }} 个相对资源</span>
        <span v-if="missingResources.length" class="text-[var(--function-warning,#b7791f)]">未找到 {{ missingResources.length }} 个资源</span>
      </div>
      <span>iframe sandbox 隔离渲染</span>
    </div>

    <div v-if="status" class="flex min-h-0 flex-1 items-center justify-center p-4">
      <div class="max-w-[520px] rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm text-[var(--text-secondary)]">
        {{ status }}
      </div>
    </div>

    <div v-else class="min-h-0 flex-1 overflow-hidden bg-white">
      <iframe
        class="h-full w-full border-0 bg-white"
        sandbox="allow-scripts allow-forms allow-popups"
        referrerpolicy="no-referrer"
        :srcdoc="previewHtml"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { getFileDownloadUrl, type FileInfo } from '../../api/file';
import { getSessionFiles, getSharedSessionFiles } from '../../api/agent';
import { useFilePanel } from '../../composables/useFilePanel';
import { useSessionFileList } from '../../composables/useSessionFileList';
import {
  findRelatedFile,
  isRelativeResourceUrl,
  splitResourceUrl,
} from '../../utils/relativeFileResources';

const props = defineProps<{
  file: FileInfo;
}>();

const { relatedFiles } = useFilePanel();
const { shared } = useSessionFileList();
const route = useRoute();
const status = ref('');
const previewHtml = ref('');
const missingResources = ref<string[]>([]);
const objectUrls = ref<string[]>([]);
let loadVersion = 0;

const resolvedCount = computed(() => objectUrls.value.length);

const revokeObjectUrls = () => {
  objectUrls.value.forEach((url) => URL.revokeObjectURL(url));
  objectUrls.value = [];
};

onBeforeUnmount(revokeObjectUrls);

const createBlobUrl = async (file: FileInfo) => {
  const url = await getFileDownloadUrl(file);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  objectUrls.value.push(objectUrl);
  return objectUrl;
};

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
    console.warn('Failed to load related HTML files:', error);
  }
};

const rewriteHtmlResources = async (html: string) => {
  const replacements = new Map<string, string>();
  const missing = new Set<string>();
  const attrPattern = /\b(src|href|poster)\s*=\s*(["'])(.*?)\2/gi;
  const cssUrlPattern = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
  const urls = new Set<string>();

  for (const match of html.matchAll(attrPattern)) {
    if (isRelativeResourceUrl(match[3])) urls.add(match[3]);
  }
  for (const match of html.matchAll(cssUrlPattern)) {
    if (isRelativeResourceUrl(match[2])) urls.add(match[2]);
  }

  if (Array.from(urls).some((url) => !findRelatedFile(props.file, relatedFiles.value, url))) {
    await ensureRelatedFiles(true);
  }

  await Promise.all(Array.from(urls).map(async (url) => {
    const related = findRelatedFile(props.file, relatedFiles.value, url);
    if (!related) {
      missing.add(url);
      return;
    }
    const { suffix } = splitResourceUrl(url);
    replacements.set(url, `${await createBlobUrl(related)}${suffix}`);
  }));

  missingResources.value = Array.from(missing);
  const rewritten = html
    .replace(attrPattern, (full, attr: string, quote: string, value: string) => {
      return replacements.has(value) ? `${attr}=${quote}${replacements.get(value)}${quote}` : full;
    })
    .replace(cssUrlPattern, (full, quote: string, value: string) => {
      return replacements.has(value) ? `url(${quote}${replacements.get(value)}${quote})` : full;
    });
  return injectPreviewViewportStyle(rewritten);
};

const injectPreviewViewportStyle = (html: string) => {
  const style = `
<style data-fairstack-html-preview>
  html, body {
    min-width: 0 !important;
    max-width: none !important;
    min-height: 100% !important;
    overflow: auto !important;
  }
  body {
    box-sizing: border-box !important;
  }
  img, video, canvas, svg {
    max-width: 100% !important;
    height: auto;
  }
  table {
    max-width: 100%;
  }
  pre, code {
    white-space: pre-wrap;
    word-break: break-word;
  }
  * {
    box-sizing: border-box;
  }
</style>`;
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head([^>]*)>/i, `<head$1>${style}`);
  }
  return `${style}${html}`;
};

const loadHtml = async () => {
  const currentVersion = ++loadVersion;
  status.value = '正在加载 HTML...';
  previewHtml.value = '';
  missingResources.value = [];
  revokeObjectUrls();

  try {
    const url = await getFileDownloadUrl(props.file);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const html = await response.text();
    await ensureRelatedFiles();
    if (currentVersion !== loadVersion) return;
    const rewritten = await rewriteHtmlResources(html);
    if (currentVersion !== loadVersion) return;
    previewHtml.value = rewritten;
    status.value = '';
  } catch (error) {
    console.error('Failed to render HTML file:', error);
    status.value = 'HTML 预览失败。请确认文件可访问，且关联资源已同步到当前任务文件列表。';
  }
};

watch(() => props.file, loadHtml, { immediate: true });
</script>
