<template>
  <section class="admin-section">
    <div class="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="section-title">资源配置</h2>
        <p class="section-description">配置当前服务器的沙箱容量、预热数量和暂停沙箱回收策略。</p>
      </div>
      <button class="secondary-action" :disabled="loading || saving" @click="load">刷新</button>
    </div>

    <div v-if="loading" class="empty-state">加载中...</div>
    <template v-else-if="config">
      <div class="mb-5 grid gap-3 sm:grid-cols-3">
        <MetricCard label="运行中沙箱" :value="String(config.running_sandboxes)" />
        <MetricCard label="预热沙箱" :value="String(config.warm_sandboxes)" />
        <MetricCard label="暂停沙箱" :value="String(config.paused_sandboxes)" />
      </div>

      <form class="space-y-4" @submit.prevent="save">
        <div class="grid gap-4 lg:grid-cols-3">
          <label class="config-card">
            <span class="field-title">沙箱并发上限</span>
            <span class="field-description">同一时刻允许处于运行状态的沙箱总数。</span>
            <input
              v-model.number="form.sandbox_max_concurrent"
              class="number-field"
              type="number"
              min="1"
              max="64"
              step="1"
            />
            <span class="field-hint">范围 1–64；降低后不会中断正在运行的分析。</span>
          </label>

          <label class="config-card">
            <span class="field-title">预热沙箱数量</span>
            <span class="field-description">为未挂载数据集的通用分析预先准备 API 环境。</span>
            <input
              v-model.number="form.sandbox_pool_size"
              class="number-field"
              type="number"
              min="0"
              max="16"
              step="1"
            />
            <span class="field-hint">必须小于并发上限，为只读挂载的数据集分析保留容量。</span>
          </label>

          <label class="config-card">
            <span class="field-title">沙箱回收时间</span>
            <span class="field-description">会话沙箱暂停且持续空闲后自动销毁。</span>
            <div class="relative">
              <input
                v-model.number="form.sandbox_paused_destroy_after_minutes"
                class="number-field pr-14"
                type="number"
                min="1"
                max="10080"
                step="1"
              />
              <span class="unit-label">分钟</span>
            </div>
            <span class="field-hint">范围 1 分钟–7 天；最长约 30 秒进入下一次回收检查。</span>
          </label>
        </div>

        <div class="rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
          <div class="grid gap-3 text-xs sm:grid-cols-2">
            <div class="flex items-center justify-between gap-3">
              <span class="text-[var(--text-tertiary)]">浏览器进程</span>
              <span class="status-chip">{{ config.browser_on_demand ? '首次使用时加载' : '随沙箱启动' }}</span>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-[var(--text-tertiary)]">VNC 服务</span>
              <span class="status-chip">{{ config.vnc_on_demand ? '打开接管时加载' : '随沙箱启动' }}</span>
            </div>
          </div>
          <p class="mt-3 text-xs leading-5 text-[var(--text-tertiary)]">
            普通文件、Shell 和数据分析只启动沙箱 API；Chrome、Xvfb、x11vnc 与 Websockify 按实际功能首次使用启动。
          </p>
        </div>

        <div v-if="validationError" class="validation-error">{{ validationError }}</div>
        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border-main)] pt-4">
          <div class="text-xs text-[var(--text-tertiary)]">
            配置来源：{{ config.configuration_source === 'admin' ? '系统管理' : '部署默认值' }}
            <span v-if="config.updated_at"> · 更新于 {{ formatTime(config.updated_at) }}</span>
          </div>
          <button class="primary-action" type="submit" :disabled="saving || !dirty || Boolean(validationError)">
            {{ saving ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </form>
    </template>
    <div v-else class="empty-state">资源配置暂不可用</div>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, reactive, ref } from 'vue';
import {
  getSandboxResourceConfiguration,
  updateSandboxResourceConfiguration,
  type SandboxResourceConfiguration,
} from '@/api/admin';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const loading = ref(false);
const saving = ref(false);
const config = ref<SandboxResourceConfiguration | null>(null);
const form = reactive({
  sandbox_max_concurrent: 1,
  sandbox_pool_size: 0,
  sandbox_paused_destroy_after_minutes: 30,
});

const dirty = computed(() => Boolean(config.value) && (
  form.sandbox_max_concurrent !== config.value?.sandbox_max_concurrent
  || form.sandbox_pool_size !== config.value?.sandbox_pool_size
  || form.sandbox_paused_destroy_after_minutes !== config.value?.sandbox_paused_destroy_after_minutes
));

const validationError = computed(() => {
  if (!Number.isInteger(form.sandbox_max_concurrent) || form.sandbox_max_concurrent < 1 || form.sandbox_max_concurrent > 64) {
    return '沙箱并发上限必须是 1 到 64 之间的整数。';
  }
  if (!Number.isInteger(form.sandbox_pool_size) || form.sandbox_pool_size < 0 || form.sandbox_pool_size > 16) {
    return '预热沙箱数量必须是 0 到 16 之间的整数。';
  }
  if (form.sandbox_pool_size >= form.sandbox_max_concurrent) {
    return '预热沙箱数量必须小于沙箱并发上限。';
  }
  if (!Number.isInteger(form.sandbox_paused_destroy_after_minutes)
    || form.sandbox_paused_destroy_after_minutes < 1
    || form.sandbox_paused_destroy_after_minutes > 10080) {
    return '沙箱回收时间必须是 1 到 10080 之间的整数分钟。';
  }
  return '';
});

function applyConfig(value: SandboxResourceConfiguration) {
  config.value = value;
  form.sandbox_max_concurrent = value.sandbox_max_concurrent;
  form.sandbox_pool_size = value.sandbox_pool_size;
  form.sandbox_paused_destroy_after_minutes = value.sandbox_paused_destroy_after_minutes;
}

async function load() {
  loading.value = true;
  try {
    applyConfig(await getSandboxResourceConfiguration());
  } catch (error) {
    showErrorToast(error instanceof Error ? error.message : '资源配置加载失败');
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (validationError.value || saving.value) return;
  saving.value = true;
  try {
    applyConfig(await updateSandboxResourceConfiguration({ ...form }));
    showSuccessToast('资源配置已保存并动态生效');
  } catch (error) {
    showErrorToast(error instanceof Error ? error.message : '资源配置保存失败');
  } finally {
    saving.value = false;
  }
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN');
}

const MetricCard = defineComponent({
  props: { label: { type: String, required: true }, value: { type: String, required: true } },
  setup(props) {
    return () => h('div', { class: 'rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4' }, [
      h('div', { class: 'text-xs text-[var(--text-tertiary)]' }, props.label),
      h('div', { class: 'mt-2 text-2xl font-semibold' }, props.value),
    ]);
  },
});

onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }
.section-title { @apply text-lg font-semibold; }
.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }
.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50; }
.primary-action { @apply rounded-lg bg-[var(--text-primary)] px-4 py-2 text-sm text-[var(--background-menu-white)] disabled:cursor-not-allowed disabled:opacity-40; }
.config-card { @apply flex min-h-[210px] flex-col rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4; }
.field-title { @apply text-sm font-medium text-[var(--text-primary)]; }
.field-description { @apply mt-1 min-h-10 text-xs leading-5 text-[var(--text-tertiary)]; }
.number-field { @apply mt-4 h-11 w-full rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-base font-medium outline-none focus:border-[var(--text-tertiary)]; }
.field-hint { @apply mt-3 text-[11px] leading-5 text-[var(--text-tertiary)]; }
.unit-label { @apply pointer-events-none absolute bottom-3 right-3 text-xs text-[var(--text-tertiary)]; }
.status-chip { @apply shrink-0 rounded border border-[var(--border-main)] px-2 py-1 text-[11px] text-[var(--text-secondary)]; }
.validation-error { @apply rounded-lg border border-[var(--function-error)] px-3 py-2 text-xs text-[var(--function-error)]; }
.empty-state { @apply py-12 text-center text-sm text-[var(--text-tertiary)]; }
</style>
