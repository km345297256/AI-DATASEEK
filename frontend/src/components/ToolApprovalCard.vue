<template>
  <section
    class="m-3 mb-0 shrink-0 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-sm"
    aria-live="polite"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-center gap-2 font-medium text-[var(--text-primary)]">
        <ShieldAlert class="size-4 shrink-0 text-amber-600" />
        <span>{{ t('Tool approval required') }}</span>
      </div>
      <span class="shrink-0 rounded-full border border-amber-500/30 px-2 py-0.5 text-xs text-[var(--text-secondary)]">
        {{ t(statusLabels[displayStatus]) }}
      </span>
    </div>

    <p class="mt-2 text-xs leading-5 text-[var(--text-secondary)]">
      {{ t('Review this single tool call before it can continue.') }}
    </p>

    <dl class="mt-3 grid gap-2 rounded-lg border border-[var(--border-light)] bg-[var(--background-white-main)] p-3 text-xs">
      <div class="grid gap-1 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-3">
        <dt class="text-[var(--text-tertiary)]">{{ t('This call') }}</dt>
        <dd class="break-all font-mono text-[var(--text-secondary)]">{{ approval.approval_id }}</dd>
      </div>
      <div class="grid gap-1 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-3">
        <dt class="text-[var(--text-tertiary)]">{{ t('Target tool') }}</dt>
        <dd class="break-words font-mono font-medium text-[var(--text-primary)]">{{ approval.tool_name }}</dd>
      </div>
      <div class="grid gap-1 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-3">
        <dt class="text-[var(--text-tertiary)]">{{ t('Expires') }}</dt>
        <dd class="text-[var(--text-secondary)]">{{ expiresAt }}</dd>
      </div>
    </dl>

    <div class="mt-3">
      <div class="text-xs font-medium text-[var(--text-secondary)]">{{ t('Redacted arguments') }}</div>
      <pre class="mt-1 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-[var(--border-light)] bg-[var(--background-gray-main)] p-2.5 font-mono text-xs leading-5 text-[var(--text-secondary)]">{{ argumentsPreview }}</pre>
    </div>

    <div v-if="approval.effects.length || approval.permissions.length" class="mt-3">
      <div class="text-xs font-medium text-[var(--text-secondary)]">{{ t('Risk and permissions') }}</div>
      <div class="mt-1.5 flex flex-wrap gap-1.5">
        <span
          v-for="effect in approval.effects"
          :key="`effect:${effect}`"
          class="max-w-full break-all rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-800 dark:text-amber-300"
        >
          {{ effect }}
        </span>
        <span
          v-for="permission in approval.permissions"
          :key="`permission:${permission}`"
          class="max-w-full break-all rounded-md border border-[var(--border-light)] px-2 py-1 text-xs text-[var(--text-secondary)]"
        >
          {{ permission }}
        </span>
      </div>
    </div>

    <div v-if="approval.credential_refs.length" class="mt-3">
      <div class="text-xs font-medium text-[var(--text-secondary)]">{{ t('Credential references') }}</div>
      <div class="mt-1.5 flex flex-wrap gap-1.5">
        <code
          v-for="reference in approval.credential_refs"
          :key="reference"
          class="max-w-full break-all rounded-md border border-[var(--border-light)] px-2 py-1 text-xs text-[var(--text-secondary)]"
        >{{ reference }}</code>
      </div>
    </div>

    <p v-if="displayStatus === 'approved'" class="mt-3 text-xs leading-5 text-[var(--text-secondary)]">
      {{ t('Approved once. Waiting for this call to consume the approval.') }}
    </p>
    <p v-else-if="displayStatus === 'expired'" class="mt-3 text-xs leading-5 text-[var(--text-secondary)]">
      {{ t('This approval request has expired.') }}
    </p>
    <p v-if="errorMessage" class="mt-3 text-xs text-[var(--function-error)]">{{ errorMessage }}</p>

    <div v-if="canDecide" class="mt-3 flex flex-wrap gap-2">
      <button
        type="button"
        class="rounded-lg bg-[var(--Button-primary-black)] px-3 py-2 text-xs font-medium text-[var(--text-onblack)] disabled:cursor-not-allowed disabled:opacity-50"
        :disabled="!!deciding"
        @click="decide('approved')"
      >
        {{ t(deciding === 'approved' ? 'Approving...' : 'Approve once') }}
      </button>
      <button
        type="button"
        class="rounded-lg border border-[var(--border-btn-main)] px-3 py-2 text-xs font-medium text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)] disabled:cursor-not-allowed disabled:opacity-50"
        :disabled="!!deciding"
        @click="decide('rejected')"
      >
        {{ t(deciding === 'rejected' ? 'Rejecting...' : 'Reject') }}
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import {
  computed,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  ref,
  watch,
} from 'vue';
import { ShieldAlert } from 'lucide-vue-next';
import { useI18n } from 'vue-i18n';
import { decideToolApproval, getToolApproval } from '@/api/toolApproval';
import type {
  ToolApprovalDecision,
  ToolApprovalStatus,
  ToolApprovalView,
} from '@/types/toolApproval';
import {
  effectiveToolApprovalStatus,
  isToolApprovalTerminal,
  newerToolApproval,
} from '@/utils/toolApproval';
import { sanitizeToolDisplayValue } from '@/utils/toolDisplay';

