<template>
  <section class="results-panel">
    <button class="results-heading" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
      <Database :size="18" />
      <span class="grow text-left"><strong>{{ result.title }}</strong><span class="ml-2 text-[var(--text-secondary)]">{{ result.resources.length }} 条数据</span></span>
      <ChevronDown :size="17" :class="expanded ? 'rotate-180' : ''" />
    </button>
    <div v-if="expanded" class="results-body">
      <p class="text-xs text-[var(--text-secondary)] mb-3">勾选要用于本次实验的数据，再导入沙盒。检索命中不代表已验证研究相关性。</p>
      <p v-if="!result.resources.length" class="py-3">没有匹配结果，请调整关键词后重试。</p>
      <label v-for="row in result.resources" :key="row.external_id" class="result-row">
        <input v-model="selected" type="checkbox" :value="row.external_id" :disabled="busy || isShare" :aria-label="`选择 ${row.external_id}`" />
        <span class="min-w-0"><span class="block text-sm font-medium break-words">{{ row.title }}</span>
          <span class="text-xs text-[var(--text-secondary)]">{{ row.external_id }}</span>
          <a :href="row.source_url" target="_blank" rel="noopener noreferrer" class="ml-3 text-xs underline" @click.stop>查看来源 ↗</a>
        </span>
      </label>
      <template v-if="result.resources.length && !isShare">
        <div class="flex items-center justify-between py-3 text-xs">
          <label class="flex items-center gap-2"><input type="checkbox" :checked="selected.length === result.resources.length" :disabled="busy" @change="toggleAll" />全选</label>
          <span>已选 {{ selected.length }} / {{ result.resources.length }}</span>
        </div>
        <p class="text-xs text-[var(--text-secondary)] mb-3">{{ isStudy ? '先查看实际文件，再选择下载。GEO 原始包与处理后数据分开标注；SRA 从 ENA 镜像获取原始 FASTQ。单文件最多 64 MiB。' : '下载 genomic FASTA ZIP 到沙盒，每包最多 32 MiB，每次最多选 3 个。' }}</p>
        <div class="flex flex-wrap gap-2">
          <button v-if="isStudy" class="action primary" :disabled="!selected.length || selected.length > 3 || busy || !sessionId" @click="loadOptions">{{ busy ? progress || '正在读取文件列表…' : '查看可下载文件' }}</button>
          <button v-else class="action primary" :disabled="!selected.length || selected.length > 3 || busy || !sessionId" @click="importSelected('download')"><Download :size="14" />{{ busy ? '正在下载并导入…' : '下载到沙盒' }}</button>
          <button class="action" :disabled="!selected.length || busy || !sessionId" @click="importSelected('catalog')">导入检索清单</button>
        </div>
        <div v-if="options.length" class="mt-3 rounded-lg border border-[var(--border-main)] p-3">
          <p class="text-sm font-medium">选择文件（每条数据最多展示 20 个文件）</p>
          <p class="text-xs text-[var(--text-secondary)] my-2">双端测序请同时选择 _1 与 _2 文件。保留压缩格式导入，不截取部分内容。</p>
          <div class="max-h-80 overflow-y-auto">
          <label v-for="file in options" :key="file.id" class="result-row">
            <input v-model="selectedFiles" type="checkbox" :value="file.id" :disabled="busy || !file.available" :aria-label="`选择文件 ${file.filename}`" />
            <span class="min-w-0 text-xs"><strong class="block break-all">{{ file.filename }}</strong>
              <span>{{ file.accession }} · {{ file.kind }} · {{ file.size == null ? '大小待下载时确认' : (file.size / 1048576).toFixed(2) + ' MiB' }}</span>
              <span v-if="file.md5" class="block">下载后核对官方 MD5</span>
              <span v-if="!file.available" class="block text-amber-700">{{ file.unavailable_reason }}</span>
              <span v-if="fileStates[file.id]" class="block" role="status">{{ fileStates[file.id] }}</span>
            </span>
          </label>
          </div>
          <button class="action primary mt-3" :disabled="busy || !selectedFiles.length" @click="downloadFiles">{{ busy ? progress : `下载所选 ${selectedFiles.length} 个文件到沙盒` }}</button>
        </div>
        <p v-if="error" role="alert" class="text-sm text-red-600 mt-3">{{ error }}</p>
        <div v-if="imported.length" class="mt-3 rounded-lg bg-[var(--background-gray-main)] p-3 text-sm">
          <p class="font-medium">已导入沙盒 · {{ imported.length }} 个文件</p>
          <p v-for="file in imported" :key="file.file_id" class="break-all mt-1">{{ file.filename }}</p>
          <button class="action mt-3" @click="prepareAnalysis">用这些文件分析 →</button>
          <p v-if="prepared" class="mt-2 text-xs">文件已放入输入框，请补充分析要求后发送。</p>
        </div>
      </template>
    </div>
  </section>
