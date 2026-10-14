<template>
  <section class="space-y-3 text-sm" aria-label="任务执行记录">
    <div class="flex items-center justify-between">
      <h3 class="font-medium">执行记录</h3>
      <button type="button" :disabled="loading" class="text-xs disabled:opacity-50" @click="$emit('refresh')">刷新</button>
    </div>
    <p v-if="error" role="alert" class="text-xs text-red-600">{{ error }}</p>
    <p v-if="loading" role="status" class="text-xs text-[var(--text-secondary)]">正在读取任务记录…</p>
    <p v-else-if="!runs.length && !error" class="text-xs text-[var(--text-secondary)]">暂无执行记录。请求未进入执行或旧版对话可能没有记录。</p>
    <article v-for="run in runs" :key="run.run_id" class="rounded-lg border border-[var(--border-main)] p-3">
      <div class="flex flex-wrap justify-between gap-2"><span>{{ formatTime(run.started_at) }}</span><span>{{ runStatusLabels[run.status] }}</span></div>
      <p class="mt-1 text-xs text-[var(--text-secondary)]">{{ run.steps.filter(step => step.status === 'succeeded').length }} / {{ run.steps.length }} 个阶段成功</p>
      <p v-if="!run.history_complete" class="mt-2 text-xs text-amber-700">记录尚不完整，不能确认整体状态。</p>
      <ul v-if="run.steps.length" class="mt-2 space-y-1 text-xs">
        <li v-for="step in run.steps" :key="step.step_id">阶段 {{ step.ordinal }} · {{ stepLabels[step.status] }}<span v-if="step.outcome_status && step.status !== 'succeeded'"> · {{ step.outcome_status }}</span></li>
      </ul>
      <details class="mt-2 text-xs text-[var(--text-secondary)]">
        <summary class="cursor-pointer">技能版本与执行详情</summary>
        <p class="mt-2 break-all">记录编号：{{ run.run_id }}</p>
        <p v-if="!run.environment_fingerprint">执行环境尚未登记。</p>
        <p v-else class="break-all">执行环境：{{ run.environment_fingerprint }}</p>
        <ul class="mt-1 space-y-1"><li v-for="skill in run.skills" :key="skill.package_id + skill.version" class="break-all">{{ skill.package_id }} · v{{ skill.version }}<br />SHA-256：{{ skill.sha256 }}</li></ul>
        <p v-if="run.legacy_skill_count">另有 {{ run.legacy_skill_count }} 个旧格式技能，未声明发布版本。</p>
        <p v-else-if="run.environment_fingerprint && !run.skills.length">本次未启用技能包。</p>
        <p class="mt-2">记录供追溯使用，暂不支持从中恢复执行。</p>
      </details>
    </article>
    <button v-if="hasMore" type="button" :disabled="loading" class="text-xs disabled:opacity-50" @click="$emit('more')">加载更早的执行记录</button>
  </section>
</template>
<script setup lang="ts">
import type { DataRun } from '@/types/dataRun';
import { runStatusLabels } from '@/utils/taskProgress';
defineProps<{ runs: DataRun[]; loading: boolean; error: string; hasMore: boolean }>();
defineEmits<{ (event: 'refresh'): void; (event: 'more'): void }>();
const stepLabels = { pending: '待执行', running: '执行中', succeeded: '成功', failed: '失败', unknown: '待确认' };
const formatTime = (value: number) => new Date(value * 1000).toLocaleString();
</script>
