<template>
  <div class="flex h-full min-h-0 flex-col bg-[var(--background-gray-main)] text-[var(--text-primary)]">
    <header class="mobile-safe-top flex items-center gap-3 border-b border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 pb-4 sm:px-6 sm:py-4">
      <button class="icon-button" title="返回分析首页" aria-label="返回分析首页" @click="router.push('/chat')">
        <ArrowLeft class="size-5" />
      </button>
      <div class="min-w-0 flex-1">
        <h1 class="text-xl font-semibold">数据集管理</h1>
        <p class="mt-0.5 text-xs text-[var(--text-tertiary)]">维护数据中心目录、元数据及执行节点上的只读存储位置</p>
      </div>
      <button class="command-button" @click="newDataset"><Plus class="size-4" />新建数据集</button>
    </header>

    <main class="grid min-h-0 flex-1 lg:grid-cols-[320px_minmax(0,1fr)]">
      <aside class="flex min-h-0 flex-col border-b border-[var(--border-main)] bg-[var(--background-menu-white)] lg:border-b-0 lg:border-r">
        <div class="p-4">
          <div class="relative">
            <Search class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--icon-secondary)]" />
            <input v-model="query" class="field pl-9" placeholder="搜索名称、数据中心或标签" @keyup.enter="reload" />
          </div>
        </div>
        <div class="min-h-[220px] flex-1 overflow-y-auto px-2 pb-2">
          <button
            v-for="item in datasets"
            :key="item.dataset_id"
            class="mb-1 flex w-full gap-3 rounded-md px-3 py-2.5 text-left hover:bg-[var(--fill-tsp-white-light)]"
            :class="selected?.dataset_id === item.dataset_id ? 'bg-[var(--fill-tsp-white-main)]' : ''"
            @click="selectDataset(item)"
          >
            <img v-if="item.preview_url" :src="item.preview_url" class="h-11 w-14 shrink-0 rounded object-cover" alt="" />
            <div v-else class="flex h-11 w-14 shrink-0 items-center justify-center rounded bg-[var(--background-gray-main)]"><Database class="size-5 text-[var(--icon-secondary)]" /></div>
            <div class="min-w-0 flex-1">
              <div class="truncate text-sm font-medium">{{ item.name }}</div>
              <div class="mt-1 truncate text-xs text-[var(--text-tertiary)]">{{ item.data_center_name }}</div>
            </div>
            <span class="mt-1 size-2 shrink-0 rounded-full" :class="item.enabled ? 'bg-emerald-500' : 'bg-gray-300'" />
          </button>
          <div v-if="!loading && !datasets.length" class="px-3 py-12 text-center text-sm text-[var(--text-tertiary)]">暂无数据集</div>
        </div>
        <div class="flex items-center justify-between border-t border-[var(--border-main)] px-4 py-3 text-xs text-[var(--text-tertiary)]">
          <span>共 {{ total }} 项</span>
          <div class="flex gap-1">
            <button class="icon-button small" title="上一页" :disabled="page === 1" @click="page--; reload()"><ChevronLeft class="size-4" /></button>
            <span class="px-2 py-1.5">{{ page }} / {{ pages }}</span>
            <button class="icon-button small" title="下一页" :disabled="page >= pages" @click="page++; reload()"><ChevronRight class="size-4" /></button>
          </div>
        </div>
      </aside>

      <section class="min-h-0 overflow-y-auto p-4 sm:p-6">
        <div v-if="form" class="mx-auto max-w-[1050px] space-y-7">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 class="text-lg font-semibold">{{ selected ? '编辑数据集' : '新建数据集' }}</h2>
              <p v-if="selected" class="mt-1 font-mono text-xs text-[var(--text-tertiary)]">{{ selected.dataset_id }}</p>
            </div>
            <div class="flex gap-2">
              <button v-if="selected" class="danger-button" title="删除数据集" @click="removeCurrent"><Trash2 class="size-4" />删除</button>
              <button class="command-button" :disabled="saving" @click="save"><Save class="size-4" />保存</button>
            </div>
          </div>

          <div class="section-grid">
            <label class="label sm:col-span-2">名称<input v-model="form.name" class="field mt-1.5" /></label>
            <label class="label">数据中心标识<input v-model="form.data_center_id" class="field mt-1.5" /></label>
            <label class="label">数据中心名称<input v-model="form.data_center_name" class="field mt-1.5" /></label>
            <label class="label sm:col-span-2">描述<textarea v-model="form.description" class="field mt-1.5 min-h-24 resize-y" /></label>
            <label class="label">时间范围<input v-model="form.temporal_coverage" class="field mt-1.5" placeholder="例如 2011-2020" /></label>
            <label class="label">空间范围<input v-model="form.spatial_coverage" class="field mt-1.5" /></label>
            <label class="label">数据类型<input v-model="form.data_type" class="field mt-1.5" /></label>
            <label class="label">标签<input v-model="tagsText" class="field mt-1.5" placeholder="逗号分隔" /></label>
            <label class="flex items-center gap-2 text-sm"><input v-model="form.enabled" type="checkbox" class="size-4" />在数据目录中启用</label>
          </div>

          <div>
            <h3 class="section-title">缩略图与元数据</h3>
            <div class="mt-3 grid gap-4 md:grid-cols-[220px_minmax(0,1fr)]">
              <button class="preview-upload" :disabled="!selected" @click="previewInput?.click()">
                <img v-if="selected?.preview_url" :src="selected.preview_url + '?v=' + previewVersion" class="h-full w-full object-cover" alt="数据集缩略图" />
                <span v-else class="flex flex-col items-center gap-2 text-xs text-[var(--text-tertiary)]"><ImagePlus class="size-6" />保存后上传缩略图</span>
              </button>
              <label class="label">结构化元数据（JSON）<textarea v-model="metadataText" class="field mt-1.5 min-h-40 font-mono text-xs" spellcheck="false" /></label>
              <input ref="previewInput" class="hidden" type="file" accept="image/png,image/jpeg,image/webp" @change="onPreview" />
            </div>
          </div>

          <div v-if="selected">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div><h3 class="section-title">数据文件</h3><p class="section-hint">文件夹上传会保留浏览器提供的相对目录结构</p></div>
              <div class="flex gap-2">
                <button class="secondary-button" @click="fileInput?.click()"><FileUp class="size-4" />上传文件</button>
                <button class="secondary-button" @click="folderInput?.click()"><FolderUp class="size-4" />上传文件夹</button>
              </div>
              <input ref="fileInput" class="hidden" type="file" multiple @change="onFiles($event, false)" />
              <input ref="folderInput" class="hidden" type="file" multiple webkitdirectory @change="onFiles($event, true)" />
            </div>
            <div class="mt-3 overflow-hidden rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)]">
              <div v-for="file in selected.files" :key="file.path" class="grid grid-cols-[minmax(0,1fr)_100px] gap-3 border-b border-[var(--border-main)] px-3 py-2 text-xs last:border-0">
                <span class="truncate font-mono">{{ file.path }}</span><span class="text-right text-[var(--text-tertiary)]">{{ formatBytes(file.size) }}</span>
              </div>
              <div v-if="!selected.files.length" class="p-5 text-center text-xs text-[var(--text-tertiary)]">尚未上传托管文件</div>
            </div>
          </div>

          <div v-if="selected">
            <h3 class="section-title">存储位置</h3>
            <p class="section-hint">路径属于选中的执行节点。Agent 只会获得只读挂载目录，不会获得任意宿主机路径权限。</p>
            <div class="mt-3 overflow-hidden rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)]">
              <div v-for="location in selected.locations" :key="location.location_id" class="flex items-start gap-3 border-b border-[var(--border-main)] px-3 py-3 last:border-0">
                <Server class="mt-0.5 size-4 shrink-0 text-[var(--icon-secondary)]" />
                <div class="min-w-0 flex-1">
                  <div class="text-sm">{{ nodeName(location.node_id) }} · {{ location.storage_type === 'managed_upload' ? '平台托管' : '服务器路径' }}</div>
                  <div class="mt-1 text-xs text-[var(--text-tertiary)]">{{ location.storage_type === 'managed_upload' ? '由平台管理存储位置' : `路径已安全登记 · 版本 ${location.version}` }}</div>
                </div>
                <span class="status-chip">只读</span>
                <button v-if="location.storage_type === 'host_path'" class="icon-button small" title="移除此位置" @click="removeLocation(location.location_id)"><X class="size-4" /></button>
              </div>
            </div>
            <div class="mt-3 grid gap-2 sm:grid-cols-[190px_minmax(0,1fr)_90px_auto]">
              <select v-model="locationForm.node_id" class="field"><option value="" disabled>选择执行节点</option><option v-for="node in nodes" :key="node.id" :value="node.id">{{ node.name }}</option></select>
              <input v-model="locationForm.source_path" class="field font-mono text-xs" placeholder="/data/archive/dataset-name" />
              <input v-model="locationForm.version" class="field" placeholder="版本" />
              <button class="secondary-button" @click="addLocation"><Link2 class="size-4" />登记路径</button>
            </div>
          </div>
        </div>
        <div v-else class="flex h-full min-h-72 items-center justify-center text-sm text-[var(--text-tertiary)]">选择一个数据集，或新建数据集</div>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowLeft, ChevronLeft, ChevronRight, Database, FileUp, FolderUp, ImagePlus, Link2, Plus, Save, Search, Server, Trash2, X } from 'lucide-vue-next';
