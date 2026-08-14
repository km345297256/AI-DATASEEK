<template>
  <section class="my-1.5" aria-label="任务耗时信息">
    <button
      type="button"
      class="flex items-center gap-1.5 text-xs text-[var(--text-tertiary)]"
      :class="content.has_steps ? 'cursor-pointer hover:text-[var(--text-secondary)]' : 'cursor-default'"
      :disabled="!content.has_steps"
      :aria-expanded="content.has_steps ? expanded : undefined"
      @click="content.has_steps && $emit('toggle')"
    >
      <Clock3 class="size-3.5" />
      <span>思考了 {{ formatDuration(content.duration_ms) }}</span>
      <ChevronRight v-if="content.has_steps" class="size-3.5 transition-transform" :class="expanded ? 'rotate-90' : ''" />
    </button>
  </section>
</template>

<script setup lang="ts">
import { ChevronRight, Clock3 } from 'lucide-vue-next';
import type { TaskSummaryContent } from '@/types/message';

defineProps<{ content: TaskSummaryContent; expanded?: boolean }>();
defineEmits<{ (event: 'toggle'): void }>();

function formatDuration(durationMs: number): string {
  const seconds = durationMs / 1000;
  return `${seconds.toFixed(seconds >= 10 ? 0 : 1)} 秒`;
}
</script>
