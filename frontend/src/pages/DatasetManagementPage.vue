<template>
  <main class="flex h-full min-w-0 w-full flex-col bg-[var(--background-gray-main)] text-[var(--text-primary)]">
    <header class="mobile-safe-top flex shrink-0 items-start gap-3 border-b border-[var(--border-main)] px-4 py-5 sm:px-7">
      <button v-if="!isLeftPanelShow" class="icon-action sm:!hidden" aria-label="打开导航" @click="toggleLeftPanel"><PanelLeft class="size-5" /></button>
      <div class="min-w-0 flex-1">
        <h1 class="text-2xl font-semibold tracking-tight">数据集管理</h1>
        <p class="mt-1.5 text-sm leading-6 text-[var(--text-secondary)]">从开放数据开始探索，或登记自己的本地数据。</p>
      </div>
      <button class="primary-action shrink-0" @click="router.push('/dataset/setup')"><Plus class="size-4" /><span class="hidden sm:inline">添加本地数据集</span><span class="sm:hidden">添加</span></button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-7 sm:py-6">
      <div class="mx-auto max-w-[1200px] space-y-5">
        <section class="rounded-2xl border border-[#c6dbd1] bg-[#f0f7f3] p-5 dark:border-[#405f50] dark:bg-[#22332b]">
          <div class="flex flex-wrap items-center justify-between gap-4">
            <div>
              <div class="flex items-center gap-2 text-sm font-semibold text-[#286d52] dark:text-[#a9cbbb]"><Globe2 class="size-4" />开放数据与本地数据 · 长期保留</div>
              <p class="mt-2 text-xs leading-6 text-[var(--text-secondary)]">本地登记自动保存在列表中，下次可直接进入探查。原始文件只读挂载。</p>
            </div>
            <div class="flex flex-wrap gap-4 sm:gap-7">
              <div><div class="text-2xl font-semibold">{{ curatedCount }}</div><div class="text-xs text-[var(--text-secondary)]">开放数据集</div></div>
              <div><div class="text-2xl font-semibold">{{ sourceCount('local') }}</div><div class="text-xs text-[var(--text-secondary)]">本地数据集</div></div>
              <div><div class="text-2xl font-semibold">{{ presets.length }}</div><div class="text-xs text-[var(--text-secondary)]">系统领域</div></div>
              <div><div class="text-2xl font-semibold">{{ formatDatasetBytes(totalBytes) }}</div><div class="text-xs text-[var(--text-secondary)]">已登记文件</div></div>
            </div>
          </div>
        </section>

        <section class="space-y-3" aria-label="筛选数据集">
          <div class="flex items-center gap-3">
            <label class="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-2">
              <Search class="size-4 shrink-0 text-[var(--text-tertiary)]" />
              <input v-model="query" aria-label="搜索数据集" placeholder="搜索名称、简介、来源…" class="min-w-0 flex-1 bg-transparent py-1 text-sm outline-none" />
            </label>
            <button class="icon-action" :disabled="loading" aria-label="刷新数据集" @click="load"><RefreshCw class="size-4" :class="loading ? 'animate-spin' : ''" /></button>
          </div>
          <div class="flex flex-wrap gap-2" aria-label="来源筛选">
            <button class="domain-filter" :class="!source ? 'selected' : ''" :aria-pressed="!source" @click="source = ''">所有来源</button>
            <button v-for="group in sourceGroups" :key="group.id" class="domain-filter" :class="source === group.id ? 'selected' : ''" :aria-pressed="source === group.id" @click="source = group.id">{{ group.label }} <span>{{ sourceCount(group.id) }}</span></button>
          </div>
          <div class="flex flex-wrap gap-2" aria-label="领域筛选">
            <button class="domain-filter" :class="!domain ? 'selected' : ''" :aria-pressed="!domain" @click="domain = ''">全部 <span>{{ datasets.length }}</span></button>
            <button v-for="preset in presets" :key="preset.id" class="domain-filter" :class="domain === preset.id ? 'selected' : ''" :aria-pressed="domain === preset.id" @click="domain = preset.id">{{ preset.name }} <span>{{ domainCount(preset.id) }}</span></button>
          </div>
        </section>

        <div v-if="loading" role="status" class="flex justify-center gap-2 py-16 text-sm text-[var(--text-secondary)]"><LoaderCircle class="size-5 animate-spin" />正在加载数据集…</div>
        <div v-else-if="error" role="alert" class="rounded-xl border border-[var(--border-main)] p-6 text-center text-sm"><p>{{ error }}</p><button class="secondary-action mx-auto mt-4" @click="load">重新加载</button></div>
        <template v-else>
          <p class="text-xs text-[var(--text-tertiary)]">{{ visibleDatasets.length }} 个数据集 · 点击「进入探查」开始分析</p>
          <div v-if="!visibleDatasets.length" class="rounded-xl border border-dashed border-[var(--border-main)] py-16 text-center text-sm text-[var(--text-secondary)]">没有匹配的数据集，请调整筛选或添加本地数据。</div>
          <div v-else class="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
            <article v-for="item in visibleDatasets" :key="item.dataset_id" :data-dataset-id="item.dataset_id" class="flex min-w-0 flex-col rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5 transition-shadow hover:shadow-md">
              <div class="flex items-center justify-between gap-2">
                <span class="rounded-md bg-[var(--background-gray-main)] px-2 py-1 text-[11px] font-medium">{{ domainName(item.domain) }}</span>
                <span class="flex items-center gap-1 text-[11px] text-[#286d52] dark:text-[#a9cbbb]"><ShieldCheck class="size-3.5" />只读</span>
              </div>
              <h2 class="mt-4 text-base font-semibold leading-6"><RouterLink v-if="canExplore(item)" :to="seekUrl(item)" class="hover:text-[#286d52]">{{ item.name }}</RouterLink><span v-else>{{ item.name }}</span></h2>
              <p class="mt-2 line-clamp-3 text-xs leading-6 text-[var(--text-secondary)]" :title="item.description">{{ item.description || '未提供简介' }}</p>
              <p v-if="catalogText(item, 'sample_scope')" class="mt-2 text-[11px] leading-5 text-[var(--text-tertiary)]">{{ catalogText(item, 'sample_scope') }}</p>
              <div class="mt-auto pt-4">
                <p class="truncate text-[11px] text-[var(--text-secondary)]" :title="catalogText(item, 'publisher') || item.data_center_name">{{ catalogText(item, 'publisher') || item.data_center_name }}</p>
                <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[var(--text-tertiary)]">
                  <span>{{ item.files.length }} 个文件</span><span>{{ formatDatasetBytes(datasetBytes(item)) }}</span><span>{{ item.data_type }}</span>
                </div>
                <div class="mt-3 flex min-h-5 flex-wrap gap-x-3 gap-y-1 text-[11px]">
                  <a v-if="sourceUrl(item)" :href="sourceUrl(item)" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-[#286d52] dark:text-[#a9cbbb]">官方来源<ExternalLink class="size-3" /></a>
                  <a v-if="licenseUrl(item)" :href="licenseUrl(item)" target="_blank" rel="noopener noreferrer" class="text-[var(--text-secondary)] underline underline-offset-2">{{ catalogText(item, 'license') }}</a>
                  <span v-else class="text-[var(--text-tertiary)]">{{ catalogText(item, 'license') || '本地登记 · 长期保留' }}</span>
                </div>
                <div class="mt-4 flex items-center gap-2 border-t border-[var(--border-main)] pt-4">
                  <RouterLink v-if="canExplore(item)" :to="seekUrl(item)" class="explore-action flex-1">进入探查<ArrowUpRight class="size-4" /></RouterLink>
                  <span v-else class="flex-1 text-xs text-[var(--text-tertiary)]">仅登记所有者可探查</span>
                  <button v-if="canManage(item)" class="icon-action" :aria-label="`编辑 ${item.name}`" @click="openEdit(item)"><Pencil class="size-4" /></button>
                  <button v-if="canManage(item)" class="icon-action" :disabled="archiving === item.dataset_id" :aria-label="`移除 ${item.name}`" @click="archive(item)"><Archive class="size-4" /></button>
                </div>
              </div>
            </article>
          </div>
        </template>
      </div>
    </div>

    <dialog ref="editDialog" class="edit-dialog" aria-labelledby="edit-title" @close="editing = null">
      <form v-if="editing" class="space-y-4 p-5" @submit.prevent="saveEdit">
        <div class="flex items-center justify-between"><h2 id="edit-title" class="text-lg font-semibold">编辑数据集</h2><button type="button" class="icon-action" aria-label="关闭编辑" :disabled="saving" @click="editDialog?.close()"><X class="size-4" /></button></div>
        <label class="edit-label">名称<input v-model.trim="editName" required maxlength="300" class="edit-field" /></label>
        <label class="edit-label">简介<textarea v-model.trim="editDescription" maxlength="4000" rows="4" class="edit-field" /></label>
        <label class="edit-label">领域<select v-model="editDomain" class="edit-field"><option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.name }}</option></select></label>
        <p class="text-xs text-[var(--text-tertiary)]">只更新登记信息，不修改文件、来源或许可。</p>
        <button type="submit" class="primary-action w-full" :disabled="saving">{{ saving ? '正在保存…' : '保存' }}</button>
      </form>
    </dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { Archive, ArrowUpRight, ExternalLink, Globe2, LoaderCircle, PanelLeft, Pencil, Plus, RefreshCw, Search, ShieldCheck, X } from 'lucide-vue-next';
