<template>
  <div class="w-full flex flex-col gap-6">

    <!-- Create new key section -->
    <div class="pb-6 border-b border-[var(--border-light)]">
      <div class="text-[13px] font-medium text-[var(--text-tertiary)] mb-3">{{ t('API Keys') }}</div>

      <div v-if="!showCreateForm" class="flex items-center justify-between">
        <p class="text-sm text-[var(--text-secondary)]">{{ t('Use API keys to access the API programmatically.') }}</p>
        <button
          class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 outline outline-1 -outline-offset-1 hover:bg-[var(--fill-tsp-white-light)] text-[var(--text-primary)] outline-[var(--border-btn-main)] bg-transparent h-[32px]"
          @click="showCreateForm = true">
          <Plus class="w-4 h-4" />
          {{ t('Create API Key') }}
        </button>
      </div>

      <!-- Inline create form -->
      <div v-else class="flex flex-col gap-3">
        <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('New API Key') }}</div>
        <div class="flex flex-col gap-2">
          <label class="text-xs text-[var(--text-secondary)]">{{ t('Name') }}</label>
          <div
            class="group rounded-[10px] overflow-hidden text-sm leading-[22px] text-[var(--text-primary)] h-9 flex items-center gap-[10px] bg-[var(--fill-tsp-white-main)] px-3 focus-within:ring-[1.5px] focus-within:ring-[var(--border-dark)] w-full sm:w-[300px]">
            <input
              v-model="newKeyName"
              class="h-full flex-1 bg-transparent placeholder:text-[var(--text-disable)]"
              :placeholder="t('e.g. My App')"
              @keyup.enter="handleCreate" />
          </div>
        </div>
        <div class="flex flex-col gap-2">
          <label class="text-xs text-[var(--text-secondary)]">{{ t('Expiration') }}</label>
          <div class="flex gap-2 flex-wrap">
            <button
              v-for="opt in expiryOptions"
              :key="opt.value ?? 'never'"
              @click="selectedExpiry = opt.value"
              :class="[
                'px-3 h-8 rounded-[10px] text-sm transition-colors border',
                selectedExpiry === opt.value
                  ? 'border-[var(--border-dark)] bg-[var(--fill-tsp-white-main)] font-medium text-[var(--text-primary)]'
                  : 'border-[var(--border-btn-main)] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]'
              ]">
              {{ t(opt.label) }}
            </button>
          </div>
        </div>
        <div class="flex gap-2 mt-1">
          <button
            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed px-[12px] rounded-[10px] gap-[6px] text-sm h-[32px] bg-[var(--Button-primary-black)] text-[var(--text-onblack)] border border-[var(--Button-primary-black)]"
            :disabled="!newKeyName.trim() || creating"
            @click="handleCreate">
            {{ creating ? t('Creating...') : t('Create') }}
          </button>
          <button
            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 px-[12px] rounded-[10px] gap-[6px] text-sm h-[32px] outline outline-1 -outline-offset-1 outline-[var(--border-btn-main)] bg-transparent text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
            @click="cancelCreate">
            {{ t('Cancel') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Newly created key banner -->
    <div v-if="newlyCreatedKey"
      class="flex flex-col gap-2 p-4 rounded-[12px] bg-[var(--fill-tsp-white-main)] border border-[var(--border-main)]">
      <div class="flex items-center justify-between">
        <span class="text-sm font-medium text-[var(--text-primary)]">{{ t('Save your API key') }}</span>
        <button @click="newlyCreatedKey = null" class="text-[var(--icon-tertiary)] hover:text-[var(--icon-primary)]">
          <X class="w-4 h-4" />
        </button>
      </div>
      <p class="text-xs text-[var(--text-secondary)]">{{ t("It won't be shown again.") }}</p>
      <div
        class="flex items-center gap-2 bg-[var(--fill-tsp-white-light)] rounded-[8px] px-3 py-2 font-mono text-sm text-[var(--text-primary)] break-all">
        <span class="flex-1">{{ newlyCreatedKey }}</span>
        <button @click="copyKey(newlyCreatedKey)" class="flex-shrink-0 text-[var(--icon-secondary)] hover:text-[var(--icon-primary)]">
          <CheckCheck v-if="copied" class="w-4 h-4 text-green-500" />
          <Copy v-else class="w-4 h-4" />
        </button>
      </div>
    </div>

    <!-- Keys list -->
    <div class="flex flex-col">
      <div v-if="loading" class="text-sm text-[var(--text-tertiary)] py-4">{{ t('Loading...') }}</div>
      <div v-else-if="keys.length === 0 && !showCreateForm"
        class="text-sm text-[var(--text-tertiary)] py-2">{{ t('No API keys yet.') }}</div>
      <div v-else class="flex flex-col">
        <div
          v-for="key in keys"
          :key="key.id"
          class="flex items-center justify-between py-3 border-b border-[var(--border-light)] last:border-b-0 gap-3">
          <div class="flex flex-col gap-[2px] min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-[var(--text-primary)] truncate">{{ key.name }}</span>
              <span
                :class="[
                  'text-[11px] px-1.5 py-0.5 rounded-full font-medium',
                  key.status === 'active' && (!key.expires_at || parseBackendDate(key.expires_at) > new Date())
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                    : 'bg-[var(--fill-tsp-white-main)] text-[var(--text-tertiary)]'
                ]">
                {{ keyStatusLabel(key) }}
              </span>
            </div>
            <div class="flex items-center gap-3 text-xs text-[var(--text-tertiary)]">
              <span class="font-mono">{{ key.key_prefix }}••••</span>
              <span>{{ t('Created') }} {{ formatDate(key.created_at) }}</span>
              <span v-if="key.expires_at">{{ t('Expires') }} {{ formatDate(key.expires_at) }}</span>
              <span v-if="key.last_used_at">{{ t('Last used') }} {{ formatDate(key.last_used_at) }}</span>
            </div>
          </div>
          <button
            v-if="key.status === 'active'"
            class="flex-shrink-0 inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 px-[12px] rounded-[10px] gap-[6px] text-sm h-[32px] outline outline-1 -outline-offset-1 outline-[var(--border-btn-main)] bg-transparent text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)]"
            @click="handleRevoke(key.id)">
            {{ t('Revoke') }}
          </button>
        </div>
      </div>
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus, Copy, CheckCheck, X } from 'lucide-vue-next'
import { listAPIKeys, createAPIKey, revokeAPIKey, type APIKey } from '@/api/apiKey'
import { showSuccessToast, showErrorToast } from '@/utils/toast'
import { formatDate as formatBackendDate, parseBackendDate } from '@/utils/time'

