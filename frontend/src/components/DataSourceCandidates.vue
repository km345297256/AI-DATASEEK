<template>
  <section class="sources">
    <button class="heading" type="button" :aria-expanded="open" @click="open = !open">{{ open ? '▾' : '▸' }} 来源与文件版本</button>
    <div v-if="open" class="body">
      <p class="hint">保存数据来源，将会话文件登记为可追溯版本。也可在对话中要求检索 NCBI 基因组，官方元数据会出现在这里；可在对话中要求下载指定版本的 FASTA 数据包（最多 32 MiB）。</p>
      <form class="form" @submit.prevent="save">
        <label>数据名称<input v-model="draft.title" required maxlength="200" :disabled="busy" placeholder="例如：某地区逐日气温数据" /></label>
        <label>公开来源链接<input v-model="draft.source_url" type="url" required maxlength="2048" :disabled="busy" placeholder="https://…" /></label>
        <label>外部标识<input v-model="draft.external_id" required maxlength="200" :disabled="busy" placeholder="例如：accession、DOI 或数据集编号" /></label>
        <p class="hint">只接受公开 HTTPS 链接；查询参数和片段不会保存。请勿填写凭据或本地路径。</p>
        <button type="submit" :disabled="busy">保存线索</button>
      </form>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <button type="button" :disabled="busy" @click="load(false)">刷新线索</button>
      <p v-if="!busy && loaded && !candidates.length" class="hint">还没有数据线索，可以先登记已知来源。</p>
      <article v-for="candidate in candidates" :key="candidate.candidate_id" class="candidate">
        <strong>{{ candidate.title }}</strong>
        <span class="tag">{{ candidate.provenance === 'ncbi_datasets' ? 'NCBI 官方元数据 · 下载文件见任务附件' : '用户提供来源 · 未核验' }}</span>
        <p v-if="candidate.discovery" class="hint">
          {{ candidate.discovery.assembly.source_database === 'refseq' ? 'RefSeq' : 'GenBank' }} · {{ candidate.discovery.assembly.assembly_level }} ·
          {{ candidate.discovery.assembly.reference_genome === null ? '参考标记未知' : candidate.discovery.assembly.reference_genome ? '参考基因组' : '未标为参考基因组' }}<br />
          官方报告长度：{{ candidate.discovery.assembly.total_sequence_length ?? '未知' }} bp；GC：{{ candidate.discovery.assembly.gc_percent ?? '未知' }}%（未独立计算）
        </p>
        <p>{{ candidate.external_id }}</p>
        <a :href="candidate.source_url" target="_blank" rel="noopener noreferrer">{{ candidate.source_url }}</a>
        <button type="button" :disabled="busy" @click="choose(candidate)">查看版本 / 登记文件</button>
        <button v-if="candidate.provenance === 'ncbi_datasets'" type="button" :disabled="busy" @click="inspectPlan(candidate)">查看获取计划</button>
      </article>
      <button v-if="nextCursor" type="button" :disabled="busy" @click="load(true)">加载更多线索</button>
      <div v-if="acquisitionPlan" class="candidate" role="status">
        <strong>{{ acquisitionPlan.accession }} · 获取尚未开始</strong>
        <p>计划格式：genomic FASTA；ZIP 传输上限：32 MiB。</p>
        <p class="hint">官方序列长度 {{ acquisitionPlan.reported_sequence_bases ?? '未知' }} bp 不等于下载字节数。此处只查看计划；请在对话中要求“下载这个数据”，由连接器执行下载并交付 ZIP 附件。</p>
      </div>
      <div v-if="selected" class="versions">
        <strong>{{ selected.title }} · 文件版本</strong>
        <p class="hint">选择本会话中的文件。每个文件最多 64 MiB，一次最多 16 个、合计 128 MiB。</p>
        <p v-if="!files.length" class="hint">请先在对话中上传文件，再回到这里登记。</p>
        <label v-for="file in files" :key="file.file_id" class="file">
          <input v-model="fileIds" type="checkbox" :value="file.file_id" :disabled="busy" />{{ file.filename || '未命名文件' }}
        </label>
        <button type="button" :disabled="busy || !fileIds.length" @click="register">登记所选文件版本</button>
        <p class="hint">登记会计算内容摘要；不代表文件来自该网站，也不自动挂载到分析环境。删除原文件后，此处的记录仍保留，但不保证文件可用。</p>
        <p v-if="versionsLoaded && !versions.length">尚未登记文件版本。</p>
        <article v-for="version in versions" :key="version.version_id" class="candidate">
          <strong>已登记摘要 · 来源关联未核验</strong>
          <p>登记时间：{{ new Date(version.acquisition.recorded_at * 1000).toLocaleString() }}</p>
          <details><summary>版本 {{ version.version_id.slice(0, 12) }} · {{ version.files.length }} 个文件</summary>
            <code>{{ version.version_id }}</code>
            <p v-for="file in version.files" :key="file.file_id">文件 {{ file.file_id }} · {{ file.size }} 字节<br /><code>SHA-256 {{ file.sha256 }}</code></p>
          </details>
        </article>
        <button v-if="versionCursor" type="button" :disabled="busy" @click="loadVersions(true)">加载更多版本</button>
      </div>
      <p v-if="busy" role="status">正在处理…</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import type { FileInfo } from '../api/file';
