<template>
  <section class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
    <div>
      <h2 class="text-lg font-semibold text-[var(--text-primary)]">{{ t('Tool credentials') }}</h2>
      <p class="mt-1 text-sm leading-5 text-[var(--text-tertiary)]">
        {{ t('Store credentials only for secret slots declared by the current Cordis tool catalog.') }}
      </p>
    </div>

    <div
      v-if="!loading && !configured"
      class="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm leading-5 text-amber-800 dark:text-amber-300"
      role="status"
    >
      <div class="font-medium">{{ t('Credential storage is not configured') }}</div>
      <p class="mt-1 text-xs">{{ t('Ask an administrator to configure the credential encryption key before adding secrets.') }}</p>
    </div>

    <div v-if="configured" class="mt-4 rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
      <h3 class="text-sm font-semibold text-[var(--text-primary)]">{{ t('Add credential') }}</h3>
      <p class="mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
        {{ t('Existing DeepSeek credentials are not copied into tool credentials.') }}
      </p>

      <div v-if="catalogLoading" class="mt-4 text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
      <div
        v-else-if="requirements.length === 0"
        class="mt-4 rounded-lg border border-dashed border-[var(--border-main)] p-3 text-sm text-[var(--text-tertiary)]"
      >
        {{ t('No credential requirements are declared by the current tool catalog.') }}
      </div>
      <form v-else class="mt-4 grid gap-3" autocomplete="off" @submit.prevent="submitCredential">
        <label class="grid gap-1.5 text-xs text-[var(--text-secondary)]">
          {{ t('Declared tool and secret slot') }}
          <select
            v-model="selectedRequirementKey"
            class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--border-dark)]"
            :disabled="creating"
            required
          >
            <option v-for="item in requirements" :key="item.key" :value="item.key">
              {{ item.tool_name }} · {{ item.provider }} / {{ item.slot }}
            </option>
          </select>
        </label>

        <label class="grid gap-1.5 text-xs text-[var(--text-secondary)]">
          {{ t('Secret value') }}
          <input
            v-model="secret"
            type="password"
            name="tool-credential-secret"
            autocomplete="new-password"
            autocapitalize="none"
            spellcheck="false"
            class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--border-dark)]"
            :placeholder="t('Enter secret')"
            :disabled="creating"
            required
          />
        </label>

        <div>
          <button
            type="submit"
            class="inline-flex h-9 items-center justify-center rounded-lg bg-[var(--Button-primary-black)] px-3 text-sm font-medium text-[var(--text-onblack)] disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="creating || !selectedRequirement || !secret"
          >
            {{ t(creating ? 'Adding credential...' : 'Add credential') }}
          </button>
        </div>
      </form>
    </div>

    <p v-if="panelError" class="mt-4 text-sm text-[var(--function-error)]" role="alert">{{ panelError }}</p>

    <div class="mt-5">
      <div class="flex items-center justify-between gap-3">
        <h3 class="text-sm font-semibold text-[var(--text-primary)]">{{ t('Saved credentials') }}</h3>
        <button
          type="button"
          class="rounded-lg border border-[var(--border-btn-main)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50"
          :disabled="loading"
          @click="loadCredentials"
        >
          {{ t('Refresh') }}
        </button>
      </div>

      <div v-if="loading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
      <div
        v-else-if="credentials.length === 0"
        class="mt-3 rounded-xl border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]"
      >
        {{ t('No tool credentials saved') }}
      </div>
      <div v-else class="mt-3 grid gap-3 lg:grid-cols-2">
        <article
          v-for="credential in credentials"
          :key="credential.reference"
          class="min-w-0 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="break-words font-mono text-sm font-semibold text-[var(--text-primary)]">{{ credential.tool_name }}</div>
              <div class="mt-1 text-xs text-[var(--text-tertiary)]">
                {{ credential.provider }} · {{ credential.slot }}
              </div>
            </div>
            <span
              class="shrink-0 rounded-full border px-2 py-0.5 text-xs"
              :class="credential.revoked
                ? 'border-[var(--border-main)] text-[var(--text-tertiary)]'
                : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'"
            >
              {{ t(credential.revoked ? 'Revoked' : 'Active') }}
            </span>
          </div>
          <code class="mt-3 block break-all text-xs text-[var(--text-secondary)]">{{ credential.reference }}</code>
          <div class="mt-2 text-xs text-[var(--text-tertiary)]">
            {{ t('Created') }} {{ formatCreatedAt(credential.created_at) }} · {{ t('Revision') }} {{ credential.revision }}
          </div>
          <button
            v-if="!credential.revoked"
            type="button"
            class="mt-3 rounded-lg border border-[var(--border-btn-main)] px-2.5 py-1.5 text-xs font-medium text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)] disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="revoking === credential.reference"
            @click="revoke(credential.reference)"
          >
            {{ t(revoking === credential.reference ? 'Revoking...' : 'Revoke') }}
          </button>
        </article>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  createCredential,
  getCredentials,
  revokeCredential,
  type ToolCredentialView,
} from '@/api/credential';
import { getPluginRuntime, type PluginRuntimeSnapshot } from '@/api/pluginRuntime';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

