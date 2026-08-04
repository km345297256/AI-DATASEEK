<template>
  <section class="admin-section">
    <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="section-title">资源用量</h2>
        <p class="section-description">Token、会话、文件存储与执行节点资源概览；Token 仅统计，不设额度。</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <input v-model="startAt" type="datetime-local" class="filter-field" aria-label="开始时间" />
        <input v-model="endAt" type="datetime-local" class="filter-field" aria-label="结束时间" />
        <select v-model="unit" class="filter-field" aria-label="存储单位">
          <option v-for="item in units" :key="item" :value="item">{{ item }}</option>
        </select>
        <button class="secondary-action" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else-if="usage" class="grid gap-3 md:grid-cols-6">
      <MetricCard label="Token 总量" :value="number(usage.token_usage.total_tokens)" :hint="`${usage.token_usage.record_count} 条记录`" />
      <MetricCard label="输入 Token" :value="number(usage.token_usage.prompt_tokens)" hint="Prompt / Input" />
      <MetricCard label="输出 Token" :value="number(usage.token_usage.completion_tokens)" hint="Completion / Output" />
      <MetricCard label="分析会话" :value="number(usage.auth_usage.sessions_total)" hint="共享系统身份 · 不限 Token" />
      <MetricCard label="磁盘用量" :value="`${numeric(usage.server_usage.disk.used_percent)}%`" :hint="`${bytes(usage.server_usage.disk.used_bytes)} / ${bytes(usage.server_usage.disk.total_bytes)}`" />
      <MetricCard label="文件存储" :value="bytes(usage.server_usage.file_storage?.total_bytes)" :hint="`${String(usage.server_usage.file_storage?.provider || 'unknown')} · ${numeric(usage.server_usage.file_storage?.object_count)} 个对象`" />

      <div class="data-card md:col-span-3">
        <h3 class="card-title">按模型统计 Token</h3>
        <div v-if="!usage.token_usage.by_model.length" class="empty-inline">暂无数据</div>
        <div v-else class="overflow-x-auto">
          <table class="data-table min-w-[500px]">
            <thead><tr><th>模型</th><th>输入</th><th>输出</th><th>总量</th></tr></thead>
            <tbody>
              <tr v-for="row in usage.token_usage.by_model.slice(0, 8)" :key="row.model_name">
                <td>{{ row.model_name }}</td><td>{{ number(row.prompt_tokens) }}</td><td>{{ number(row.completion_tokens) }}</td><td>{{ number(row.total_tokens) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="data-card md:col-span-3">
        <h3 class="card-title">按工作区统计 Token</h3>
        <div v-if="!usage.token_usage_by_workspace.length" class="empty-inline">暂无工作区用量</div>
        <div v-else class="space-y-2">
          <div v-for="row in usage.token_usage_by_workspace.slice(0, 8)" :key="row.key" class="flex items-center justify-between gap-3 text-xs">
            <span class="min-w-0 truncate text-[var(--text-secondary)]">{{ row.label || row.key }}</span>
            <span class="shrink-0 font-medium">{{ number(row.total_tokens) }}</span>
          </div>
        </div>
      </div>

      <div class="data-card md:col-span-6">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="card-title mb-0">服务器与执行节点</h3>
          <span class="text-[11px] text-[var(--text-tertiary)]">数据生成于 {{ formatTime(usage.generated_at) }}</span>
        </div>
        <div class="mt-3 grid gap-3 text-xs sm:grid-cols-3">
          <div>CPU 负载：{{ loadAverage }}</div>
          <div>Backend RSS：{{ bytes(rssBytes) }}</div>
          <div>Docker：{{ numeric(usage.server_usage.docker.containers_running) }} / {{ numeric(usage.server_usage.docker.containers_total) }}</div>
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-2">
          <article v-for="node in usage.execution_nodes_usage" :key="node.id" class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-3">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0"><div class="truncate text-sm font-medium">{{ node.name }}</div><div class="truncate text-[11px] text-[var(--text-tertiary)]">{{ node.base_url || node.id }}</div></div>
              <span class="status-chip">{{ node.status }}</span>
            </div>
            <div class="mt-3 grid grid-cols-3 gap-2 text-[11px] text-[var(--text-secondary)]">
              <span>预热 {{ numeric(node.health.warm_sandboxes) }}</span>
              <span>运行 {{ numeric(node.health.running_sandboxes) }}</span>
              <span>CPU {{ optional(node.health.cpu_percent) }}%</span>
              <span>内存 {{ bytes(node.health.memory_used_bytes) }}</span>
              <span>上限 {{ numeric(node.capacity.max_sandboxes) }}</span>
              <span>{{ node.enabled ? '已启用' : '已停用' }}</span>
            </div>
            <div v-if="node.failure_reason" class="mt-2 truncate text-[11px] text-[var(--function-error)]">{{ node.failure_reason }}</div>
          </article>
          <div v-if="!usage.execution_nodes_usage.length" class="empty-inline md:col-span-2">暂无执行节点</div>
        </div>
      </div>
    </div>
    <div v-else class="empty-state">资源信息暂不可用</div>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from 'vue';
import { getResourceUsage, type ResourceUsageOverview } from '@/api/admin';
import { showErrorToast } from '@/utils/toast';

type ByteUnit = 'Bytes' | 'KB' | 'MB' | 'GB' | 'TB';
const units: ByteUnit[] = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
const unit = ref<ByteUnit>('MB');
const startAt = ref('');
const endAt = ref('');
const loading = ref(false);
const usage = ref<ResourceUsageOverview | null>(null);

const rssBytes = computed(() => numeric(usage.value?.server_usage.memory.process_max_rss_kb) * 1024);
const loadAverage = computed(() => {
  const value = usage.value?.server_usage.cpu.load_average;
  return Array.isArray(value) ? value.map(optional).join(' / ') : optional(value);
});

function numeric(value: unknown): number { return Number(value || 0); }
function optional(value: unknown): string { return value === null || value === undefined ? '-' : String(value); }
function number(value: number): string { return new Intl.NumberFormat('zh-CN').format(value || 0); }
function bytes(value: unknown): string {
  const count = numeric(value);
  const exponent = units.indexOf(unit.value);
  const converted = exponent === 0 ? count : count / (1024 ** exponent);
  return `${converted.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${unit.value}`;
}
function formatTime(value: string): string { return value ? new Date(value).toLocaleString('zh-CN') : '-'; }

async function load() {
  loading.value = true;
  try {
    usage.value = await getResourceUsage({
      start_at: startAt.value || undefined,
      end_at: endAt.value || undefined,
      include_sandboxes: false,
    });
  } catch (error) {
    showErrorToast(error instanceof Error ? error.message : '资源用量加载失败');
  } finally {
    loading.value = false;
  }
}

const MetricCard = defineComponent({
  props: { label: { type: String, required: true }, value: { type: String, required: true }, hint: { type: String, required: true } },
  setup(props) {
    return () => h('div', { class: 'rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4' }, [
      h('div', { class: 'text-xs text-[var(--text-tertiary)]' }, props.label),
      h('div', { class: 'mt-2 text-2xl font-semibold' }, props.value),
      h('div', { class: 'mt-1 truncate text-xs text-[var(--text-tertiary)]' }, props.hint),
    ]);
  },
});

onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }
.section-title { @apply text-lg font-semibold; }
.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }
.filter-field { @apply h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-xs outline-none; }
.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50; }
.data-card { @apply rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4; }
.card-title { @apply mb-3 text-xs font-medium text-[var(--text-tertiary)]; }
.empty-state { @apply py-12 text-center text-sm text-[var(--text-tertiary)]; }
.empty-inline { @apply py-5 text-center text-xs text-[var(--text-tertiary)]; }
.data-table { @apply w-full text-left text-xs; }
.data-table th { @apply px-2 py-2 text-[var(--text-tertiary)]; }
.data-table td { @apply border-t border-[var(--border-main)] px-2 py-2 text-[var(--text-secondary)]; }
.status-chip { @apply shrink-0 rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]; }
</style>