import { archiveDatasetRegistration, listManagedDatasets, updateDatasetRegistration, type DataCenterDataset } from '@/api/dataset';
import { getDomainPresetCatalog, type DomainPreset } from '@/api/domainPreset';
import { useAuth } from '@/composables/useAuth';
import { useLeftPanel } from '@/composables/useLeftPanel';
import { catalogText, datasetBytes, datasetMatches, datasetSourceGroup, formatDatasetBytes, publicSourceUrl, type DatasetSourceGroup } from '@/utils/datasetCatalog';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const router = useRouter();
const { currentUser, isAdmin } = useAuth();
const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel();
const datasets = ref<DataCenterDataset[]>([]);
const presets = ref<DomainPreset[]>([]);
const query = ref('');
const domain = ref('');
const source = ref<DatasetSourceGroup | ''>('');
const sourceGroups: { id: DatasetSourceGroup; label: string }[] = [
  { id: 'local', label: '本地数据' },
  { id: 'international', label: '国际开放数据' },
  { id: 'scidb', label: 'ScienceDB' },
  { id: 'tpdc', label: '青藏高原数据中心' },
  { id: 'chemdc', label: '化学数据中心' },
  { id: 'ngdc', label: '国家基因组科学数据中心' },
];
const loading = ref(false);
const error = ref('');
const archiving = ref('');
const editing = ref<DataCenterDataset | null>(null);
const editDialog = ref<HTMLDialogElement>();
const editName = ref('');
const editDescription = ref('');
const editDomain = ref('general');
const saving = ref(false);
const visibleDatasets = computed(() => datasets.value.filter(item => (!source.value || datasetSourceGroup(item) === source.value) && datasetMatches(item, domain.value, query.value)));
const sourceCount = (id: DatasetSourceGroup) => datasets.value.filter(item => datasetSourceGroup(item) === id).length;
const curatedCount = computed(() => datasets.value.filter(item => item.metadata?.curated === true).length);
const totalBytes = computed(() => datasets.value.reduce((sum, item) => sum + datasetBytes(item), 0));
const domainCount = (id: string) => datasets.value.filter(item => (item.domain || 'general') === id).length;
const domainName = (id?: string) => presets.value.find(item => item.id === (id || 'general'))?.name || '通用数据分析';
const sourceUrl = (item: DataCenterDataset) => publicSourceUrl(catalogText(item, 'source_url'));
const licenseUrl = (item: DataCenterDataset) => publicSourceUrl(catalogText(item, 'license_url'));
const seekUrl = (item: DataCenterDataset) => `/dataset/seek/${encodeURIComponent(item.dataset_id)}`;
const canManage = (item: DataCenterDataset) => isAdmin.value || item.created_by === currentUser.value?.id;
const canExplore = (item: DataCenterDataset) => !item.created_by || item.created_by === currentUser.value?.id;

