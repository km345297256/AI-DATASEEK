<template>
  <section class="admin-section">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div><h2 class="section-title">MCP 管理</h2><p class="section-description">查看、搜索和管理全局、用户与工作区 MCP。</p></div>
      <div class="flex flex-wrap gap-2">
        <input v-model="query" class="filter-field" placeholder="搜索 MCP" @keydown.enter="search" />
        <select v-model="scopeFilter" class="filter-field" @change="search"><option value="">全部范围</option><option v-for="scope in scopes" :key="scope" :value="scope">{{ scope }}</option></select>
        <button class="secondary-action" @click="search">搜索</button>
      </div>
    </div>
    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else class="overflow-x-auto">
      <table class="data-table min-w-[1000px]">
        <thead><tr><th>名称</th><th>归属</th><th>范围</th><th>传输</th><th>风险</th><th>状态</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          <tr v-for="server in servers" :key="server.name">
            <td><div class="font-medium text-[var(--text-primary)]">{{ server.name }}</div><div class="max-w-[300px] truncate text-[11px] text-[var(--text-tertiary)]">{{ server.description || server.command || server.url || '-' }}</div></td>
            <td><div>{{ server.owner_fullname || '-' }}</div><div class="text-[11px] text-[var(--text-tertiary)]">{{ server.owner_user_id || server.user_id || '-' }}</div></td>
            <td><select :value="server.scope" class="row-select" @change="setScope(server.name, ($event.target as HTMLSelectElement).value as ResourceScope)"><option v-for="scope in scopes" :key="scope" :value="scope">{{ scope }}</option></select></td>
            <td>{{ server.transport }}</td><td>{{ server.risk_level }}</td><td><span class="status-chip">{{ server.enabled ? '启用' : '停用' }}</span></td>
            <td class="text-right"><button class="secondary-action compact mr-2" @click="setEnabled(server.name, !server.enabled)">{{ server.enabled ? '停用' : '启用' }}</button><button class="danger-action" @click="remove(server.name)">删除</button></td>
          </tr>
          <tr v-if="!servers.length"><td colspan="7" class="empty-state">暂无 MCP</td></tr>
        </tbody>
      </table>
    </div>
    <AdminPager :page="page" :page-size="pageSize" :total="total" @change="changePage" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import AdminPager from './AdminPager.vue';
import { deleteAdminMCPServer, listAdminMCPServers, updateAdminMCPServer, type AdminMCPServerInfo, type ResourceScope } from '@/api/admin';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const scopes: ResourceScope[] = ['global', 'user', 'workspace', 'private', 'shared'];
const pageSize = 10;
const page = ref(1), total = ref(0);
const query = ref('');
const scopeFilter = ref<ResourceScope | ''>('');
const loading = ref(false);
const servers = ref<AdminMCPServerInfo[]>([]);

async function load() { loading.value = true; try { const result = await listAdminMCPServers({ query: query.value || undefined, scope: scopeFilter.value || undefined, limit: pageSize, offset: (page.value - 1) * pageSize }); servers.value = result.servers; total.value = result.total; } catch (error) { showErrorToast(error instanceof Error ? error.message : 'MCP 加载失败'); } finally { loading.value = false; } }
function search() { page.value = 1; void load(); }
function changePage(value: number) { page.value = value; void load(); }
async function setEnabled(name: string, enabled: boolean) { try { await updateAdminMCPServer(name, { enabled }); await load(); showSuccessToast('MCP 状态已更新'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '更新失败'); } }
async function setScope(name: string, scope: ResourceScope) { try { await updateAdminMCPServer(name, { scope }); await load(); showSuccessToast('MCP 范围已更新'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '更新失败'); } }
async function remove(name: string) { if (!window.confirm(`确定删除 MCP“${name}”吗？`)) return; try { await deleteAdminMCPServer(name); await load(); showSuccessToast('MCP 已删除'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '删除失败'); } }
onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }.section-title { @apply text-lg font-semibold; }.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }.filter-field { @apply h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-sm outline-none; }.row-select { @apply h-8 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-xs outline-none; }.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)]; }.secondary-action.compact { @apply px-3 py-1.5 text-xs; }.danger-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-1.5 text-xs text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)]; }.empty-state { @apply py-10 text-center text-sm text-[var(--text-tertiary)]; }.data-table { @apply w-full text-left text-xs; }.data-table th { @apply px-3 py-2 text-[var(--text-tertiary)]; }.data-table td { @apply border-t border-[var(--border-main)] px-3 py-3 text-[var(--text-secondary)]; }.status-chip { @apply rounded border border-[var(--border-main)] px-1.5 py-0.5 text-xs text-[var(--text-tertiary)]; }
</style>
