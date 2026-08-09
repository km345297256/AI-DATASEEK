<template>
  <div class="flex w-full flex-col gap-4">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <label class="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-lg bg-[var(--background-gray-main)] px-3">
        <Search :size="16" class="shrink-0 text-[var(--icon-tertiary)]" />
        <input
          v-model="query"
          class="min-w-0 flex-1 bg-transparent text-sm text-[var(--text-primary)] outline-none"
          :placeholder="t('Search skills')"
        />
      </label>
      <div class="flex shrink-0 gap-2">
        <button
          type="button"
          class="inline-flex h-10 items-center gap-1.5 rounded-lg border border-[var(--border-main)] px-3 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]"
          @click="browseSkills"
        >
          <Library :size="15" />
          {{ t('Browse skills') }}
        </button>
        <button
          type="button"
          class="inline-flex h-10 items-center gap-1.5 rounded-lg border border-[var(--border-main)] px-3 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50"
          :disabled="uploading"
          @click="fileInput?.click()"
        >
          <Upload :size="15" />
          {{ uploading ? t('Uploading...') : t('Upload') }}
        </button>
        <input ref="fileInput" class="hidden" type="file" accept=".md,.zip" @change="handleUpload" />
      </div>
    </div>

    <div class="flex items-center justify-between border-b border-[var(--border-main)] pb-2">
      <span class="text-xs font-medium text-[var(--text-tertiary)]">{{ t('Added skills') }}</span>
      <span class="text-xs text-[var(--text-tertiary)]">{{ t('Auto enabled count', { count: autoEnabledSkills.size }) }}</span>
    </div>

    <div v-if="loading" class="py-10 text-center text-sm text-[var(--text-tertiary)]">
      {{ t('Loading') }}...
    </div>
    <div v-else-if="filteredSkills.length === 0" class="rounded-lg border border-dashed border-[var(--border-main)] py-10 text-center text-sm text-[var(--text-tertiary)]">
      {{ query ? t('No matching skills') : t('No skills yet') }}
    </div>
    <div v-else class="grid gap-3 lg:grid-cols-2">
      <article
        v-for="skill in filteredSkills"
        :key="skill.id"
        class="flex min-h-[92px] items-start gap-3 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-3"
      >
        <div class="flex size-9 shrink-0 items-center justify-center rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)]">
          <Puzzle :size="17" class="text-[var(--icon-secondary)]" />
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="truncate text-sm font-medium text-[var(--text-primary)]">{{ skill.name }}</span>
            <span class="shrink-0 text-[10px] text-[var(--text-tertiary)]">
              {{ skill.scope === 'global' ? t('Global') : t('Personal') }}
            </span>
          </div>
          <p class="skill-description mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
            {{ skill.description || t('No description') }}
          </p>
        </div>
        <div class="flex shrink-0 flex-col items-end gap-1">
          <button
            type="button"
            role="switch"
            class="relative h-6 w-11 rounded-full transition-colors disabled:opacity-50"
            :class="autoEnabledSkills.has(skill.name) ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
            :aria-checked="autoEnabledSkills.has(skill.name)"
            :aria-label="t('Auto enable skill', { name: skill.name })"
            :disabled="saving"
            @click="toggleSkill(skill.name)"
          >
            <span
              class="absolute left-0 top-0.5 size-5 rounded-full bg-white shadow-sm transition-transform"
              :class="autoEnabledSkills.has(skill.name) ? 'translate-x-[21px]' : 'translate-x-0.5'"
            />
          </button>
          <span class="text-[10px] text-[var(--text-tertiary)]">
            {{ autoEnabledSkills.has(skill.name) ? t('Automatic') : t('Manual') }}
          </span>
        </div>
      </article>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Library, Puzzle, Search, Upload } from 'lucide-vue-next';
import { useI18n } from 'vue-i18n';
import { useRouter } from 'vue-router';
import {
  getSkillPreferences,
  listSkills,
  updateSkillPreferences,
  uploadSkill,
  type SkillInfo,
} from '@/api/skill';
import { useSettingsDialog } from '@/composables/useSettingsDialog';
import { showErrorToast, showSuccessToast } from '@/utils/toast';
import { eventBus } from '@/utils/eventBus';
import { EVENT_SKILL_CATALOG_UPDATED, EVENT_SKILL_PREFERENCES_UPDATED } from '@/constants/event';

const { t } = useI18n();
const router = useRouter();
const { closeSettingsDialog } = useSettingsDialog();
const skills = ref<SkillInfo[]>([]);
const autoEnabledSkills = ref(new Set<string>());
const query = ref('');
const loading = ref(false);
const saving = ref(false);
const uploading = ref(false);
const fileInput = ref<HTMLInputElement>();

const filteredSkills = computed(() => {
  const normalized = query.value.trim().toLowerCase();
  if (!normalized) return skills.value;
  return skills.value.filter((skill) => [skill.name, skill.description, ...(skill.triggers || [])]
    .some((value) => value?.toLowerCase().includes(normalized)));
});

const load = async () => {
  loading.value = true;
  try {
    const [availableSkills, enabledSkills] = await Promise.all([
      listSkills(),
      getSkillPreferences(),
    ]);
    skills.value = availableSkills;
    autoEnabledSkills.value = new Set(enabledSkills);
  } catch (error) {
    console.error('Failed to load skill settings:', error);
    showErrorToast(t('Failed to load skill settings'));
  } finally {
    loading.value = false;
  }
};

const toggleSkill = async (name: string) => {
  if (saving.value) return;
  const previous = new Set(autoEnabledSkills.value);
  const next = new Set(previous);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  autoEnabledSkills.value = next;
  saving.value = true;
  try {
    const saved = await updateSkillPreferences([...next]);
    autoEnabledSkills.value = new Set(saved);
    eventBus.emit(EVENT_SKILL_PREFERENCES_UPDATED, saved);
  } catch (error) {
    autoEnabledSkills.value = previous;
    console.error('Failed to update skill settings:', error);
    showErrorToast(t('Failed to update skill settings'));
  } finally {
    saving.value = false;
  }
};

const handleUpload = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  try {
    await uploadSkill(file);
    await load();
    eventBus.emit(EVENT_SKILL_CATALOG_UPDATED);
    showSuccessToast(t('Skill uploaded'));
  } catch (error) {
    console.error('Failed to upload skill:', error);
    showErrorToast(t('Failed to upload skill'));
  } finally {
    uploading.value = false;
    input.value = '';
  }
};

const browseSkills = () => {
  closeSettingsDialog();
  router.push({ path: '/chat/plugins', query: { tab: 'skills' } });
};

onMounted(load);
</script>

<style scoped>
.skill-description {
  display: -webkit-box;
  overflow: hidden;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
</style>
