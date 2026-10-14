<template>
  <div class="mt-2 flex flex-wrap gap-1 text-[11px] text-[var(--text-tertiary)]">
    <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ t(skillCategories[skillCategory(skill)]) }}</span>
    <template v-if="skill.format === 'data-skill-v1'">
      <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ t('Version') }} {{ skill.version }}</span>
      <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5" :title="t('Skill verification explanation')">{{ t('Skill not verified') }}</span>
    </template>
    <span v-else class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ t('Legacy skill') }}</span>
    <span v-for="domain in skill.domains || []" :key="domain" class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ domain }}</span>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import type { SkillInfo } from '@/api/skill';
import { skillCategories, skillCategory } from '@/utils/skillCatalog';
defineProps<{ skill: SkillInfo }>();
const { t } = useI18n();
</script>
