<template>
  <div class="flex min-h-0 flex-1 flex-col bg-[var(--background-gray-main)]">
    <div class="flex shrink-0 items-center justify-between border-b border-[var(--border-main)] px-4 py-2 text-xs text-[var(--text-tertiary)]">
      <span class="font-medium text-[var(--text-secondary)]">CSV 在线预览</span>
      <span>第 {{ pageIndex + 1 }} 页 · {{ rows.length }} 行{{ page?.columns_truncated ? '（最多显示 50 列）' : '' }}</span>
      <div class="flex gap-3">
        <button :disabled="loading || pageIndex === 0" class="disabled:opacity-40" @click="loadPage(pageIndex - 1)">上一页</button>
        <button :disabled="loading || page?.next_offset == null" class="disabled:opacity-40" @click="loadPage(pageIndex + 1)">下一页</button>
      </div>
    </div>
    <div v-if="status" class="flex min-h-0 flex-1 items-center justify-center p-4">
      <div class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-sm text-[var(--text-secondary)]">{{ status }}</div>
    </div>
    <div v-else class="min-h-0 flex-1 overflow-auto bg-[var(--background-menu-white)] p-3">
      <table class="min-w-full border-collapse text-left text-xs">
        <thead><tr><th v-for="(header, index) in headers" :key="index" class="sticky top-0 whitespace-nowrap border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 py-1.5 font-medium">{{ header || `列 ${index + 1}` }}</th></tr></thead>
        <tbody><tr v-for="(row, rowIndex) in rows" :key="rowIndex" class="hover:bg-[var(--background-gray-main)]"><td v-for="(value, columnIndex) in row" :key="columnIndex" class="max-w-[280px] whitespace-nowrap border border-[var(--border-main)] px-2 py-1.5" :title="value">{{ value }}</td></tr></tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { FileInfo } from '../../api/file';
import { useFilePreviewPages } from '../../composables/useFilePreviewPages';

import type { VisualizationPlugin } from '../../visualizations/contract';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const { page, headers, loading, error, pageIndex, loadPage } = useFilePreviewPages(() => props.file, () => props.plugin);
const rows = computed(() => (page.value?.rows || []).map(row => row.concat(Array(Math.max(0, headers.value.length - row.length)).fill(''))));
const status = computed(() => loading.value ? '正在加载 CSV...' : error.value);
</script>
