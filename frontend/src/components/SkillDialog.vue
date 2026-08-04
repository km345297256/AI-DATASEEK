<template>
  <Dialog v-model:open="open">
    <DialogContent class="w-[520px]">
      <DialogHeader>
        <DialogTitle>{{ t('Skills') }}</DialogTitle>
      </DialogHeader>

      <div class="px-6 pb-5 pt-2">
        <div class="mb-4 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-[var(--text-secondary)]">{{ t('Select skills for this message') }}</p>
            <p class="mt-1 text-xs text-[var(--text-tertiary)]">{{ t('Upload Markdown or ZIP skill packages') }}</p>
          </div>
          <button
            class="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
            :disabled="uploading"
            @click="fileInput?.click()"
          >
            <Upload :size="15" />
            <span>{{ uploading ? t('Uploading...') : t('Upload') }}</span>
          </button>
          <input ref="fileInput" class="hidden" type="file" accept=".md,.zip" @change="handleUpload" />
        </div>

        <div v-if="loading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">
          {{ t('Loading') }}...
        </div>
        <div v-else-if="visibleSkills.length === 0" class="rounded-lg border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">
          {{ t('No skills yet') }}
        </div>
        <div v-else class="max-h-[360px] overflow-y-auto rounded-lg border border-[var(--border-main)]">
          <label
            v-for="skill in visibleSkills"
            :key="skill.name"
            class="flex cursor-pointer items-start gap-3 border-b border-[var(--border-main)] px-3 py-3 last:border-b-0 hover:bg-[var(--fill-tsp-white-light)]"
          >
            <input
              type="checkbox"
              class="mt-1 h-4 w-4 accent-[var(--Button-primary-black)]"
              :checked="selectedSkills.includes(skill.name)"
              @change="toggleSkill(skill.name)"
            />
            <div class="min-w-0 flex-1">
              <div class="truncate text-sm font-medium text-[var(--text-primary)]">{{ skill.name }}</div>
              <div class="mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
                {{ skill.description || t('No description') }}
              </div>
              <div v-if="skill.triggers.length" class="mt-2 flex flex-wrap gap-1">
                <span
                  v-for="trigger in skill.triggers.slice(0, 6)"
                  :key="trigger"
                  class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]"
                >
                  {{ trigger }}
                </span>
              </div>
            </div>
          </label>
        </div>

        <DialogFooter>
          <button
            class="rounded-[10px] border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-dark)]"
            @click="open = false"
          >
            {{ t('Done') }}
          </button>
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Upload } from 'lucide-vue-next';
import { useI18n } from 'vue-i18n';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { getSkillPreferences, listSkills, uploadSkill, type SkillInfo } from '@/api/skill';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const props = defineProps<{
  open: boolean;
  selectedSkills: string[];
}>();

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void;
  (e: 'update:selectedSkills', value: string[]): void;
}>();

const { t } = useI18n();
const skills = ref<SkillInfo[]>([]);
const autoEnabledSkillNames = ref(new Set<string>());
const loading = ref(false);
const uploading = ref(false);
const fileInput = ref<HTMLInputElement>();

const open = computed({
  get: () => props.open,
  set: (value: boolean) => emit('update:open', value),
});

const selectedSkills = computed(() => props.selectedSkills ?? []);
const visibleSkills = computed(() => skills.value.filter(
  (skill) => !autoEnabledSkillNames.value.has(skill.name.trim().toLowerCase()),
));

const loadSkills = async () => {
  loading.value = true;
  try {
    const [availableSkills, preferences] = await Promise.all([listSkills(), getSkillPreferences()]);
    skills.value = availableSkills;
    autoEnabledSkillNames.value = new Set(preferences.map((name) => name.trim().toLowerCase()));
  } catch (error) {
    console.error('Failed to load skills:', error);
    showErrorToast(t('Failed to load skills'));
  } finally {
    loading.value = false;
  }
};

const toggleSkill = (name: string) => {
  const next = new Set(selectedSkills.value);
  if (next.has(name)) {
    next.delete(name);
  } else {
    next.add(name);
  }
  emit('update:selectedSkills', [...next]);
};

const handleUpload = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  uploading.value = true;
  try {
    const uploaded = await uploadSkill(file);
    await loadSkills();
    showSuccessToast(t('Skill uploaded'));
    emit('update:selectedSkills', [...new Set([...selectedSkills.value, ...uploaded.map((skill) => skill.name)])]);
  } catch (error) {
    console.error('Failed to upload skill:', error);
    showErrorToast(t('Failed to upload skill'));
  } finally {
    uploading.value = false;
    input.value = '';
  }
};

watch(open, (value) => {
  if (value) {
    loadSkills();
  }
});
</script>