const props = defineProps<{
  initial: ToolApprovalView;
  sessionId?: string;
  isShare?: boolean;
}>();

const { t, locale } = useI18n();
const approval = ref<ToolApprovalView>(props.initial);
const now = ref(Date.now());
const deciding = ref<ToolApprovalDecision | ''>('');
const errorKind = ref<'status' | 'decision' | ''>('');
const componentActive = ref(true);
const pageVisible = ref(typeof document === 'undefined' || document.visibilityState !== 'hidden');

const statusLabels: Record<ToolApprovalStatus, string> = {
  pending: 'Approval pending',
  approved: 'Approval approved',
  rejected: 'Approval rejected',
  consumed: 'Approval consumed',
  expired: 'Approval expired',
  cancelled: 'Approval cancelled',
};

const displayStatus = computed(() => effectiveToolApprovalStatus(approval.value, now.value));
const final = computed(() => isToolApprovalTerminal(displayStatus.value));
const canDecide = computed(() => (
  !!props.sessionId
  && !props.isShare
  && displayStatus.value === 'pending'
  && !deciding.value
));
const errorMessage = computed(() => {
  if (errorKind.value === 'status') return t('Approval status unavailable');
  if (errorKind.value === 'decision') return t('Approval decision failed');
  return '';
});
const argumentsPreview = computed(() => {
  try {
    return JSON.stringify(sanitizeToolDisplayValue(approval.value.arguments_preview), null, 2);
  } catch {
    return '{}';
  }
});
const expiresAt = computed(() => {
  const timestamp = Date.parse(approval.value.expires_at);
  if (!Number.isFinite(timestamp)) return t('Unavailable');
  return new Intl.DateTimeFormat(locale.value, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(timestamp);
});

let generation = 0;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let expiryTimer: ReturnType<typeof setTimeout> | undefined;
let pollRequest: AbortController | undefined;
let decisionRequest: AbortController | undefined;
let disposed = false;

const canPoll = () => (
  !disposed
  && componentActive.value
  && pageVisible.value
  && !props.isShare
  && !!props.sessionId
  && !final.value
);

function stopPoll() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = undefined;
  pollRequest?.abort();
  pollRequest = undefined;
}

function stopExpiryTimer() {
  if (expiryTimer) clearTimeout(expiryTimer);
  expiryTimer = undefined;
}