async function load() {
  if (loading.value) return;
  loading.value = true;
  error.value = '';
  try {
    const [catalog, first] = await Promise.all([getDomainPresetCatalog(), listManagedDatasets()]);
    presets.value = catalog.presets;
    const items = [...first.datasets];
    while (items.length < first.total) {
      const page = await listManagedDatasets(items.length);
      if (!page.datasets.length) break;
      items.push(...page.datasets);
    }
    datasets.value = items;
  } catch { error.value = '数据集加载失败，请检查本机服务后重试。'; }
  finally { loading.value = false; }
}

async function openEdit(item: DataCenterDataset) {
  editing.value = item;
  editName.value = item.name;
  editDescription.value = item.description;
  editDomain.value = item.domain || 'general';
  await nextTick();
  editDialog.value?.showModal();
}

async function saveEdit() {
  if (!editing.value || saving.value) return;
  saving.value = true;
  try {
    const result = await updateDatasetRegistration(editing.value.dataset_id, { name: editName.value, description: editDescription.value, domain: editDomain.value });
    datasets.value = datasets.value.map(item => item.dataset_id === result.dataset_id ? result : item);
    editDialog.value?.close();
    showSuccessToast('数据集信息已保存');
  } catch { showErrorToast('保存失败，请重试'); }
  finally { saving.value = false; }
}

