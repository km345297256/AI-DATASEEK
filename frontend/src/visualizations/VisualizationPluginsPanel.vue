<template>
  <section class="mb-6 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
    <div class="mb-3 flex flex-wrap items-start justify-between gap-3">
      <div><h2 class="text-lg font-semibold text-[var(--text-primary)]">可视化插件</h2><p class="mt-1 text-sm leading-6 text-[var(--text-tertiary)]">由 Cordis 管理能力和生命周期。可分别起停同一格式的地图、曲线等视图；在文件预览中自由切换。当前为本机共享配置，保存后刷新仍有效。</p></div>
      <button type="button" class="shrink-0 rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm text-[var(--text-primary)] disabled:opacity-50" :disabled="loading || !!updating" @click="refresh">刷新目录</button>
    </div>
    <p class="mb-4 text-xs text-[var(--text-tertiary)]">统一可视化协议 · 按能力组合视图 · 只读文件 · 可信适配器按需加载 · 地图使用离线底图，无外部地图请求<span v-if="catalog"> · {{ catalog.plugins.length }} 个插件 · Cordis {{ catalog.revision.slice(0, 12) }}</span></p>
    <div v-if="error" role="alert" class="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{{ error }}</div>
    <p v-if="!catalog && loading" class="py-6 text-center text-sm text-[var(--text-tertiary)]">正在加载插件…</p>
    <div v-if="catalog" class="grid gap-3 lg:grid-cols-2">
      <article v-for="plugin in filteredPlugins" :key="plugin.id" class="min-w-0 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4" :data-plugin-id="plugin.id">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0"><h3 class="text-sm font-semibold text-[var(--text-primary)]">{{ plugin.name }}</h3><p class="mt-1 text-[11px] text-[var(--text-tertiary)]">{{ plugin.id }} · v{{ plugin.version }} · 协议 v{{ plugin.contract_version }}</p></div>
          <button type="button" role="switch" :aria-checked="plugin.enabled" :aria-label="`${plugin.enabled ? '停止' : '启动'} ${plugin.name}`" :disabled="!!updating" class="shrink-0 rounded-md border px-3 py-1.5 text-xs disabled:opacity-50" :class="plugin.enabled ? 'border-emerald-300 bg-emerald-50 text-emerald-800' : 'border-[var(--border-main)] text-[var(--text-secondary)]'" @click="toggle(plugin.id, !plugin.enabled)">{{ updating === plugin.id ? '保存中…' : plugin.enabled ? '已启动 · 停止' : '已停止 · 启动' }}</button>
        </div>
        <p class="mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ plugin.description }}</p>
        <div class="mt-3 flex flex-wrap gap-1 text-[11px] text-[var(--text-secondary)]"><span class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ viewKindLabel(plugin.view_kind) }}</span><span v-for="extension in plugin.extensions" :key="extension" class="rounded border border-[var(--border-main)] px-1.5 py-0.5">.{{ extension }}</span><span v-for="filename in plugin.filenames" :key="filename" class="rounded border border-[var(--border-main)] px-1.5 py-0.5">{{ filename }}</span></div>
        <p class="mt-2 text-[11px] text-[var(--text-tertiary)]">{{ plugin.capabilities.operations.map(operationLabel).join(' · ') }}</p>
        <p class="mt-2 text-[11px] text-[var(--text-tertiary)]">读取上限 {{ byteLabel(plugin.limits.max_input_bytes) }}{{ plugin.capabilities.input_mode === 'window' ? '（本次选区/时间窗累计；源文件大小另有限制）' : plugin.capabilities.input_mode === 'prefix' ? '（前缀抽样）' : plugin.capabilities.input_mode === 'page' ? '（每页；不限制原文件总大小）' : '' }} · {{ plugin.adapter }}</p>
        <p v-if="plugin.capabilities.operations.includes('job')" class="mt-2 text-xs text-[var(--text-tertiary)]">启用后不会自动分析；进入预览并明确启动才会运行，可取消。</p>
      </article>
    </div>
    <p v-if="catalog && !filteredPlugins.length" class="py-6 text-sm text-[var(--text-tertiary)]">没有匹配的可视化插件。</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useVisualizationCatalog } from './catalog';
import { viewKindLabel } from './contract';
const props = withDefaults(defineProps<{ query?: string }>(), { query: '' });
const { catalog, loading, error, updating, refresh, toggle } = useVisualizationCatalog();
const byteLabel = (bytes: number) => bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KiB` : `${Math.round(bytes / 1024 / 1024)} MiB`;
const operationLabel = (operation: string) => ({ bytes: '原始数据', page: '分页浏览', preview: '结构化预览', prepare: '结构准备', job: '后台任务' })[operation] || operation;
const filteredPlugins = computed(() => {
  const query = props.query.trim().toLowerCase();
  return (catalog.value?.plugins ?? []).filter((plugin) => !query || [plugin.name, plugin.id, plugin.description, ...plugin.extensions, ...plugin.filenames].join(' ').toLowerCase().includes(query));
});
onMounted(() => { void refresh(); });
</script>
