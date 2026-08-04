<template>
  <section class="admin-section">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div><h2 class="section-title">技能管理</h2><p class="section-description">查看、搜索和管理所有用户的技能。</p></div>
      <div class="flex flex-wrap gap-2">
        <input v-model="query" class="filter-field" placeholder="搜索技能" @keydown.enter="search" />
        <select v-model="scopeFilter" class="filter-field" @change="search"><option value="">全部范围</option><option v-for="scope in scopes" :key="scope" :value="scope">{{ scope }}</option></select>
        <button class="secondary-action" @click="search">搜索</button>
      </div>
    </div>
    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else class="overflow-x-auto">
      <table class="data-table min-w-[1000px]">
        <thead><tr><th>技能</th><th>归属</th><th>范围</th><th>路径</th><th>更新时间</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          <tr v-for="skill in skills" :key="skill.id">
            <td><div class="font-medium text-[var(--text-primary)]">{{ skill.name }}</div><div class="max-w-[300px] truncate text-[11px] text-[var(--text-tertiary)]">{{ skill.description || skill.triggers.join(', ') || '-' }}</div></td>
            <td><div>{{ skill.owner_fullname || '-' }}</div><div class="text-[11px] text-[var(--text-tertiary)]">{{ skill.owner_user_id || skill.user_id || '-' }}</div></td>
            <td><select :value="skill.scope" class="row-select" @change="setScope(skill.id, ($event.target as HTMLSelectElement).value as ResourceScope)"><option v-for="scope in scopes" :key="scope" :value="scope">{{ scope }}</option></select></td>
            <td class="max-w-[300px] truncate font-mono">{{ skill.path }}</td><td>{{ formatTime(skill.updated_at) }}</td>
            <td class="text-right"><button class="danger-action" @click="remove(skill.id, skill.name)">删除</button></td>
          </tr>
          <tr v-if="!skills.length"><td colspan="6" class="empty-state">暂无技能</td></tr>
        </tbody>
      </table>
    </div>
    <AdminPager :page="page" :page-size="pageSize" :total="total" @change="changePage" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import AdminPager from './AdminPager.vue';
import { deleteAdminSkill, listAdminSkills, updateAdminSkill, type AdminSkillInfo, type ResourceScope } from '@/api/admin';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const scopes: ResourceScope[] = ['global', 'user', 'workspace', 'private', 'shared'];
const pageSize = 10;
const page = ref(1), total = ref(0);
const query = ref('');
const scopeFilter = ref<ResourceScope | ''>('');
const loading = ref(false);
const skills = ref<AdminSkillInfo[]>([]);

async function load() { loading.value = true; try { const result = await listAdminSkills({ query: query.value || undefined, scope: scopeFilter.value || undefined, limit: pageSize, offset: (page.value - 1) * pageSize }); skills.value = result.skills; total.value = result.total; } catch (error) { showErrorToast(error instanceof Error ? error.message : '技能加载失败'); } finally { loading.value = false; } }
function search() { page.value = 1; void load(); }
function changePage(value: number) { page.value = value; void load(); }
async function setScope(id: string, scope: ResourceScope) { try { await updateAdminSkill(id, { scope }); await load(); showSuccessToast('技能范围已更新'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '更新失败'); } }
async function remove(id: string, name: string) { if (!window.confirm(`确定删除技能“${name}”吗？`)) return; try { await deleteAdminSkill(id); await load(); showSuccessToast('技能已删除'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '删除失败'); } }
function formatTime(value: string) { return value ? new Date(value).toLocaleString('zh-CN') : '-'; }
onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }.section-title { @apply text-lg font-semibold; }.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }.filter-field { @apply h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-sm outline-none; }.row-select { @apply h-8 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-xs outline-none; }.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)]; }.danger-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-1.5 text-xs text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)]; }.empty-state { @apply py-10 text-center text-sm text-[var(--text-tertiary)]; }.data-table { @apply w-full text-left text-xs; }.data-table th { @apply px-3 py-2 text-[var(--text-tertiary)]; }.data-table td { @apply border-t border-[var(--border-main)] px-3 py-3 text-[var(--text-secondary)]; }
</style>
