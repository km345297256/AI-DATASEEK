<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-3 text-[var(--text-primary)]">
    <p class="mb-2 text-xs text-[var(--text-secondary)]">{{ note }}</p>
    <p v-if="loading" role="status" class="mb-2 text-sm">正在加载受控可视化预览…</p>
    <p v-if="error" role="alert" class="mb-2 rounded border border-red-200 p-3 text-sm text-red-700">{{ error }}</p>
    <slot />
  </section>
</template>
<script setup lang="ts">
defineProps<{ loading: boolean; error: string; note: string }>();
</script>
