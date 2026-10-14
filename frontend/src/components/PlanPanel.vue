<template>
  <section class="mb-2 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]" aria-label="任务进度">
    <button type="button" class="flex w-full items-center gap-2 px-4 py-2 text-left" :aria-expanded="isExpanded" aria-label="查看任务进度与执行记录" @click="isExpanded = !isExpanded">
      <StepSuccessIcon v-if="completed" />
      <CircleX v-else-if="failed" class="shrink-0 text-[var(--function-error)]" :size="16" />
      <Clock v-else class="shrink-0 text-[var(--icon-secondary)]" :size="16" />
      <span class="min-w-0 flex-1 truncate text-sm text-[var(--text-secondary)]">{{ currentLabel }}</span>
      <span v-if="plan?.steps.length" class="text-xs text-[var(--text-tertiary)]">{{ planProgress }}</span>
      <ChevronDown v-if="isExpanded" :size="16" /><ChevronUp v-else :size="16" />
    </button>
    <div v-if="isExpanded" class="max-h-[45vh] space-y-4 overflow-y-auto border-t border-[var(--border-main)] p-4">
      <div v-if="plan?.steps.length">
        <h3 class="mb-2 text-sm font-medium">{{ $t('Task Progress') }}</h3>
        <ol class="space-y-2">
          <li v-for="step in plan.steps" :key="step.id" class="flex items-start gap-2 text-sm">
            <StepSuccessIcon v-if="step.status === 'completed'" />
            <CircleX v-else-if="step.status === 'failed'" class="shrink-0 text-[var(--function-error)]" :size="16" />
            <Clock v-else class="shrink-0" :size="16" />
            <span class="min-w-0 break-words">{{ step.description }}</span>
          </li>
        </ol>
      </div>
      <DataRunHistory v-if="sessionId" :runs="runs" :loading="loading" :error="error" :has-more="nextBefore !== null" @refresh="load(false)" @more="load(true)" />
    </div>
  </section>
</template>
<script setup lang="ts">
import { ref, computed, toRef } from 'vue';
import { ChevronUp, ChevronDown, Clock, CircleX } from 'lucide-vue-next';
import StepSuccessIcon from './icons/StepSuccessIcon.vue';
import DataRunHistory from './DataRunHistory.vue';
import { useDataRuns } from '@/composables/useDataRuns';
import { taskRunLabel } from '@/utils/taskProgress';
import type { PlanEventData } from '../types/event';
const props = defineProps<{ plan?: PlanEventData; sessionId?: string; revision?: number; isRunning?: boolean }>();
const isExpanded = ref(false);
const { runs, nextBefore, loading, error, load } = useDataRuns(toRef(props, 'sessionId'), toRef(props, 'revision'));
const latest = computed(() => runs.value[0]);
const planProgress = computed(() => `${props.plan?.steps.filter(step => ['completed', 'failed'].includes(step.status)).length ?? 0} / ${props.plan?.steps.length ?? 0}`);
// A finished step/plan does not establish overall success; the run does.
const completed = computed(() => !props.isRunning && !error.value && latest.value?.history_complete && latest.value.status === 'succeeded');
const failed = computed(() => !props.isRunning && !error.value && latest.value?.status === 'failed');
const currentLabel = computed(() => props.sessionId
  ? taskRunLabel(latest.value, Boolean(props.isRunning), error.value)
  : props.plan?.steps.find(step => ['running', 'pending'].includes(step.status))?.description || '阶段记录');
</script>
