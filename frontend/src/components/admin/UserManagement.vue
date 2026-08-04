<template>
  <section class="admin-section">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div><h2 class="section-title">用户管理</h2><p class="section-description">管理账号状态、注册审批、角色与 Token 配额。</p></div>
      <div class="flex flex-wrap gap-2">
        <input v-model="query" class="filter-field" placeholder="搜索用户" @keydown.enter="search" />
        <select v-model="registrationFilter" class="filter-field" @change="search"><option value="">全部注册状态</option><option value="pending">待审批</option><option value="approved">已通过</option><option value="rejected">已拒绝</option></select>
        <button class="secondary-action" @click="search">搜索</button>
      </div>
    </div>

    <div class="mb-5 grid gap-3 md:grid-cols-2">
      <article v-for="quota in quotas" :key="quota.role" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
        <div class="mb-3 flex items-center justify-between gap-3"><div><div class="text-sm font-semibold">{{ quota.role }} 角色配额</div><div class="text-xs text-[var(--text-tertiary)]">初始 Token 与每日自动补增</div></div><button class="primary-action" @click="saveQuota(quota)">保存</button></div>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="quota-label">初始 Token<div class="mt-1 flex gap-2"><input :value="quota.initial_tokens ?? ''" class="quota-input" type="number" min="0" placeholder="不限量" @change="setQuotaValue(quota, 'initial_tokens', ($event.target as HTMLInputElement).value)" /><button class="mini-action" @click="quota.initial_tokens = null">不限量</button></div></label>
          <label class="quota-label">每日补增 Token<div class="mt-1 flex gap-2"><input :value="quota.daily_refill_tokens ?? ''" class="quota-input" type="number" min="0" placeholder="不限量" @change="setQuotaValue(quota, 'daily_refill_tokens', ($event.target as HTMLInputElement).value)" /><button class="mini-action" @click="quota.daily_refill_tokens = null">不限量</button></div></label>
        </div>
      </article>
    </div>

    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else class="overflow-x-auto">
      <table class="data-table min-w-[1240px]">
        <thead><tr><th>用户</th><th>角色</th><th>可用 Token</th><th>每日补增</th><th>账号状态</th><th>注册审批</th><th>任务空间</th><th>最后登录</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td><div class="font-medium text-[var(--text-primary)]">{{ user.fullname }}</div><div class="text-[11px] text-[var(--text-tertiary)]">{{ user.email }} · {{ user.id }}</div></td>
            <td><select :value="user.role" class="row-select" :disabled="user.id === currentUser?.id" @change="updateRole(user.id, ($event.target as HTMLSelectElement).value as UserRole)"><option v-for="role in roles" :key="role" :value="role">{{ role }}</option></select></td>
            <td><div class="flex items-center gap-2"><input :value="user.token_balance ?? ''" class="row-number" type="number" min="0" placeholder="不限量" @change="updateBalance(user.id, ($event.target as HTMLInputElement).value)" /><button class="mini-action" @click="setUnlimited(user.id)">不限量</button></div><div class="mt-1 text-[11px] text-[var(--text-tertiary)]">{{ tokenValue(user.token_balance) }}</div></td>
            <td><div class="flex items-center gap-2"><input :value="user.token_daily_refill_override ?? user.token_daily_refill ?? ''" class="row-number" type="number" min="0" placeholder="不限量" @change="updateDailyRefill(user.id, ($event.target as HTMLInputElement).value)" /><button v-if="user.token_daily_refill_override !== null && user.token_daily_refill_override !== undefined" class="mini-action" @click="clearDailyRefill(user.id)">跟随角色</button></div><div class="mt-1 text-[11px] text-[var(--text-tertiary)]">上次补增 {{ user.token_last_refill_date || '-' }}</div></td>
            <td><span class="status-chip">{{ user.is_active ? '启用' : '停用' }}</span></td>
            <td><div class="flex flex-wrap items-center gap-2"><span class="status-chip">{{ registrationLabel(user.registration_status) }}</span><button v-if="user.registration_status === 'pending'" class="mini-action" @click="review(user.id, 'approved')">通过</button><button v-if="user.registration_status === 'pending'" class="danger-action compact" @click="review(user.id, 'rejected')">拒绝</button></div><div v-if="user.registration_review_note" class="mt-1 max-w-[220px] truncate text-[11px] text-[var(--text-tertiary)]">{{ user.registration_review_note }}</div></td>
            <td>{{ user.member_count }} / {{ user.workspace_count }}</td><td>{{ formatTime(user.last_login_at) }}</td>
            <td class="text-right"><button v-if="user.is_active" class="danger-action" :disabled="user.id === currentUser?.id" @click="setActive(user.id, false)">停用</button><button v-else class="secondary-action compact" @click="setActive(user.id, true)">启用</button></td>
          </tr>
          <tr v-if="!users.length"><td colspan="9" class="empty-state">暂无用户</td></tr>
        </tbody>
      </table>
    </div>
    <AdminPager :page="page" :page-size="pageSize" :total="total" @change="changePage" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import AdminPager from './AdminPager.vue';
