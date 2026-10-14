<template>
  <section class="space-y-4" aria-label="数据连接器">
    <div class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5">
      <h2 class="text-lg font-semibold">数据连接器</h2>
      <p class="mt-2 text-sm text-[var(--text-secondary)]">连接数据源，让系统在对话中检索和读取数据，再用技能完成处理与分析。</p>
      <p class="mt-2 text-xs text-[var(--text-tertiary)]">下面列出已接入统一调用标准的连接器。已有 MCP 服务仍在 MCP 页面管理。</p>
      <RouterLink to="/plugins?tab=mcp" class="mt-3 inline-block text-sm underline">管理 MCP 服务</RouterLink>
    </div>
    <p v-if="loading" role="status">正在读取连接器…</p>
    <div v-else-if="error" role="alert">{{ error }} <button class="underline" @click="load">重试</button></div>
    <p v-else-if="!connectors.length" class="text-sm text-[var(--text-secondary)]">暂无已注册的数据连接器。</p>
    <article v-for="connector in connectors" :key="connector.id" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h3 class="font-medium">{{ connector.title }}</h3>
        <span class="rounded-md bg-[var(--fill-tsp-gray-main)] px-2 py-1 text-xs">{{ connector.auth === 'none' ? '公开数据 · 无需授权' : '需要账户授权' }}</span>
      </div>
      <p class="mt-2 text-sm text-[var(--text-secondary)]">{{ connector.description }}</p>
      <div class="mt-3 flex flex-wrap gap-2"><span v-for="operation in connector.operations" :key="operation.name" class="rounded-md border border-[var(--border-main)] px-2 py-1 text-xs">{{ operation.title }}</span></div>
      <ul class="mt-3 space-y-1 text-xs text-[var(--text-tertiary)]"><li v-for="limit in connector.limitations" :key="limit">{{ limit }}</li></ul>
      <p class="mt-4 text-sm">在对话中说明数据源和检索条件，系统可调用上述能力。</p>
      <details class="mt-3 text-xs text-[var(--text-secondary)]">
        <summary class="cursor-pointer">查看接入规范与能力详情</summary>
        <p class="mt-3">{{ connector.id }} · v{{ connector.version }} · {{ connector.schema_version }} · {{ connector.transport }}</p>
        <p class="mt-2">来源域名：{{ connector.domains.join('、') }}</p>
        <div v-for="operation in connector.operations" :key="operation.name" class="mt-3">
          <p>{{ operation.name }} — {{ operation.description }}</p>
          <pre class="mt-2 max-h-52 overflow-auto rounded bg-[var(--background-gray-main)] p-3">{{ JSON.stringify(operation.input_schema, null, 2) }}</pre>
        </div>
      </details>
    </article>
    <p class="text-xs text-[var(--text-tertiary)]">自定义 HTTP / MCP 适配器与账户授权接入尚在建设，当前目录不提供未实现的连接或下载操作。</p>
  </section>
</template>
<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { listDataConnectors, type DataConnector } from '@/api/dataConnector';
const connectors = ref<DataConnector[]>([]);
const loading = ref(false), error = ref('');
let disposed = false;
async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await listDataConnectors();
    if (!disposed) connectors.value = result;
  } catch {
    if (!disposed) error.value = '连接器目录暂时无法读取。';
  } finally { if (!disposed) loading.value = false; }
}
onMounted(load);
onUnmounted(() => { disposed = true; });
</script>
