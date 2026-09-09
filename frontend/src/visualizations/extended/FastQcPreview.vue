<template>
  <section class="min-h-0 flex-1 overflow-auto p-4">
    <h3 class="mb-2 font-medium">FastQC / MultiQC 质控</h3>
    <p class="mb-3 text-sm text-gray-500">完整质控不会自动执行。点击后创建可取消的 AnalysisJob；结果保存到私有 Artifact Store，不发送给模型或外网。限64 MiB未压缩 FASTQ。</p>
    <div class="mb-4 flex gap-3 text-sm"><button class="rounded border px-4 py-2" :disabled="active || busy" @click="start">启动质控</button><button v-if="active" class="rounded border px-4 py-2" @click="cancel">取消任务</button><button v-if="job && !active" @click="refresh">刷新结果</button><span role="status" class="self-center">{{ statusLabel }}</span></div>
    <p v-if="error" role="alert" class="mb-3 text-amber-700">{{ error }}</p>
    <article v-for="section in sections" :key="section.name" class="mb-4 rounded border p-3">
      <h4 class="mb-2 font-medium">{{ section.name }} · {{ section.status }}</h4>
      <div class="max-h-64 overflow-auto"><table class="w-full text-xs"><thead><tr><th v-for="(column,i) in section.columns" :key="i" class="border p-1">{{ column }}</th></tr></thead><tbody><tr v-for="(row,i) in section.rows" :key="i"><td v-for="(cell,j) in row" :key="j" class="border p-1">{{ cell }}</td></tr></tbody></table></div>
    </article>
    <article v-if="summary" class="mb-4"><h4 class="font-medium">MultiQC 汇总</h4><div class="overflow-auto"><table class="text-xs"><thead><tr><th v-for="(column,i) in summary.columns" :key="i" class="border p-1">{{ column }}</th></tr></thead><tbody><tr v-for="(row,i) in summary.rows" :key="i"><td v-for="(cell,j) in row" :key="j" class="border p-1">{{ cell }}</td></tr></tbody></table></div></article>
    <p v-for="warning in warnings" :key="warning" class="text-xs text-amber-700">{{ warning }}</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { apiClient, BASE_URL } from '../../api/client';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { readBoundedBinary } from '../boundedBinary';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
interface Job { job_id: string; status: string }
interface Section { name: string; status: string; columns: string[]; rows: string[][] }
const job = ref<Job>(), busy = ref(false), error = ref(''), sections = ref<Section[]>([]), warnings = ref<string[]>([]), summary = ref<{ columns: string[]; rows: unknown[][] }>();
const active = computed(() => !!job.value && ['queued', 'running', 'cancelling'].includes(job.value.status));
const statusLabel = computed(() => ({ queued: '排队中', running: '运行中', cancelling: '正在取消', succeeded: '已完成', failed: '失败（请检查读取器依赖和 FASTQ 格式）', cancelled: '已取消', timed_out: '超时', interrupted: '服务中断' } as Record<string, string>)[job.value?.status ?? ''] ?? '尚未启动');
const path = `/files/${encodeURIComponent(props.file.file_id)}/visualization-jobs`;
const loads = usePreviewLoad(); let timer: ReturnType<typeof setTimeout> | undefined;
function schedule() { if (timer) clearTimeout(timer); if (active.value) timer = setTimeout(refresh, 1500); }
async function refresh() {
  const load = loads.begin();
  try {
    if (!job.value) { const response = await apiClient.get(path, { signal: load.signal }); load.assertCurrent(); job.value = response.data.data[0]; }
    else { const response = await apiClient.get(`${path}/${job.value.job_id}`, { signal: load.signal }); load.assertCurrent(); job.value = response.data.data; }
    if (job.value?.status === 'succeeded') {
      const response = await fetch(`${BASE_URL}${path}/${job.value.job_id}/result`, { signal: load.signal, credentials: 'same-origin' });
      if (!response.ok) { await response.body?.cancel(); throw new Error('质控结果已过期、文件已变化或插件已停用。'); }
      const bytes = await readBoundedBinary(response, 8 * 1024 * 1024 + 4096, load.signal); load.assertCurrent();
      const data = JSON.parse(new TextDecoder().decode(bytes)).data;
      if (data?.contract_version !== 2 || data.type !== 'fastqc' || !Array.isArray(data.sections) || data.sections.length > 32) throw new Error('质控结果协议无效。');
      sections.value = data.sections; summary.value = data.table; warnings.value = data.warnings;
    }
  } catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '质控任务暂不可用。'; }
  finally { if (load.isCurrent()) schedule(); }
}
async function start() {
  busy.value = true; error.value = ''; sections.value = []; summary.value = undefined;
  const load = loads.begin();
  try { const response = await apiClient.post(path, { plugin_id: props.plugin.id, options: { confirm: true } }, { signal: load.signal }); load.assertCurrent(); job.value = response.data.data; schedule(); }
  catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '启动失败。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
async function cancel() {
  if (!job.value) return;
  const load = loads.begin(); error.value = '';
  if (timer) clearTimeout(timer);
  try {
    await apiClient.post(`${path}/${job.value.job_id}/cancel`, {}, { signal: load.signal, headers: { 'X-Analysis-Job-Action': 'cancel' } });
    load.assertCurrent(); await refresh();
  } catch (e) {
    if (load.isCurrent()) { error.value = e instanceof Error ? e.message : '取消请求失败，请重试。'; schedule(); }
  }
}
onMounted(refresh);
onScopeDispose(() => {
  if (timer) clearTimeout(timer);
  if (active.value && job.value) void fetch(`${BASE_URL}${path}/${job.value.job_id}/cancel`, { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'X-Analysis-Job-Action': 'cancel' } }).catch(() => undefined);
});
</script>
