<template>
  <div class="mt-4 flex flex-wrap items-center justify-end gap-2">
    <button class="pager-button" :disabled="normalizedPage === 1" @click="change(normalizedPage - 1)">上一页</button>
    <span class="text-xs text-[var(--text-tertiary)]">{{ normalizedPage }} / {{ totalPages }}</span>
    <button class="pager-button" :disabled="normalizedPage >= totalPages" @click="change(normalizedPage + 1)">下一页</button>
    <span class="ml-1 text-xs text-[var(--text-tertiary)]">共 {{ total }} 条</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ page: number; pageSize: number; total: number }>();
const emit = defineEmits<{ change: [page: number] }>();

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)));
const normalizedPage = computed(() => Math.min(Math.max(1, props.page), totalPages.value));

function change(page: number) {
  const next = Math.min(Math.max(1, page), totalPages.value);
  if (next !== props.page) emit('change', next);
}
</script>

<style scoped>
.pager-button {
  @apply rounded-lg border border-[var(--border-main)] px-2.5 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-40;
}
</style>