import { listCandidates, createCandidate, listVersions, registerVersion, getAcquisitionPlan, type AcquisitionPlan, type DataCandidate, type DataVersion } from '../api/dataCandidate';
const props = defineProps<{ sessionId: string; files: FileInfo[]; revision?: number }>();
const acquisitionPlan = ref<AcquisitionPlan | null>(null);
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
const open = ref(false), busy = ref(false), loaded = ref(false), versionsLoaded = ref(false);
const error = ref(''), notice = ref('');
const candidates = ref<DataCandidate[]>([]), versions = ref<DataVersion[]>([]);
const nextCursor = ref<string | null>(null), versionCursor = ref<string | null>(null);
const selected = ref<DataCandidate | null>(null), fileIds = ref<string[]>([]);
const draft = ref({ title: '', source_url: '', external_id: '' });
let generation = 0, controller: AbortController | null = null;
async function perform(work: (signal: AbortSignal, current: () => boolean) => Promise<void>) {
  if (busy.value) return;
  const ticket = generation;
  const session = props.sessionId;
  const current = () => generation === ticket && props.sessionId === session;
  const request = new AbortController();
  controller = request; busy.value = true; error.value = ''; notice.value = '';
  try { await work(request.signal, current); }
  catch { if (current()) error.value = '操作未确认成功。请检查来源为公开 HTTPS 链接、文件可访问且未超限，再刷新重试。'; }
  finally { if (current()) { busy.value = false; controller = null; } }
}
async function load(more = false) {
  await perform(async (signal, current) => {
    const page = await listCandidates(props.sessionId, more ? nextCursor.value : null, signal);
    if (!current()) return;
    candidates.value = [...new Map([...(more ? candidates.value : []), ...page.candidates].map(c => [c.candidate_id, c])).values()];
    nextCursor.value = page.next_cursor; loaded.value = true;
  });
}
async function save() {
  await perform(async (signal, current) => {
    const candidate = await createCandidate(props.sessionId, { ...draft.value }, signal);
    if (!current()) return;
    candidates.value = [candidate, ...candidates.value.filter(c => c.candidate_id !== candidate.candidate_id)];
    draft.value = { title: '', source_url: '', external_id: '' }; loaded.value = true;
    notice.value = '来源线索已保存；尚未下载或核验数据。';
  });
}
async function choose(candidate: DataCandidate) {
  if (busy.value) return;
  selected.value = candidate; versions.value = []; versionCursor.value = null;
  versionsLoaded.value = false; fileIds.value = [];
  await loadVersions();
}
async function loadVersions(more = false) {
  const candidate = selected.value;
  if (!candidate) return;
  await perform(async (signal, current) => {
    const page = await listVersions(props.sessionId, candidate.candidate_id, more ? versionCursor.value : null, signal);
    if (!current()) return;
    versions.value = [...new Map([...(more ? versions.value : []), ...page.versions].map(v => [v.version_id, v])).values()];
    versionCursor.value = page.next_cursor; versionsLoaded.value = true;
  });
}
async function inspectPlan(candidate: DataCandidate) {
  acquisitionPlan.value = null;
  await perform(async (signal, current) => {
    const plan = await getAcquisitionPlan(props.sessionId, candidate.candidate_id, signal);
    if (current()) acquisitionPlan.value = plan;
  });
}
async function register() {
  const candidate = selected.value;
  if (!candidate || !fileIds.value.length) return;
  await perform(async (signal, current) => {
    const version = await registerVersion(props.sessionId, candidate.candidate_id, [...fileIds.value], signal);
    if (!current()) return;
    versions.value = [version, ...versions.value.filter(v => v.version_id !== version.version_id)];
    fileIds.value = []; versionsLoaded.value = true;
    notice.value = '文件内容摘要已登记，来源关联尚未核验。';
  });
}
watch(() => props.revision, () => {
  if (refreshTimer) clearTimeout(refreshTimer);
  if (open.value) refreshTimer = setTimeout(() => { refreshTimer = null; void load(); }, 1500);
});
watch(open, value => { if (value && !loaded.value) void load(); });
watch(() => props.sessionId, () => {
  generation++; controller?.abort(); controller = null;
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = null; acquisitionPlan.value = null;
  busy.value = false; loaded.value = false; versionsLoaded.value = false;
  candidates.value = []; versions.value = []; selected.value = null; fileIds.value = [];
  nextCursor.value = null; versionCursor.value = null; error.value = ''; notice.value = '';
  draft.value = { title: '', source_url: '', external_id: '' };
  if (open.value) void load();
});
onUnmounted(() => { generation++; controller?.abort(); if (refreshTimer) clearTimeout(refreshTimer); });
</script>

<style scoped>
.sources { margin: 10px 0; border: 1px solid var(--border-main, #e5e7eb); border-radius: 12px; font-size: 13px; }
.heading { width: 100%; text-align: left; padding: 10px 14px; font-weight: 600; }
.body { padding: 0 14px 14px; display: grid; gap: 10px; }
.hint { color: var(--text-secondary, #6b7280); line-height: 1.6; }
.form, .versions { display: grid; gap: 10px; }
.form label { display: grid; gap: 4px; }
.form input { border: 1px solid var(--border-main, #ddd); padding: 8px; border-radius: 6px; background: transparent; }
.body button { border: 1px solid var(--border-main, #ddd); padding: 6px 10px; border-radius: 6px; justify-self: start; }
button:disabled { opacity: .5; cursor: wait; }
.candidate { border-top: 1px solid var(--border-main, #eee); padding-top: 10px; display: grid; gap: 6px; overflow-wrap: anywhere; }
.tag { color: var(--text-secondary, #6b7280); font-size: 12px; }
a { color: #2563eb; overflow-wrap: anywhere; }
.file { display: flex; gap: 8px; align-items: center; overflow-wrap: anywhere; }
code { font-size: 11px; overflow-wrap: anywhere; }
[role="alert"] { color: #b45309; }
</style>
