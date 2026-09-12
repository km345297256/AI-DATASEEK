<template>
  <section class="flex min-h-0 flex-1 flex-col">
    <div class="flex flex-wrap items-center gap-3 border-b p-3 text-sm">
      <button v-if="memberId" :disabled="busy" class="rounded border px-2 py-1" @click="directory">返回归档目录</button>
      <button :disabled="busy || offset === 0" @click="turn(-200)">上一页</button>
      <span v-if="page">{{ page.mode === 'text' ? '文本行' : '成员' }} {{ offset + (page.total ? 1 : 0) }}–{{ offset + (page.mode === 'text' ? page.lines.length : page.resources.length) }} / {{ page.total }}</span>
      <button :disabled="busy || !page || offset + 200 >= page.total" @click="turn(200)">下一页</button>
    </div>
    <p class="px-4 py-2 text-xs text-gray-500">仅主动选择的小型 UTF-8 文本成员（≤256 KiB）会在隔离读取器内展开。不写入文件、不执行内容、不递归解包。归档仍限 64 MiB。</p>
    <p v-if="busy" role="status" class="p-4">正在验证归档资源…</p>
    <p v-if="error" role="alert" class="p-4 text-amber-700">{{ error }}</p>
    <p v-for="warning in warnings" :key="warning" class="px-4 py-1 text-xs text-amber-700">{{ warning }}</p>
    <div v-if="page?.mode === 'directory'" class="min-h-0 flex-1 overflow-auto">
      <table class="w-full border-collapse text-sm"><thead><tr><th class="p-2 text-left">成员</th><th>类型</th><th>字节</th><th>只读预览</th></tr></thead>
        <tbody><tr v-for="(resource, i) in page.resources" :key="i" class="border-t"><td class="max-w-md break-all p-2">{{ resource.name }}</td><td>{{ resource.kind }}</td><td>{{ resource.bytes }}</td><td><button v-if="resource.memberId" class="rounded border px-2 py-1" :disabled="busy" @click="openMember(resource.memberId)">查看文本</button><span v-else class="text-xs text-gray-500">当前格式或预算不支持</span></td></tr></tbody>
      </table>
    </div>
    <div v-else-if="page?.mode === 'text'" class="min-h-0 flex-1 overflow-auto p-4">
      <h3 class="mb-3 break-all font-medium">{{ page.name }}</h3>
      <div v-for="line in page.lines" :key="line[0]" class="flex gap-4 font-mono text-xs leading-6"><span class="w-12 shrink-0 text-right text-gray-400">{{ line[0] }}</span><span class="whitespace-pre-wrap break-all">{{ line[1] || ' ' }}</span></div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestVisualization } from '../runtime';
import { parseMemberPage, type MemberPage } from './archiveMembersData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), page = shallowRef<MemberPage>(), memberId = ref<string | null>(null), offset = ref(0);
const busy = ref(false), error = ref(''), warnings = ref<string[]>([]);
let version: string | undefined, directoryOffset = 0;
async function load() {
  const request = scope.begin(), requestedMember = memberId.value, requestedOffset = offset.value;
  busy.value = true; error.value = ''; page.value = undefined; warnings.value = [];
  try {
    if (requestedMember && !version) throw new Error('请重新打开当前归档目录，获取成员版本。');
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'table', row_offset: requestedOffset,
      ...(version ? { version } : {}), ...(requestedMember ? { member_id: requestedMember } : {}) }, request.signal);
    request.assertCurrent();
    if (result.kind !== 'table' || (version && result.version !== version)) throw new Error('归档版本已变化，请重新打开预览。');
    page.value = parseMemberPage(result.payload, result.metadata, requestedMember, requestedOffset);
    version = result.version; warnings.value = result.warnings.slice(0, 8);
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : '无法预览此成员。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
function openMember(id: string) {
  if (busy.value || !page.value?.resources.some(item => item.memberId === id)) return;
  directoryOffset = offset.value; memberId.value = id; offset.value = 0; void load();
}
function directory() { if (busy.value) return; memberId.value = null; offset.value = directoryOffset; void load(); }
function turn(delta: number) { if (busy.value || !page.value) return; const next = offset.value + delta; if (next < 0 || next >= page.value.total) return; offset.value = next; void load(); }
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version], () => {
  memberId.value = null; offset.value = 0; directoryOffset = 0; version = undefined; void load();
}, { immediate: true });
</script>
