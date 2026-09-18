<template>
  <section class="rounded-lg border border-[var(--border-main)] bg-[var(--background-white-main)] px-3 py-2.5 text-sm" role="status" aria-live="polite" aria-label="分析完成情况">
    <div class="font-medium" :class="analysisOutcomeIsComplete(outcome) ? 'text-[#247357]' : 'text-[var(--text-primary)]'">
      {{ analysisOutcomeTitle(outcome) }}
    </div>
    <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">{{ analysisOutcomeReason(outcome, hasDeliveredFiles) }}</p>
    <p v-if="outcome.missing.length" class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">待完成：{{ analysisOutcomeMissing(outcome).join('、') }}</p>
    <div v-if="issues.length" class="mt-3 rounded-md border border-amber-300/60 bg-amber-50/60 px-3 py-2 dark:border-amber-800/60 dark:bg-amber-950/20">
      <div class="text-xs font-medium text-[var(--text-primary)]">{{ issues.some((issue) => issue.blocking) ? '文件交付问题' : '附加文件问题' }}</div>
      <p v-if="analysisOutcomeIsComplete(outcome)" class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">以下文件未计入本次交付。<template v-if="hasDeliveredFiles">本次已交付文件以附件为准。</template></p>
      <ul class="mt-2 space-y-2 text-xs leading-5">
        <li v-for="(issue, index) in issues" :key="`${issue.artifact_name}-${issue.reason_code}-${index}`" class="min-w-0">
          <div class="flex flex-wrap items-baseline gap-x-2">
            <span class="break-all font-medium text-[var(--text-primary)]">{{ issue.artifact_name }}</span>
            <span class="text-[var(--text-tertiary)]">{{ issue.kind_label }} · {{ issue.blocking ? '影响所需成果' : '附加文件' }}</span>
          </div>
          <p class="text-[var(--text-secondary)]">{{ issue.reason }}</p>
        </li>
      </ul>
    </div>
    <button
      v-if="allowResume && outcome.can_resume && outcome.resume_from"
      type="button"
      class="mt-2 rounded-md border border-[#247357]/30 px-3 py-1.5 text-xs font-medium text-[#247357] hover:bg-[#e8f3ee] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#247357]"
      @click="$emit('resume')"
    >继续未完成部分</button>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { AnalysisOutcome } from '../types/analysisOutcome';
import { analysisOutcomeIsComplete, analysisOutcomeIssues, analysisOutcomeMissing, analysisOutcomeReason, analysisOutcomeTitle } from '../utils/analysisOutcome';

const props = defineProps<{ outcome: AnalysisOutcome; allowResume?: boolean; hasDeliveredFiles?: boolean }>();
const issues = computed(() => analysisOutcomeIssues(props.outcome));
defineEmits<{ (event: 'resume'): void }>();
</script>
