<template>
  <section class="admin-section">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="section-title">任务管理</h2>
        <p class="section-description">查看所有用户的数据分析任务及完整回放。</p>
      </div>
      <div class="flex flex-wrap gap-2">
        <input v-model="query" class="filter-field" placeholder="搜索任务、用户、标题" @keydown.enter="search" />
        <input v-model="userFilter" class="filter-field" placeholder="用户 ID" @keydown.enter="search" />
        <select v-model="statusFilter" class="filter-field" @change="search">
          <option value="">全部状态</option>
          <option value="pending">pending</option>
          <option value="running">running</option>
          <option value="waiting">waiting</option>
          <option value="completed">completed</option>
        </select>
        <button class="secondary-action" @click="search">搜索</button>
      </div>
    </div>

    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else class="overflow-x-auto">
      <table class="data-table min-w-[1050px]">
        <thead><tr><th>任务</th><th>任务 ID</th><th>用户</th><th>状态</th><th>Sandbox</th><th>最近消息</th><th>更新时间</th><th class="text-right">操作</th></tr></thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.session_id">
            <td><div class="font-medium text-[var(--text-primary)]">{{ task.title || '未命名任务' }}</div></td>
            <td><div>{{ task.session_id }}</div><div class="text-[11px] text-[var(--text-tertiary)]">{{ task.task_id || '-' }}</div></td>
            <td><div>{{ task.user_fullname || '未知用户' }}</div><div class="text-[11px] text-[var(--text-tertiary)]">{{ task.user_id }}</div></td>
            <td><span class="status-chip">{{ task.status }}</span></td>
            <td>{{ task.sandbox_id || '-' }}</td>
            <td class="max-w-[300px] truncate">{{ task.latest_message || '-' }}</td>
            <td>{{ formatTime(task.updated_at) }}</td>
            <td class="text-right"><button class="secondary-action compact" @click="openReplay(task.session_id)">查看</button></td>
          </tr>
          <tr v-if="!tasks.length"><td colspan="8" class="empty-state">暂无任务</td></tr>
        </tbody>
      </table>
    </div>
    <AdminPager :page="page" :page-size="pageSize" :total="total" @change="changePage" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import AdminPager from './AdminPager.vue';
import { listAdminTasks, type AdminTaskInfo, type TaskStatus } from '@/api/admin';
import { showErrorToast } from '@/utils/toast';

const router = useRouter();
const pageSize = 10;
const page = ref(1);
const total = ref(0);
const query = ref('');
const userFilter = ref('');
const statusFilter = ref<TaskStatus | ''>('');
const loading = ref(false);
const tasks = ref<AdminTaskInfo[]>([]);

async function load() {
  loading.value = true;
  try {
    const result = await listAdminTasks({
      query: query.value || undefined,
      user_id: userFilter.value || undefined,
      status: statusFilter.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    });
    tasks.value = result.tasks;
    total.value = result.total;
  } catch (error) {
    showErrorToast(error instanceof Error ? error.message : '任务加载失败');
  } finally {
    loading.value = false;
  }
}
function search() { page.value = 1; void load(); }
function changePage(value: number) { page.value = value; void load(); }
function openReplay(sessionId: string) { void router.push(`/chat/admin/tasks/${encodeURIComponent(sessionId)}/replay`); }
function formatTime(value: string) { return value ? new Date(value).toLocaleString('zh-CN') : '-'; }

onMounted(load);
</script>

<style scoped>
.admin-section { @apply rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5; }
.section-title { @apply text-lg font-semibold; }
.section-description { @apply mt-1 text-sm text-[var(--text-tertiary)]; }
.filter-field { @apply h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-2 text-sm outline-none; }
.secondary-action { @apply rounded-lg border border-[var(--border-main)] px-3 py-2 text-sm hover:bg-[var(--fill-tsp-white-light)]; }
.secondary-action.compact { @apply px-3 py-1.5 text-xs; }
.empty-state { @apply py-10 text-center text-sm text-[var(--text-tertiary)]; }
.data-table { @apply w-full text-left text-xs; }
.data-table th { @apply px-3 py-2 text-[var(--text-tertiary)]; }
.data-table td { @apply border-t border-[var(--border-main)] px-3 py-3 text-[var(--text-secondary)]; }
.status-chip { @apply rounded border border-[var(--border-main)] px-1.5 py-0.5 text-xs text-[var(--text-tertiary)]; }
</style>
