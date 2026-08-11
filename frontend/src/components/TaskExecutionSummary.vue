<template>
  <section class="my-2 border-y border-[var(--border-main)] py-3" aria-label="任务耗时信息">
    <div class="flex items-center justify-between gap-3">
      <div class="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
        <Clock3 class="size-4 text-[#2b7659]" />
        任务耗时
      </div>
      <span class="text-xs font-medium text-[#286d52]">{{ formatDuration(content.duration_seconds) }}</span>
    </div>

    <dl class="mt-3 grid grid-cols-1 gap-2 text-xs text-[var(--text-secondary)] sm:grid-cols-3">
      <div>
        <dt class="text-[11px] text-[var(--text-tertiary)]">开始时间</dt>
        <dd class="mt-0.5 font-medium text-[var(--text-primary)]">{{ formatTimestamp(content.started_at) }}</dd>
      </div>
      <div>
        <dt class="text-[11px] text-[var(--text-tertiary)]">结束时间</dt>
        <dd class="mt-0.5 font-medium text-[var(--text-primary)]">{{ formatTimestamp(content.ended_at) }}</dd>
      </div>
      <div>
        <dt class="text-[11px] text-[var(--text-tertiary)]">执行步骤</dt>
        <dd class="mt-0.5 font-medium text-[var(--text-primary)]">共 {{ content.steps.length }} 个步骤</dd>
      </div>
    </dl>

    <ol v-if="content.steps.length" class="mt-3 divide-y divide-[var(--border-main)] border-t border-[var(--border-main)]">
      <li v-for="(step, index) in content.steps" :key="step.id" class="flex items-start gap-3 py-2 text-xs">
        <span class="flex size-5 shrink-0 items-center justify-center rounded-full bg-[var(--fill-tsp-white-dark)] text-[10px] font-medium text-[var(--text-secondary)]">{{ index + 1 }}</span>
        <span class="min-w-0 flex-1 break-words leading-5 text-[var(--text-secondary)]">{{ step.description }}</span>
        <span class="shrink-0 font-medium text-[var(--text-primary)]">{{ formatDuration(step.duration_seconds) }}</span>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { Clock3 } from 'lucide-vue-next';
import type { TaskSummaryContent } from '@/types/message';
import { DISPLAY_TIME_ZONE } from '@/utils/time';

defineProps<{ content: TaskSummaryContent }>();

const formatTimestamp = (timestamp: number) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: DISPLAY_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
}).format(new Date(timestamp * 1000));

const formatDuration = (seconds: number) => {
  const totalSeconds = Math.max(0, Math.round(seconds));
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  if (minutes < 60) return remainingSeconds ? `${minutes} 分 ${remainingSeconds} 秒` : `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分` : `${hours} 小时`;
};
</script>