import { listExecutionNodes, type ExecutionNodeInfo } from '@/api/admin';
import { addDatasetLocation, createAdminDataset, deleteAdminDataset, listAdminDatasets, removeDatasetLocation, updateAdminDataset, uploadDatasetFiles, uploadDatasetPreview, type DataCenterDataset, type DatasetPayload } from '@/api/dataset';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const router = useRouter();
const pageSize = 12;
const page = ref(1);
const total = ref(0);
const query = ref('');
const loading = ref(false);
const saving = ref(false);
const datasets = ref<DataCenterDataset[]>([]);
const selected = ref<DataCenterDataset | null>(null);
const nodes = ref<ExecutionNodeInfo[]>([]);
const form = ref<DatasetPayload | null>(null);
const tagsText = ref('');
const metadataText = ref('{}');
const previewVersion = ref(Date.now());
const fileInput = ref<HTMLInputElement | null>(null);
const folderInput = ref<HTMLInputElement | null>(null);
const previewInput = ref<HTMLInputElement | null>(null);
const locationForm = ref({ node_id: '', source_path: '', version: '1' });
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

const emptyForm = (): DatasetPayload => ({ name: '', data_center_id: '', data_center_name: '', description: '', temporal_coverage: '', spatial_coverage: '', data_type: '', tags: [], metadata: {}, enabled: true });
const setForm = (item: DataCenterDataset | null) => {
  form.value = item ? { name: item.name, data_center_id: item.data_center_id, data_center_name: item.data_center_name, description: item.description, temporal_coverage: item.temporal_coverage, spatial_coverage: item.spatial_coverage, data_type: item.data_type, tags: [...item.tags], metadata: { ...item.metadata }, enabled: item.enabled } : emptyForm();
  tagsText.value = item?.tags.join(', ') || '';
  metadataText.value = JSON.stringify(item?.metadata || {}, null, 2);
};
const selectDataset = (item: DataCenterDataset) => { selected.value = item; setForm(item); };
const newDataset = () => { selected.value = null; setForm(null); };

