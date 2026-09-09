<template>
    <section class="flex min-h-0 flex-1 flex-col text-left">
        <div class="flex shrink-0 items-center justify-between border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
            <span>文本分页预览 · 第 {{ pageIndex + 1 }} 页（每页最多 64 KiB）</span>
            <div class="flex gap-3">
                <button :disabled="loading || pageIndex === 0" class="disabled:opacity-40" @click="loadPage(pageIndex - 1)">上一页</button>
                <button :disabled="loading || page?.next_offset == null" class="disabled:opacity-40" @click="loadPage(pageIndex + 1)">下一页</button>
            </div>
        </div>
        <div v-if="loading || error" class="p-4 text-sm text-[var(--text-secondary)]">{{ loading ? '正在加载文本...' : error }}</div>
        <div v-else class="min-h-0 flex-1" data-keybinding-context="9" data-mode-id="c"
            style="width: 100%; --vscode-editorCodeLens-lineHeight: 15px; --vscode-editorCodeLens-fontSize: 10px; --vscode-editorCodeLens-fontFeatureSettings: 'liga' off, 'calt' off;">

          <MonacoEditor
            :value="page?.text || ''"
            :filename="file.filename"
            :read-only="true"
            theme="vs"
            :line-numbers="'off'"
            :word-wrap="'on'"
            :minimap="false"
            :scroll-beyond-last-line="false"
            :automatic-layout="true"
          />
        </div>
    </section>
</template>

<script setup lang="ts">
import MonacoEditor from '@/components/ui/MonacoEditor.vue';
import type { FileInfo } from '../../api/file';
import { useFilePreviewPages } from '../../composables/useFilePreviewPages';
import type { VisualizationPlugin } from '../../visualizations/contract';

const props = defineProps<{
  file: FileInfo;
  plugin: VisualizationPlugin;
}>();

const { page, loading, error, pageIndex, loadPage } = useFilePreviewPages(() => props.file, () => props.plugin);
</script>