const { t } = useI18n()

const keys = ref<APIKey[]>([])
const loading = ref(false)
const showCreateForm = ref(false)
const creating = ref(false)
const newKeyName = ref('')
const selectedExpiry = ref<number | null>(30)
const newlyCreatedKey = ref<string | null>(null)
const copied = ref(false)

const expiryOptions: { label: string; value: number | null }[] = [
  { label: '30 days', value: 30 },
  { label: '90 days', value: 90 },
  { label: '1 year', value: 365 },
  { label: 'No expiration', value: null },
]

async function loadKeys() {
  loading.value = true
  try {
    keys.value = await listAPIKeys()
  } catch (e: any) {
    showErrorToast(e?.message || t('Failed to load API keys'))
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  if (!newKeyName.value.trim() || creating.value) return
  creating.value = true
  try {
    const result = await createAPIKey({
      name: newKeyName.value.trim(),
      scopes: ['full'],
      expires_in_days: selectedExpiry.value,
    })
    newlyCreatedKey.value = result.key
    await loadKeys()
    cancelCreate()
    showSuccessToast(t('API key created'))
  } catch (e: any) {
    showErrorToast(e?.message || t('Failed to create API key'))
  } finally {
    creating.value = false
  }
}

async function handleRevoke(keyId: string) {
  try {
    await revokeAPIKey(keyId)
    await loadKeys()
    showSuccessToast(t('API key revoked'))
  } catch (e: any) {
    showErrorToast(e?.message || t('Failed to revoke API key'))
  }
}

function cancelCreate() {
  showCreateForm.value = false
  newKeyName.value = ''
  selectedExpiry.value = 30
}

async function copyKey(key: string) {
  await navigator.clipboard.writeText(key)
  copied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}

function formatDate(iso: string) {
  return formatBackendDate(iso)
}

function keyStatusLabel(key: APIKey) {
  if (key.status === 'revoked') return t('Revoked')
  if (key.expires_at && parseBackendDate(key.expires_at) <= new Date()) return t('Expired')
  return t('Active')
}

onMounted(loadKeys)
</script>