const reload = async () => {
  loading.value = true;
  try {
    const result = await listAdminDatasets({ query: query.value || undefined, limit: pageSize, offset: (page.value - 1) * pageSize });
    datasets.value = result.datasets; total.value = result.total;
    if (selected.value) {
      const fresh = result.datasets.find((item) => item.dataset_id === selected.value?.dataset_id);
      if (fresh) selectDataset(fresh);
    } else if (!form.value && result.datasets[0]) selectDataset(result.datasets[0]);
  } catch (error) { showErrorToast(error instanceof Error ? error.message : '数据集加载失败'); }
  finally { loading.value = false; }
};

const save = async () => {
  if (!form.value) return;
  saving.value = true;
  try {
    const metadata = JSON.parse(metadataText.value || '{}');
    if (!metadata || Array.isArray(metadata) || typeof metadata !== 'object') throw new Error('元数据必须是 JSON 对象');
    const payload = { ...form.value, tags: tagsText.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean), metadata };
    const item = selected.value ? await updateAdminDataset(selected.value.dataset_id, payload) : await createAdminDataset(payload);
    selected.value = item; setForm(item); await reload(); showSuccessToast('数据集已保存');
  } catch (error) { showErrorToast(error instanceof Error ? error.message : '保存失败'); }
  finally { saving.value = false; }
};
const removeCurrent = async () => {
  if (!selected.value || !window.confirm(`确定彻底删除数据集“${selected.value.name}”吗？托管文件也会被删除。`)) return;
  try { await deleteAdminDataset(selected.value.dataset_id); selected.value = null; form.value = null; await reload(); showSuccessToast('数据集已删除'); }
  catch (error) { showErrorToast(error instanceof Error ? error.message : '删除失败'); }
};
const onFiles = async (event: Event, folder: boolean) => {
  if (!selected.value) return;
  const input = event.target as HTMLInputElement; const files = Array.from(input.files || []); if (!files.length) return;
  const paths = files.map((file) => folder ? ((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name) : file.name);
  try { selected.value = await uploadDatasetFiles(selected.value.dataset_id, files, paths); setForm(selected.value); await reload(); showSuccessToast(`已上传 ${files.length} 个文件`); }
  catch (error) { showErrorToast(error instanceof Error ? error.message : '上传失败'); }
  finally { input.value = ''; }
};
const onPreview = async (event: Event) => {
  if (!selected.value) return; const input = event.target as HTMLInputElement; const file = input.files?.[0]; if (!file) return;
  try { selected.value = await uploadDatasetPreview(selected.value.dataset_id, file); previewVersion.value = Date.now(); await reload(); showSuccessToast('缩略图已更新'); }
  catch (error) { showErrorToast(error instanceof Error ? error.message : '缩略图上传失败'); }
  finally { input.value = ''; }
};
const addLocation = async () => {
  if (!selected.value || !locationForm.value.node_id || !locationForm.value.source_path) return showErrorToast('请选择节点并填写绝对路径');
  try { selected.value = await addDatasetLocation(selected.value.dataset_id, { ...locationForm.value, storage_type: 'host_path' }); locationForm.value.source_path = ''; await reload(); showSuccessToast('服务器路径已登记'); }
  catch (error) { showErrorToast(error instanceof Error ? error.message : '路径登记失败'); }
};
const removeLocation = async (locationId: string) => {
  if (!selected.value) return;
  try { selected.value = await removeDatasetLocation(selected.value.dataset_id, locationId); await reload(); }
  catch (error) { showErrorToast(error instanceof Error ? error.message : '移除失败'); }
};
const nodeName = (id: string) => nodes.value.find((node) => node.id === id)?.name || id;
const formatBytes = (size: number) => size < 1024 ? `${size} B` : size < 1024 ** 2 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 ** 2).toFixed(1)} MB`;

onMounted(async () => {
  try { nodes.value = (await listExecutionNodes({ limit: 100, offset: 0 })).nodes; } catch { nodes.value = []; }
  await reload();
});
</script>

<style scoped>
.field { width: 100%; min-height: 40px; border: 1px solid var(--border-main); border-radius: 6px; background: var(--background-menu-white); padding: 8px 10px; font-size: 14px; outline: none; }
.field:focus { border-color: var(--text-tertiary); }
.label { display: block; font-size: 13px; color: var(--text-secondary); }
.section-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.section-title { font-size: 14px; font-weight: 600; }
.section-hint { margin-top: 4px; font-size: 12px; color: var(--text-tertiary); }
.icon-button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 6px; color: var(--text-secondary); }
.icon-button:hover { background: var(--fill-tsp-white-light); }
.icon-button.small { width: 30px; height: 30px; }
.icon-button:disabled { opacity: .35; }
.command-button,.secondary-button,.danger-button { display: inline-flex; min-height: 38px; align-items: center; justify-content: center; gap: 7px; border-radius: 6px; padding: 0 13px; font-size: 13px; }
.command-button { background: var(--text-primary); color: var(--background-menu-white); }
.secondary-button { border: 1px solid var(--border-main); background: var(--background-menu-white); }
.danger-button { border: 1px solid #fecaca; color: #b91c1c; background: #fff; }
.preview-upload { height: 160px; overflow: hidden; border: 1px dashed var(--border-main); border-radius: 6px; background: var(--background-menu-white); }
.status-chip { border: 1px solid var(--border-main); border-radius: 999px; padding: 2px 7px; font-size: 11px; color: var(--text-tertiary); }
@media (max-width: 639px) { .section-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
