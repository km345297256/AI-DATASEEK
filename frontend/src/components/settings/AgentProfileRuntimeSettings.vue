<template>
  <div class="flex w-full flex-col gap-5 pb-6">
    <div v-if="loading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">
      {{ t('Loading') }}...
    </div>

    <div v-else-if="loadFailed" class="rounded-xl border border-[var(--function-error)]/30 bg-[var(--function-error)]/5 p-4 text-sm text-[var(--function-error)]">
      {{ t('Failed to load agent profiles') }}
    </div>

    <template v-else>
      <section v-if="profiles.length === 0 && !creatingNew" class="rounded-xl border border-dashed border-[var(--border-main)] p-4">
        <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('No agent profiles found') }}</div>
        <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">
          {{ t('No profile was created automatically. Tasks continue to use the server defaults above.') }}
        </p>
      </section>

      <div v-if="profiles.length > 0 && !creatingNew" class="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label class="flex flex-1 flex-col gap-1.5 text-xs text-[var(--text-secondary)]">
          {{ t('Agent Profile') }}
          <select
            v-model="selectedProfileId"
            class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-sm text-[var(--text-primary)] outline-none"
          >
            <option v-for="profile in profiles" :key="profile.id" :value="profile.id">
              {{ profile.name }} · {{ profile.model_name }}
            </option>
          </select>
        </label>
        <button
          v-if="isAdmin"
          type="button"
          class="h-10 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
          @click="startCreate"
        >
          {{ t('New profile from current model') }}
        </button>
      </div>

      <section v-if="creatingNew || selectedProfile" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4">
        <div v-if="creatingNew" class="mb-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="text-sm font-semibold text-[var(--text-primary)]">{{ t('New profile from current model') }}</div>
              <p class="mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
                {{ t('This creates a profile with the current server model settings and never copies an API key.') }}
              </p>
            </div>
            <button v-if="profiles.length > 0" type="button" class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]" @click="cancelCreate">
              {{ t('Cancel') }}
            </button>
          </div>
          <label class="mt-3 flex flex-col gap-1.5 text-xs text-[var(--text-secondary)]">
            {{ t('Profile name') }}
            <input
              v-model="newProfileName"
              maxlength="100"
              class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-sm text-[var(--text-primary)] outline-none"
              :placeholder="t('For example: Geoscience on demand')"
            />
          </label>
        </div>

        <div class="mt-4 divide-y divide-[var(--border-light)] border-y border-[var(--border-light)]">
          <label class="flex cursor-pointer items-start justify-between gap-4 py-4">
            <span class="min-w-0">
              <span class="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                {{ t('Code Mode') }}
                <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[10px] font-normal text-[var(--text-tertiary)]">{{ t('Experimental') }}</span>
              </span>
              <span class="mt-1 block text-xs leading-5 text-[var(--text-tertiary)]">
                {{ t('Opt in to code-oriented execution for tasks using this profile.') }}
              </span>
            </span>
            <input v-model="draft.code_mode_enabled" type="checkbox" class="mt-1 h-4 w-4 shrink-0 accent-[var(--Button-primary-black)]" :disabled="!isAdmin || saving" />
          </label>

          <label class="flex cursor-pointer items-start justify-between gap-4 py-4">
            <span class="min-w-0">
              <span class="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                {{ t('Domain SubAgents') }}
                <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[10px] font-normal text-[var(--text-tertiary)]">{{ t('Experimental') }}</span>
              </span>
              <span class="mt-1 block text-xs leading-5 text-[var(--text-tertiary)]">
                {{ t('Opt in to the preset-specific domain SubAgent pilot for tasks using this profile.') }}
              </span>
            </span>
            <input v-model="draft.domain_subagents_enabled" type="checkbox" class="mt-1 h-4 w-4 shrink-0 accent-[var(--Button-primary-black)]" :disabled="!isAdmin || saving" />
          </label>
        </div>

        <div class="mt-4 grid gap-4 sm:grid-cols-2">
          <label class="flex flex-col gap-1.5 text-xs text-[var(--text-secondary)]">
            {{ t('Domain preset') }}
            <select
              v-model="draft.preset_id"
              class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-sm text-[var(--text-primary)] outline-none disabled:opacity-60"
              :disabled="!isAdmin || saving"
            >
              <option v-for="preset in catalog?.presets || []" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
            </select>
          </label>
          <label class="flex flex-col gap-1.5 text-xs text-[var(--text-secondary)]">
            {{ t('Tool selection') }}
            <select
              v-model="draft.selection_mode"
              class="h-10 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-sm text-[var(--text-primary)] outline-none disabled:opacity-60"
              :disabled="!isAdmin || saving"
            >
              <option value="on_demand">{{ t('On demand') }}</option>
              <option value="all">{{ t('All preset tools') }}</option>
            </select>
          </label>
        </div>

        <details v-if="selectedPreset" class="mt-3 rounded-lg bg-[var(--background-gray-main)] p-3">
          <summary class="cursor-pointer text-xs font-medium text-[var(--text-secondary)]">{{ t('Preset scope and tool counts') }}</summary>
          <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ selectedPreset.description }}</p>
          <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--text-secondary)]">
            <span>{{ selectedPreset.initial_tool_count }} {{ t('initial tools') }}</span>
            <span>{{ selectedPreset.available_tool_count }} {{ t('available tools') }}</span>
          </div>
          <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">
            {{ draft.selection_mode === 'on_demand'
              ? t('On-demand mode starts with the preset initial tools and expands only when the task needs more tools.')
              : t('All mode exposes every tool in this preset, not every tool in the full catalog.') }}
          </p>
        </details>

        <div class="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p class="text-xs leading-5 text-[var(--text-tertiary)]">
            {{ t('Saved changes apply to new tasks that select this profile.') }}
          </p>
          <button
            v-if="isAdmin"
            type="button"
            class="h-10 rounded-lg bg-[var(--Button-primary-black)] px-4 text-sm font-medium text-[var(--text-onblack)] disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="saving || !selectedPreset || (creatingNew ? !newProfileName.trim() : !hasChanges)"
            @click="save"
          >
            {{ saving ? t('Saving...') : creatingNew ? t('Create profile') : t('Save') }}
          </button>
          <span v-else class="text-xs text-[var(--text-tertiary)]">{{ t('Only administrators can update agent profiles.') }}</span>
        </div>
        <button
          v-if="selectedProfile && !creatingNew"
          type="button"
          class="mt-3 min-h-10 w-full rounded-lg border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="saving || hasChanges"
          @click="useProfileInNewChat"
        >
          {{ t('Use this profile in a new chat') }}
        </button>
      </section>
    </template>

    <details class="rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
      <summary class="cursor-pointer text-sm font-semibold text-[var(--text-primary)]">{{ t('Default without a profile') }}</summary>
      <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ t('Tasks without a selected profile use the server defaults shown here.') }}</p>
      <div class="mt-3 flex flex-wrap gap-2 text-xs text-[var(--text-secondary)]">
        <span>{{ presetLabel(noProfileDefaults.preset_id) }} · {{ selectionModeLabel(noProfileDefaults.selection_mode) }}</span>
        <span>{{ t('Code Mode') }}: {{ enabledLabel(noProfileDefaults.code_mode_enabled) }}</span>
        <span>{{ t('Domain SubAgents') }}: {{ enabledLabel(noProfileDefaults.domain_subagents_enabled) }}</span>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRouter } from 'vue-router';