function scheduleExpiry() {
  stopExpiryTimer();
  if (final.value) return;
  const remaining = Date.parse(approval.value.expires_at) - Date.now();
  if (!Number.isFinite(remaining)) return;
  if (remaining <= 0) {
    now.value = Date.now();
    stopPoll();
    return;
  }
  expiryTimer = setTimeout(() => {
    now.value = Date.now();
    if (final.value) stopPoll();
    else scheduleExpiry();
  }, Math.min(remaining + 20, 2_147_000_000));
}

function schedulePoll(epoch: number) {
  if (!canPoll() || deciding.value) return;
  pollTimer = setTimeout(() => refresh(epoch), 2500);
}

async function refresh(epoch: number) {
  if (epoch !== generation || !canPoll() || deciding.value || !props.sessionId) return;
  const controller = new AbortController();
  pollRequest = controller;
  try {
    const latest = await getToolApproval(
      props.sessionId,
      approval.value.approval_id,
      controller.signal,
    );
    if (epoch !== generation || disposed || controller.signal.aborted) return;
    approval.value = newerToolApproval(approval.value, latest)!;
    errorKind.value = '';
    now.value = Date.now();
    scheduleExpiry();
  } catch {
    if (epoch !== generation || disposed || controller.signal.aborted || final.value) return;
    errorKind.value = 'status';
  } finally {
    if (pollRequest === controller) pollRequest = undefined;
    if (epoch === generation && !controller.signal.aborted) schedulePoll(epoch);
  }
}

function restartPolling() {
  stopPoll();
  scheduleExpiry();
  // Visibility/KeepAlive changes must not invalidate an in-flight decision:
  // its finally block owns clearing the busy state and resuming the poll.
  if (deciding.value) return;
  generation += 1;
  if (canPoll()) void refresh(generation);
}

async function decide(decision: ToolApprovalDecision) {
  if (!canDecide.value || !props.sessionId) return;
  const epoch = generation;
  deciding.value = decision;
  errorKind.value = '';
  stopPoll();
  const controller = new AbortController();
  decisionRequest = controller;
  try {
    const latest = await decideToolApproval(
      props.sessionId,
      approval.value.approval_id,
      decision,
      approval.value.revision,
      controller.signal,
    );
    if (epoch !== generation || disposed || controller.signal.aborted) return;
    approval.value = newerToolApproval(approval.value, latest)!;
    now.value = Date.now();
    scheduleExpiry();
  } catch {
    if (epoch === generation && !disposed && !controller.signal.aborted) {
      errorKind.value = 'decision';
    }
  } finally {
    if (decisionRequest === controller) decisionRequest = undefined;
    if (epoch === generation && !disposed) {
      deciding.value = '';
      schedulePoll(epoch);
    }
  }
}

function handleVisibilityChange() {
  pageVisible.value = document.visibilityState !== 'hidden';
  if (pageVisible.value) restartPolling();
  else stopPoll();
}

watch(
  () => `${props.sessionId || ''}:${props.initial.approval_id}:${!!props.isShare}`,
  () => {
    approval.value = props.initial;
    now.value = Date.now();
    deciding.value = '';
    errorKind.value = '';
    decisionRequest?.abort();
    decisionRequest = undefined;
    restartPolling();
  },
  { immediate: true },
);

watch(
  () => props.initial,
  (incoming) => {
    approval.value = newerToolApproval(approval.value, incoming)!;
    now.value = Date.now();
    scheduleExpiry();
    if (final.value) stopPoll();
  },
  { deep: true },
);

onMounted(() => document.addEventListener('visibilitychange', handleVisibilityChange));
onActivated(() => {
  componentActive.value = true;
  restartPolling();
});
onDeactivated(() => {
  componentActive.value = false;
  stopPoll();
});
onBeforeUnmount(() => {
  disposed = true;
  generation += 1;
  stopPoll();
  stopExpiryTimer();
  decisionRequest?.abort();
  decisionRequest = undefined;
  document.removeEventListener('visibilitychange', handleVisibilityChange);
});
</script>
