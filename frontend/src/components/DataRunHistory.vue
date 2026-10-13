<template>
  <section class="my-2 rounded-xl border border-[var(--border-main)] bg-[var(--background-white-main)] text-sm">
    <button type="button" class="flex w-full items-center justify-between px-4 py-3" :aria-expanded="open" @click="open = !open">
      <span class="font-medium">实验运行</span><span class="text-xs text-[var(--text-secondary)]">{{ open ? '收起' : '查看阶段与技能版本' }}</span>
    </button>
    <div v-if="open" class="space-y-3 border-t border-[var(--border-main)] px-4 py-3">
      <div class="flex justify-between text-xs text-[var(--text-secondary)]">
        <span>每次新任务独立记录；恢复执行暂需检查后重新发起。</span>
        <button type="button" :disabled="loading" class="shrink-0 px-2 disabled:opacity-50" @click="load(false)">刷新</button>
      </div>
      <p v-if="error" role="alert" class="text-xs text-red-600">{{ error }}</p>
      <p v-if="loading" role="status" class="text-xs text-[var(--text-secondary)]">正在读取运行记录…</p>
      <p v-else-if="!runs.length && !error" class="text-xs text-[var(--text-secondary)]">暂无运行记录。升级后新发起的分析会记录在这里；旧对话不会补造运行记录。</p>
      <article v-for="run in runs" :key="run.run_id" class="rounded-lg border border-[var(--border-main)] p-3">
        <div class="flex flex-wrap justify-between gap-2"><span>运行 {{ run.run_id.slice(0, 8) }}</span><span>{{ labels[run.status] }}</span></div>
        <p class="mt-1 text-xs text-[var(--text-secondary)]">{{ formatTime(run.started_at) }} · {{ run.steps.filter(step => step.status === 'succeeded').length }} / {{ run.steps.length }} 个阶段成功</p>
        <p v-if="!run.history_complete" class="mt-2 text-xs text-amber-700">记录超过本次读取上限，尚不能确认完整状态。</p>
        <ul v-if="run.steps.length" class="mt-2 flex flex-wrap gap-2 text-xs"><li v-for="step in run.steps" :key="step.step_id" class="rounded bg-[var(--fill-tsp-gray-main)] px-2 py-1">阶段 {{ step.ordinal }} · {{ stepLabels[step.status] }}</li></ul>
        <div class="mt-2 text-xs text-[var(--text-secondary)]">
          <p v-if="!run.environment_fingerprint">执行环境尚未登记。</p>
          <p v-else>执行环境已记录 · {{ run.environment_fingerprint.slice(0, 12) }}</p>
          <ul class="mt-1 space-y-1"><li v-for="skill in run.skills" :key="skill.package_id + skill.version" class="break-all">{{ skill.package_id }} @ {{ skill.version }} · {{ skill.sha256.slice(0, 12) }}</li></ul>
          <p v-if="run.legacy_skill_count">另有 {{ run.legacy_skill_count }} 个旧格式技能，未声明发布版本。</p>
          <p v-else-if="run.environment_fingerprint && !run.skills.length">本次未启用技能包。</p>
        </div>
      </article>
      <button v-if="nextBefore !== null" type="button" :disabled="loading" class="text-xs disabled:opacity-50" @click="load(true)">加载更早的运行</button>
    </div>
  </section>
</template>
<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import { getDataRuns } from '../api/agent';
import type { DataRun } from '../types/dataRun';
const props = defineProps<{ sessionId: string; revision?: number }>();
const open = ref(false);
const runs = ref<DataRun[]>([]);
const nextBefore = ref<number | null>(null);
const loading = ref(false);
const error = ref('');
const labels: Record<DataRun['status'], string> = { running: '运行中（最后记录）', waiting: '等待输入', succeeded: '运行成功', partial: '部分完成', failed: '运行失败', unknown: '结果待确认' };
const stepLabels = { pending: '待执行', running: '执行中', succeeded: '成功', failed: '失败', unknown: '待确认' };
const formatTime = (value: number) => new Date(value * 1000).toLocaleString();
let request: AbortController | undefined;
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
async function load(append = false) {
  if (!props.sessionId) return;
  request?.abort();
  const controller = new AbortController();
  request = controller;
  const current = ++generation;
  loading.value = true;
  error.value = '';
  try {
    const page = await getDataRuns(props.sessionId, append ? nextBefore.value ?? undefined : undefined, controller.signal);
    if (current !== generation) return;
    const combined = append ? [...runs.value, ...page.runs] : page.runs;
    runs.value = [...new Map(combined.map(run => [run.run_id, run])).values()];
    nextBefore.value = page.next_before_seq;
  } catch {
    if (current === generation && !controller.signal.aborted) error.value = '运行记录暂时无法读取，请点击刷新重试。';
  } finally {
    if (current === generation) loading.value = false;
  }
}
watch([() => props.sessionId, open], () => {
  ++generation;
  request?.abort();
  clearTimeout(timer);
  timer = undefined;
  runs.value = [];
  nextBefore.value = null;
  loading.value = false;
  error.value = '';
  if (open.value) void load();
});
watch(() => props.revision, () => {
  if (!open.value || timer) return;
  timer = setTimeout(() => { timer = undefined; void load(); }, 1500);
});
onUnmounted(() => { ++generation; request?.abort(); clearTimeout(timer); });
</script>