import {
  createRuntimePresetProfile,
  listAgentProfiles,
  updateAgentProfile,
  type AgentProfile,
  type AgentToolRuntimeConfig,
  type AgentToolSelectionMode,
} from '@/api/agentProfile';
import { getDomainPresetCatalog, type DomainPresetCatalog } from '@/api/domainPreset';
import { useAgentProfile } from '@/composables/useAgentProfile';
import { useAuth } from '@/composables/useAuth';
import { useSettingsDialog } from '@/composables/useSettingsDialog';
import { copyAgentToolRuntime, sameAgentToolRuntime } from '@/utils/agentToolRuntime';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const { t } = useI18n();
const { isAdmin } = useAuth();
const { refreshProfiles, setSelectedProfile } = useAgentProfile();
const { closeSettingsDialog } = useSettingsDialog();
const router = useRouter();

const NO_PROFILE_FALLBACK: AgentToolRuntimeConfig = {
  preset_id: 'general',
  selection_mode: 'on_demand',
  code_mode_enabled: false,
  domain_subagents_enabled: false,
};

const profiles = ref<AgentProfile[]>([]);
const catalog = ref<DomainPresetCatalog | null>(null);
const selectedProfileId = ref('');
const creatingNew = ref(false);
const newProfileName = ref('');
const savedRuntime = ref<AgentToolRuntimeConfig | null>(null);
const loading = ref(true);
const saving = ref(false);
const loadFailed = ref(false);
const draft = reactive<AgentToolRuntimeConfig>(copyAgentToolRuntime());