async function archive(item: DataCenterDataset) {
  if (archiving.value || !window.confirm(`从列表移除「${item.name}」？这只归档登记，不会删除原始文件。`)) return;
  archiving.value = item.dataset_id;
  try {
    await archiveDatasetRegistration(item.dataset_id);
    datasets.value = datasets.value.filter(value => value.dataset_id !== item.dataset_id);
    showSuccessToast('已移除登记，原始文件保持不变');
  } catch { showErrorToast('移除失败，请重试'); }
  finally { archiving.value = ''; }
}

onMounted(load);
</script>

<style scoped>
.primary-action, .secondary-action, .explore-action { display: inline-flex; align-items: center; justify-content: center; gap: 0.45rem; border-radius: 0.6rem; padding: 0.6rem 0.85rem; min-height: 2.5rem; font-size: 0.8125rem; font-weight: 500; }
.primary-action { background: #286d52; color: white; }
.primary-action:hover { background: #20583f; }
.explore-action { background: #edf6f1; color: #2c7256; border: 1px solid #c6dfd1; transition: background-color 150ms ease, border-color 150ms ease, color 150ms ease; }
.explore-action:hover { background: #e0efe6; color: #235d47; border-color: #a8cdb9; }
.explore-action:focus-visible { outline: 2px solid #6b927f; outline-offset: 2px; }
.dark .explore-action { background: #263c31; color: #b7dcc7; border-color: #476453; }
.dark .explore-action:hover { background: #304b3b; border-color: #648971; }
.secondary-action, .icon-action { border: 1px solid var(--border-main); background: var(--background-menu-white); }
.icon-action { display: inline-flex; align-items: center; justify-content: center; width: 2.5rem; height: 2.5rem; border-radius: 0.6rem; flex-shrink: 0; }
.icon-action:hover { background: var(--fill-tsp-gray-main); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.domain-filter { padding: 0.45rem 0.75rem; border: 1px solid var(--border-main); border-radius: 0.6rem; font-size: 0.75rem; background: var(--background-menu-white); }
.domain-filter span { margin-left: 0.25rem; opacity: 0.65; }
.domain-filter.selected { color: white; background: #286d52; border-color: #286d52; }
.edit-dialog { margin: auto; width: min(30rem, calc(100vw - 2rem)); max-height: calc(100dvh - 2rem); border-radius: 1rem; border: 1px solid var(--border-main); color: var(--text-primary); background: var(--background-menu-white); }
.edit-dialog::backdrop { background: #0005; }
.edit-label { display: flex; flex-direction: column; gap: 0.4rem; font-size: 0.8125rem; }
.edit-field { width: 100%; padding: 0.65rem; border: 1px solid var(--border-main); border-radius: 0.5rem; background: var(--background-gray-main); }
</style>