import { useAuth } from '@/composables/useAuth';
import { activateAdminUser, deactivateAdminUser, decideAdminUserRegistration, listAdminUsers, listRoleTokenQuotas, updateAdminUser, updateRoleTokenQuota, type AdminUserInfo, type RoleTokenQuotaInfo } from '@/api/admin';
import type { RegistrationStatus, UserRole } from '@/api/auth';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const { currentUser } = useAuth();
const roles: UserRole[] = ['admin', 'user', 'software'];
const pageSize = 10;
const page = ref(1), total = ref(0);
const query = ref('');
const registrationFilter = ref<RegistrationStatus | ''>('pending');
const loading = ref(false);
const users = ref<AdminUserInfo[]>([]);
const quotas = ref<RoleTokenQuotaInfo[]>([]);

async function load() { loading.value = true; try { const [result, quotaResult] = await Promise.all([listAdminUsers({ query: query.value || undefined, registration_status: registrationFilter.value || undefined, limit: pageSize, offset: (page.value - 1) * pageSize }), listRoleTokenQuotas()]); users.value = result.users; total.value = result.total; quotas.value = quotaResult.quotas; } catch (error) { showErrorToast(error instanceof Error ? error.message : '用户加载失败'); } finally { loading.value = false; } }
function search() { page.value = 1; void load(); }
function changePage(value: number) { page.value = value; void load(); }
function parseToken(value: string): number | null { return value.trim() === '' ? null : Math.max(0, Number(value || 0)); }
function setQuotaValue(quota: RoleTokenQuotaInfo, field: 'initial_tokens' | 'daily_refill_tokens', value: string) { quota[field] = parseToken(value); }
async function saveQuota(quota: RoleTokenQuotaInfo) { try { await updateRoleTokenQuota(quota.role, { initial_tokens: quota.initial_tokens, daily_refill_tokens: quota.daily_refill_tokens }); await load(); showSuccessToast('角色配额已保存'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '保存失败'); } }
async function review(id: string, status: 'approved' | 'rejected') { const decision_note = status === 'rejected' ? window.prompt('请输入拒绝原因（可选）') || undefined : undefined; try { await decideAdminUserRegistration(id, { status, decision_note }); await load(); showSuccessToast(status === 'approved' ? '注册已通过' : '注册已拒绝'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '审批失败'); } }
async function setActive(id: string, active: boolean) { try { active ? await activateAdminUser(id) : await deactivateAdminUser(id); await load(); showSuccessToast(active ? '用户已启用' : '用户已停用'); } catch (error) { showErrorToast(error instanceof Error ? error.message : '更新失败'); } }
async function updateRole(id: string, role: UserRole) { await updateUser(id, { role }, '用户角色已更新'); }
async function updateBalance(id: string, value: string) { await updateUser(id, { token_balance: parseToken(value) }, 'Token 余额已更新'); }
async function setUnlimited(id: string) { await updateUser(id, { token_balance: null }, 'Token 已设为不限量'); }
async function updateDailyRefill(id: string, value: string) { await updateUser(id, { token_daily_refill_override: parseToken(value) }, '每日补增已更新'); }
async function clearDailyRefill(id: string) { await updateUser(id, { token_daily_refill_override: null }, '已改为跟随角色'); }
async function updateUser(id: string, payload: Parameters<typeof updateAdminUser>[1], message: string) { try { const updated = await updateAdminUser(id, payload); users.value = users.value.map((item) => item.id === id ? updated : item); showSuccessToast(message); } catch (error) { showErrorToast(error instanceof Error ? error.message : '更新失败'); await load(); } }
function registrationLabel(value: RegistrationStatus) { return value === 'pending' ? '待审批' : value === 'approved' ? '已通过' : '已拒绝'; }
function tokenValue(value: number | null) { return value === null ? '不限量' : new Intl.NumberFormat('zh-CN').format(value); }
function formatTime(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '-'; }
onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }.section-title { @apply text-lg font-semibold; }.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }.filter-field { @apply h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-sm outline-none; }.row-select { @apply h-8 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-xs outline-none disabled:opacity-50; }.row-number { @apply h-8 w-28 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-xs outline-none; }.quota-label { @apply text-xs text-[var(--text-tertiary)]; }.quota-input { @apply h-9 min-w-0 flex-1 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-sm outline-none; }.primary-action { @apply rounded-lg bg-[var(--text-primary)] px-3 py-1.5 text-xs font-medium text-[var(--background-menu-white)]; }.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-50; }.secondary-action.compact { @apply px-3 py-1.5 text-xs; }.mini-action { @apply shrink-0 rounded-lg border border-[var(--border-main)] px-2 py-1 text-[11px] hover:bg-[var(--fill-tsp-white-light)]; }.danger-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-1.5 text-xs text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-light)] disabled:opacity-40; }.danger-action.compact { @apply px-2 py-1; }.status-chip { @apply rounded border border-[var(--border-main)] px-1.5 py-0.5 text-xs text-[var(--text-tertiary)]; }.empty-state { @apply py-10 text-center text-sm text-[var(--text-tertiary)]; }.data-table { @apply w-full text-left text-xs; }.data-table th { @apply px-3 py-2 text-[var(--text-tertiary)]; }.data-table td { @apply border-t border-[var(--border-main)] px-3 py-3 text-[var(--text-secondary)]; }
</style>