const selectedProfile = computed(() => profiles.value.find((profile) => profile.id === selectedProfileId.value) || null);
const selectedPreset = computed(() => catalog.value?.presets.find((preset) => preset.id === draft.preset_id) || null);
const noProfileDefaults = computed(() => catalog.value?.defaults || NO_PROFILE_FALLBACK);
const hasChanges = computed(() => !sameAgentToolRuntime(savedRuntime.value, draft));

function syncDraft(profile: AgentProfile | null): void {
  const next = copyAgentToolRuntime(profile?.tool_runtime);
  Object.assign(draft, next);
  savedRuntime.value = copyAgentToolRuntime(next);
}

function startCreate(): void {
  creatingNew.value = true;
  newProfileName.value = '';
  Object.assign(draft, copyAgentToolRuntime(noProfileDefaults.value));
  savedRuntime.value = null;
}

function cancelCreate(): void {
  creatingNew.value = false;
  newProfileName.value = '';
  syncDraft(selectedProfile.value);
}

function presetLabel(presetId: string): string {
  return catalog.value?.presets.find((preset) => preset.id === presetId)?.name || presetId;
}

function selectionModeLabel(mode: AgentToolSelectionMode): string {
  return mode === 'on_demand' ? t('On demand') : t('All preset tools');
}

function enabledLabel(enabled: boolean): string {
  return t(enabled ? 'Enabled' : 'Disabled');
}

function useProfileInNewChat(): void {
  if (!selectedProfile.value || creatingNew.value || saving.value || hasChanges.value) return;
  setSelectedProfile(selectedProfile.value);
  closeSettingsDialog();
  void router.push('/');
}

async function load(): Promise<void> {
  loading.value = true;
  loadFailed.value = false;
  try {
    const [profileResult, presetResult] = await Promise.all([
      listAgentProfiles(),
      getDomainPresetCatalog(),
    ]);
    profiles.value = profileResult;
    catalog.value = presetResult;
    selectedProfileId.value = profileResult[0]?.id || '';
    if (profileResult.length === 0 && isAdmin.value) {
      startCreate();
    } else {
      syncDraft(profileResult[0] || null);
    }
  } catch {
    loadFailed.value = true;
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  const profile = selectedProfile.value;
  if (!isAdmin.value || !selectedPreset.value) return;
  if (creatingNew.value && !newProfileName.value.trim()) return;
  if (!creatingNew.value && (!profile || !hasChanges.value)) return;
  saving.value = true;
  try {
    const toolRuntime = copyAgentToolRuntime(draft);
    const updated = creatingNew.value
      ? await createRuntimePresetProfile({ name: newProfileName.value.trim(), tool_runtime: toolRuntime })
      : await updateAgentProfile(profile!.id, { tool_runtime: toolRuntime });
    const index = profiles.value.findIndex((candidate) => candidate.id === updated.id);
    if (index >= 0) profiles.value[index] = updated;
    else profiles.value.push(updated);
    creatingNew.value = false;
    newProfileName.value = '';
    selectedProfileId.value = updated.id;
    syncDraft(updated);
    void refreshProfiles().catch(() => undefined);
    showSuccessToast(t(index >= 0 ? 'Agent profile tool settings saved' : 'Agent profile created. Select it in a new chat to use it.'));
  } catch {
    showErrorToast(t(creatingNew.value ? 'Failed to create agent profile' : 'Failed to save agent profile tool settings'));
  } finally {
    saving.value = false;
  }
}

watch(selectedProfileId, () => {
  if (!creatingNew.value) syncDraft(selectedProfile.value);
});
onMounted(load);
</script>
