<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]">
    <div class="flex-1 flex items-center justify-center">
      <div class="max-w-[250px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center">
        {{ t('Skill') }}
      </div>
    </div>
  </div>
  <div class="flex-1 min-h-0 w-full overflow-y-auto">
    <div class="flex-1 min-h-0 max-w-[640px] mx-auto">
      <div class="flex flex-col overflow-auto h-full px-4 py-4">
        <div v-if="skill" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-white-main)] p-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="text-[var(--text-primary)] text-base font-semibold truncate">
                {{ skill.name }}
              </div>
              <div class="text-[var(--text-tertiary)] text-xs mt-1">
                {{ t('Created from current task') }}
              </div>
            </div>
            <span class="shrink-0 rounded-full bg-[var(--fill-tsp-gray-main)] px-2 py-1 text-xs text-[var(--text-secondary)]">
              {{ skill.scope || 'user' }}
            </span>
          </div>

          <div class="mt-4">
            <div class="text-[var(--text-primary)] text-sm font-medium mb-1">{{ t('Description') }}</div>
            <div class="text-[var(--text-secondary)] text-sm whitespace-pre-wrap">
              {{ skill.description || t('No description') }}
            </div>
          </div>

          <div v-if="skill.triggers?.length" class="mt-4">
            <div class="text-[var(--text-primary)] text-sm font-medium mb-2">{{ t('Triggers') }}</div>
            <div class="flex flex-wrap gap-2">
              <span v-for="trigger in skill.triggers" :key="trigger"
                class="rounded-full border border-[var(--border-light)] bg-[var(--fill-tsp-gray-main)] px-2 py-1 text-xs text-[var(--text-secondary)]">
                {{ trigger }}
              </span>
            </div>
          </div>

          <div class="mt-4 grid gap-2 text-xs text-[var(--text-tertiary)]">
            <div v-if="skill.created_from_session_id">
              {{ t('Source session') }}: <code>{{ skill.created_from_session_id }}</code>
            </div>
            <div v-if="skill.path">
              {{ t('Storage path') }}: <code>{{ skill.path }}</code>
            </div>
          </div>
        </div>

        <div v-else-if="toolContent.status === 'calling'" class="text-[var(--text-tertiary)] text-sm">
          {{ t('Tool is executing...') }}
        </div>
        <div v-else class="text-[var(--text-tertiary)] text-sm">
          {{ t('Waiting for result...') }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ToolContent } from '@/types/message';

const { t } = useI18n();

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

const skill = computed(() => {
  const result = props.toolContent.content?.result;
  if (!result || typeof result !== 'object') return null;
  if ('data' in result && result.data) return result.data;
  return result;
});
</script>