interface CredentialRequirementOption {
  key: string;
  tool_name: string;
  provider: string;
  slot: string;
}

const { t, locale } = useI18n();
const configured = ref(false);
const credentials = ref<ToolCredentialView[]>([]);
const runtime = ref<PluginRuntimeSnapshot | null>(null);
const loading = ref(true);
const catalogLoading = ref(true);
const creating = ref(false);
const revoking = ref('');
const selectedRequirementKey = ref('');
const secret = ref('');
const errorKind = ref<'credentials' | 'catalog' | 'create' | 'revoke' | ''>('');
const loadController = new AbortController();
const catalogController = new AbortController();
let disposed = false;

const requirements = computed<CredentialRequirementOption[]>(() => {
  const unique = new Map<string, CredentialRequirementOption>();
  for (const tool of runtime.value?.tools || []) {
    for (const requirement of tool.execution?.credentials || []) {
      const key = JSON.stringify([tool.name, requirement.provider, requirement.slot]);
      unique.set(key, {
        key,
        tool_name: tool.name,
        provider: requirement.provider,
        slot: requirement.slot,
      });
    }
  }
  return [...unique.values()].sort((left, right) => (
    left.tool_name.localeCompare(right.tool_name)
    || left.provider.localeCompare(right.provider)
    || left.slot.localeCompare(right.slot)
  ));
});

const selectedRequirement = computed(() => (
  requirements.value.find((item) => item.key === selectedRequirementKey.value) || null
));

const panelError = computed(() => {
  if (errorKind.value === 'credentials') return t('Failed to load tool credentials');
  if (errorKind.value === 'catalog') return t('Failed to load credential requirements');
  if (errorKind.value === 'create') return t('Failed to add credential');
  if (errorKind.value === 'revoke') return t('Failed to revoke credential');
  return '';
});

watch(requirements, (options) => {
  if (!options.some((item) => item.key === selectedRequirementKey.value)) {
    selectedRequirementKey.value = options[0]?.key || '';
  }
}, { immediate: true });

async function loadCredentials() {
  if (disposed) return;
  loading.value = true;
  if (errorKind.value === 'credentials') errorKind.value = '';
  try {
    const result = await getCredentials(loadController.signal);
    if (disposed) return;
    configured.value = result.configured;
    credentials.value = result.credentials;
  } catch {
    if (disposed || loadController.signal.aborted) return;
    errorKind.value = 'credentials';
    showErrorToast(t('Failed to load tool credentials'));
  } finally {
    if (!disposed) loading.value = false;
  }
}

async function loadCatalog() {
  catalogLoading.value = true;
  if (errorKind.value === 'catalog') errorKind.value = '';
  try {
    runtime.value = await getPluginRuntime();
  } catch {
    if (disposed || catalogController.signal.aborted) return;
    errorKind.value = 'catalog';
    showErrorToast(t('Failed to load credential requirements'));
  } finally {
    if (!disposed) catalogLoading.value = false;
  }
}

async function submitCredential() {
  const requirement = selectedRequirement.value;
  if (!configured.value || !requirement || !secret.value || creating.value) return;
  creating.value = true;
  errorKind.value = '';
  try {
    await createCredential({
      provider: requirement.provider,
      tool_name: requirement.tool_name,
      slot: requirement.slot,
      secret: secret.value,
    });
    // Clear the only UI copy before any follow-up request or notification.
    secret.value = '';
    showSuccessToast(t('Credential added'));
    await loadCredentials();
  } catch {
    if (!disposed) {
      errorKind.value = 'create';
      showErrorToast(t('Failed to add credential'));
    }
  } finally {
    if (!disposed) creating.value = false;
  }
}

async function revoke(reference: string) {
  if (revoking.value) return;
  revoking.value = reference;
  errorKind.value = '';
  try {
    await revokeCredential(reference);
    showSuccessToast(t('Credential revoked'));
    await loadCredentials();
  } catch {
    if (!disposed) {
      errorKind.value = 'revoke';
      showErrorToast(t('Failed to revoke credential'));
    }
  } finally {
    if (!disposed) revoking.value = '';
  }
}

function formatCreatedAt(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return t('Unavailable');
  return new Intl.DateTimeFormat(locale.value, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(timestamp);
}

onMounted(() => {
  void loadCredentials();
  void loadCatalog();
});

onBeforeUnmount(() => {
  disposed = true;
  secret.value = '';
  loadController.abort();
  catalogController.abort();
});
</script>