</template>
<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { ChevronDown, Database, Download } from 'lucide-vue-next';
import { apiClient, type ApiResponse } from '../api/client';
import type { FileInfo } from '../api/file';
import { eventBus } from '../utils/eventBus';
export interface ConnectorResultPanel {
  connector_id: string; title: string; can_download: boolean;
  resources: Array<{ external_id: string; title: string; source_url: string }>;
}
const props = defineProps<{ result: ConnectorResultPanel; toolCallId: string; sessionId?: string; isShare?: boolean }>();
const expanded = ref(true), busy = ref(false), prepared = ref(false);
const selected = ref<string[]>([]), imported = ref<FileInfo[]>([]), error = ref('');
interface DownloadOption { id: string; accession: string; filename: string; size: number | null; kind: string; md5?: string; available: boolean; unavailable_reason: string; }
const isStudy = computed(() => ['ncbi-geo', 'ncbi-sra'].includes(props.result.connector_id));
const options = ref<DownloadOption[]>([]), selectedFiles = ref<string[]>([]);
const fileStates = ref<Record<string, string>>({}), progress = ref('');
watch(selected, () => { options.value = []; selectedFiles.value = []; error.value = ''; });
function errorText(e: any): string { return e.response?.data?.detail || e.response?.data?.message || '请求未完成，请检查任务文件后重试。'; }
async function loadOptions() {
  busy.value = true; error.value = ''; progress.value = '';
  try {
    const response = await apiClient.post<ApiResponse<{options: DownloadOption[]; errors: string[]}>>(`/sessions/${props.sessionId}/data-connectors/download-options`, {
      tool_call_id: props.toolCallId, resource_ids: selected.value, mode: 'download',
    }, {timeout: 190000});
    options.value = response.data.data!.options; selectedFiles.value = [];
    error.value = response.data.data!.errors.join('；');
  } catch(e) { error.value = errorText(e); }
  finally { busy.value = false; }
}
async function downloadFiles() {
  busy.value = true; error.value = ''; prepared.value = false;
  const chosen = options.value.filter(file => selectedFiles.value.includes(file.id) && file.available);
  for (const [index, file] of chosen.entries()) {
    progress.value = `正在下载并导入 ${index + 1} / ${chosen.length}`;
    fileStates.value[file.id] = '正在下载、校验并导入…';
    try {
      const response = await apiClient.post<ApiResponse<{files: FileInfo[]; failures: Array<{message: string}>}>>(`/sessions/${props.sessionId}/data-connectors/import`, {
        tool_call_id: props.toolCallId, resource_ids: [file.accession], file_ids: [file.id], mode: 'download',
      }, {timeout: 120000});
      const data = response.data.data!;
      if (data.failures.length || !data.files.length) throw new Error(data.failures.map(item => item.message).join('；') || '导入未确认完成');
      imported.value = [...imported.value.filter(old => !data.files.some(f => f.file_id === old.file_id)), ...data.files];
      fileStates.value[file.id] = '已导入沙盒';
    } catch(e: any) {
      fileStates.value[file.id] = e.response ? errorText(e) : e.message || '下载失败，可重试';
    }
  }
  busy.value = false; progress.value = '';
}
function toggleAll() { selected.value = selected.value.length === props.result.resources.length ? [] : props.result.resources.map(row => row.external_id); }
async function importSelected(mode: 'download' | 'catalog') {
  if (busy.value || !props.sessionId || props.isShare) return;
  busy.value = true; error.value = ''; prepared.value = false;
  try {
    const response = await apiClient.post<ApiResponse<{ files: FileInfo[]; failures: Array<{id: string; message: string}> }>>(`/sessions/${props.sessionId}/data-connectors/import`, {
      tool_call_id: props.toolCallId, resource_ids: selected.value, mode,
    }, { timeout: 240000 });
    const data = response.data.data!;
    imported.value = data.files;
    error.value = data.failures.map(f => `${f.id}：${f.message}`).join('；');
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.response?.data?.message || '导入未确认完成，请检查任务文件后重试。';
  } finally { busy.value = false; }
}
function prepareAnalysis() {
  eventBus.emit('connector-analysis-draft', { sessionId: props.sessionId, files: imported.value });
  prepared.value = true;
}
</script>
<style scoped>
.results-panel { border: 1px solid var(--border-main); border-radius: 12px; background: var(--background-white-main); overflow: hidden; margin: 8px 0; }
.results-heading { display: flex; align-items: center; gap: 10px; width: 100%; padding: 14px; font-size: 14px; }
.results-body { padding: 0 14px 14px; }
.result-row { display: flex; align-items: start; gap: 10px; padding: 12px 0; border-top: 1px solid var(--border-light); cursor: pointer; }
.result-row input { margin-top: 4px; flex-shrink: 0; }
.action { display: inline-flex; align-items: center; justify-content: center; gap: 6px; border: 1px solid var(--border-main); border-radius: 8px; padding: 8px 12px; font-size: 13px; }
.action.primary { background: var(--text-primary); color: var(--background-white-main); }
.action:disabled { opacity: .45; cursor: not-allowed; }
</style>
