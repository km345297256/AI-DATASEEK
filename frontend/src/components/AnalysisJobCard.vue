<template>
  <section class="m-3 mb-0 shrink-0 rounded-xl border border-[var(--border-light)] bg-[var(--background-white-main)] p-3 text-sm" aria-live="polite">
    <div class="flex items-center justify-between gap-3">
      <div class="flex items-center gap-2 font-medium">
        <Loader2 v-if="!terminal" class="size-4 animate-spin text-[var(--text-tertiary)]" />
        <CheckCircle2 v-else-if="job.status === 'succeeded'" class="size-4 text-emerald-600" />
        <CircleAlert v-else class="size-4 text-amber-600" />
        <span>{{ t('Analysis job') }}</span>
      </div>
      <span class="text-xs text-[var(--text-secondary)]">{{ t(labels[job.status]) }}</span>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--text-tertiary)]">
      <span>{{ t('Job elapsed') }} {{ elapsed }}s</span>
      <span v-if="job.timeout_seconds">{{ t('Job time limit') }} {{ job.timeout_seconds }}s</span>
    </div>
    <p v-if="job.status === 'interrupted'" class="mt-2 text-xs text-[var(--text-secondary)]">{{ t('Job interrupted explanation') }}</p>
    <p v-if="error" class="mt-2 text-xs text-amber-700">{{ error }}</p>
    <button v-if="sessionId && !isShare && job.cancellable && !terminal && job.status !== 'cancelling'"
      type="button" :disabled="cancelling" @click="cancel"
      class="mt-2 rounded-lg border border-[var(--border-light)] px-2.5 py-1 text-xs disabled:opacity-50 hover:bg-black/5">
      {{ t(cancelling ? 'Job cancelling' : 'Cancel analysis job') }}
    </button>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { CheckCircle2, CircleAlert, Loader2 } from 'lucide-vue-next';
import { useI18n } from 'vue-i18n';
import type { AnalysisJobStatus, AnalysisJobView } from '@/types/analysisJob';
import { getAnalysisJob, cancelAnalysisJob } from '@/api/analysisJob';
import { analysisJobElapsedSeconds, isAnalysisJobTerminal, newerAnalysisJob } from '@/utils/analysisJob';

const props = defineProps<{ initial: AnalysisJobView; sessionId?: string; isShare?: boolean }>();
const { t } = useI18n();
const job = ref<AnalysisJobView>(props.initial);
const error = ref('');
const cancelling = ref(false);
const now = ref(Date.now());
const terminal = computed(() => isAnalysisJobTerminal(job.value));
const elapsed = computed(() => analysisJobElapsedSeconds(job.value, now.value));
const labels: Record<AnalysisJobStatus, string> = {
  queued: 'Job queued', running: 'Job running', cancelling: 'Job cancelling',
  succeeded: 'Job succeeded', failed: 'Job failed', cancelled: 'Job cancelled',
  timed_out: 'Job timed out', interrupted: 'Job interrupted',
};
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let request: AbortController | undefined;
let disposed = false;

function stop() {
  if (timer) clearTimeout(timer);
  timer = undefined;
  request?.abort();
  request = undefined;
}

async function refresh(epoch: number) {
  if (disposed || epoch !== generation || terminal.value || !props.sessionId || props.isShare) return;
  const sessionId = props.sessionId;
  const jobId = job.value.job_id;
  request = new AbortController();
  try {
    const latest = await getAnalysisJob(sessionId, jobId, request.signal);
    if (epoch !== generation || disposed) return;
    job.value = newerAnalysisJob(job.value, latest)!;
    error.value = '';
  } catch {
    if (epoch !== generation || disposed || terminal.value) return;
    error.value = t('Job status unavailable');
  } finally {
    if (epoch === generation && !disposed && !terminal.value) {
      now.value = Date.now();
      timer = setTimeout(() => refresh(epoch), 2500);
    }
  }
}

watch(() => `${props.sessionId || ''}:${props.initial.job_id}:${!!props.isShare}`, () => {
  stop();
  generation++;
  job.value = props.initial;
  error.value = '';
  cancelling.value = false;
  now.value = Date.now();
  void refresh(generation);
}, { immediate: true });

watch(() => props.initial, (incoming) => {
  job.value = newerAnalysisJob(job.value, incoming)!;
  now.value = Date.now();
  if (terminal.value) stop();
}, { deep: true });

async function cancel() {
  if (!props.sessionId || props.isShare || terminal.value || cancelling.value) return;
  const epoch = generation;
  cancelling.value = true;
  try {
    const latest = await cancelAnalysisJob(props.sessionId, job.value.job_id);
    if (epoch !== generation || disposed) return;
    job.value = newerAnalysisJob(job.value, latest)!;
    error.value = '';
  } catch {
    if (epoch === generation && !disposed) error.value = t('Job cancellation failed');
  } finally {
    if (epoch === generation && !disposed) cancelling.value = false;
  }
}

onBeforeUnmount(() => { disposed = true; generation++; stop(); });
</script>
