<template>
  <section class="my-2 border-y border-[var(--border-main)] py-2.5" aria-label="任务耗时信息">
    <div class="flex items-center justify-between gap-3">
      <div class="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
        <Clock3 class="size-4 text-[#2b7659]" />
        任务耗时
      </div>
      <span class="text-xs font-medium tabular-nums text-[#286d52]">{{ content.duration_ms }} ms</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Clock3 } from 'lucide-vue-next';
import type { TaskSummaryContent } from '@/types/message';

defineProps<{ content: TaskSummaryContent }>();
</script>
