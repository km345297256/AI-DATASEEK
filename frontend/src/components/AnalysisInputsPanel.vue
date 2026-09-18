<template>
  <section class="my-3 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]" aria-label="本次分析资料">
    <header class="flex items-center justify-between gap-2 px-3 py-2.5">
      <button type="button" class="flex min-w-0 items-center gap-2 text-sm font-medium" :aria-expanded="expanded" @click="expanded = !expanded">
        <ChevronDown class="size-4 transition-transform" :class="expanded ? '' : '-rotate-90'" />
        本次分析资料<span v-if="files.length" class="text-xs font-normal text-[var(--text-tertiary)]">已选 {{ selectedIds?.length || 0 }} / {{ files.length }}</span>
      </button>
      <span v-if="running" class="text-xs text-[var(--text-tertiary)]">本轮资料已锁定</span>
    </header>
    <div v-if="expanded" class="border-t border-[var(--border-main)] px-3 py-2">
      <p v-if="loading" role="status" class="py-2 text-xs text-[var(--text-tertiary)]">正在读取会话资料…</p>
      <p v-else-if="error" role="alert" class="py-2 text-xs text-amber-700">{{ error }} <button type="button" class="underline" @click="$emit('refresh')">重新加载</button></p>
      <template v-else>
        <p v-if="!files.length" class="py-2 text-xs text-[var(--text-tertiary)]">可直接提问，或通过下方输入框上传数据。上传资料不会自动加入数据集管理。</p>
        <template v-else>
          <p class="mb-2 text-[11px] leading-5 text-[var(--text-tertiary)]">勾选下一次分析使用的资料；新上传文件会一并加入。取消勾选不会删除文件，生成成果不会自动作为输入。</p>
          <ul class="max-h-56 overflow-y-auto">
            <li v-for="file in files" :key="file.file_id" class="flex min-w-0 items-center gap-2 border-t border-[var(--border-main)] py-2 first:border-0">
              <input type="checkbox" :checked="selectedIds?.includes(file.file_id)" :disabled="running" :aria-label="`分析资料 ${file.filename} (${file.file_id})`" @change="$emit('toggle', file.file_id)" />
              <FileText class="size-4 shrink-0 text-[#2b7659]" />
              <span class="min-w-0 flex-1">
                <span class="block truncate text-xs" :title="file.filename">{{ file.filename }}</span>
                <span class="block text-[10px] text-[var(--text-tertiary)]">{{ file.file_id.slice(-8) }}<template v-if="file.size != null"> · {{ formatSize(file.size) }}</template></span>
              </span>
              <button type="button" class="rounded p-1.5 text-[#2b7659] hover:bg-[var(--fill-tsp-white-light)]" :aria-label="`预览 ${file.filename}`" @click="showFilePanel(file, files)"><Eye class="size-4" /></button>
            </li>
          </ul>
        </template>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { ChevronDown, Eye, FileText } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { useFilePanel } from '../composables/useFilePanel';

defineProps<{ files: FileInfo[]; selectedIds?: string[]; loading: boolean; error: string; running: boolean }>();
defineEmits<{ (event: 'toggle', fileId: string): void; (event: 'refresh'): void }>();
const expanded = ref(true);
const { showFilePanel } = useFilePanel();
function formatSize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}
</script>
