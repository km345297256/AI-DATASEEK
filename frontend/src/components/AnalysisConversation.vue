<template>
  <div class="analysis-conversation flex min-w-0 max-w-full flex-col gap-2">
    <template v-for="{ message, index } in displayEntries" :key="messageKey(message)">
      <div v-if="showMessage(message, index)" :data-message-key="messageKey(message)" class="analysis-message">
        <ChatMessage
          :message="message"
          :session-id="sessionId || undefined"
          :hide-header="isConsecutiveAssistant(messages, index)"
          :show-assistant-actions="!isLoading && presentation.latestAssistant === message"
          :allow-analysis-resume="canResumeAnalysis(index)"
          :task-summary-expanded="expanded.has(messageKey(message))"
          :show-product-button="allowProducts && !isLoading && presentation.latestAssistant === message"
          @resume-analysis="$emit('resumeAnalysis', index)"
          @tool-click="$emit('toolClick', $event)"
          @jupyter-opened="$emit('jupyterOpened', $event)"
          @task-summary-toggle="toggleSummary(message)"
          @show-product="$emit('showProduct')"
        />
      </div>
      <section v-if="deliveredImages(message).length" class="mb-3 mt-1 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]" aria-label="可视化成果">
        <header class="flex items-center justify-between border-b border-[var(--border-main)] px-3.5 py-2.5">
          <span class="flex items-center gap-2 text-xs font-medium"><ImageIcon class="size-3.5 text-[#2b7659]" />可视化成果</span>
          <span class="text-[10px] text-[var(--text-tertiary)]">点击图片查看与下载</span>
        </header>
        <div class="grid gap-3 p-3" :class="deliveredImages(message).length > 1 ? 'sm:grid-cols-2' : 'grid-cols-1'">
          <button v-for="file in deliveredImages(message)" :key="file.file_id" type="button"
            class="overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] text-left"
            @click="showFilePanel(file, deliveredFiles(message))">
            <img v-if="previewUrl(file)" :src="previewUrl(file)" :alt="file.filename" loading="lazy" class="max-h-[520px] w-full bg-white object-contain" />
            <span class="flex items-center justify-between gap-3 px-3 py-2 text-xs">
              <span class="truncate font-medium">{{ file.filename }}</span><span class="shrink-0 text-[var(--text-tertiary)]">查看成果</span>
            </span>
          </button>
        </div>
      </section>
    </template>
    <section v-if="completionAdvice?.recommendations.length && !isLoading" class="mt-4 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4" aria-label="推荐追问">
      <h3 class="mb-2 text-sm font-medium">推荐追问</h3>
      <div class="flex flex-col gap-2 text-sm text-[var(--text-secondary)]">
        <button v-for="(item, index) in completionAdvice.recommendations" :key="`${item}-${index}`" type="button"
          class="flex w-full items-center justify-between gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-2 text-left hover:bg-[var(--fill-tsp-white-dark)]"
          @click="$emit('followUp', item)">
          <span class="min-w-0 flex-1">{{ item }}</span><ChevronRight class="size-4 shrink-0" />
        </button>
      </div>
      <p v-if="completionAdvice.is_skill_candidate && completionAdvice.skill_reason" class="mt-3 text-xs text-[var(--text-tertiary)]">{{ completionAdvice.skill_reason }}</p>
    </section>
    <button v-if="completionAdvice?.shapefile_preview_available && !isLoading" type="button" :disabled="previewLoading"
      class="mt-3 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm disabled:opacity-50" @click="openShapefilePreview">
      {{ previewLoading ? '正在准备地图预览…' : '查看 Shapefile 地图' }}
    </button>
    <button v-if="completionAdvice?.molecular_preview_available && !isLoading" type="button"
      class="mt-3 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm" @click="openMolecularPreview">查看分子结构 3D 预览</button>
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ChevronRight, Image as ImageIcon } from 'lucide-vue-next';
import ChatMessage from './ChatMessage.vue';
import { API_CONFIG } from '../api/client';
import type { FileInfo } from '../api/file';
import { prepareShapefilePreview } from '../api/file';
import type { CompletionAdviceData } from '../types/event';
import { isConsecutiveAssistant, type Message, type ToolContent } from '../types/message';
import { ConversationPresentationIndex, currentTurnDeliveries, deliveredFiles, deliveredImages } from '../utils/analysisPresentation';
import { useFilePanel } from '../composables/useFilePanel';
import { showErrorToast } from '../utils/toast';

const props = withDefaults(defineProps<{
  messages: Message[];
  sessionId?: string | null;
  isLoading: boolean;
  completionAdvice?: CompletionAdviceData;
  canResumeAnalysis: (index: number) => boolean;
  messageKey: (message: Message) => number;
  allowProducts?: boolean;
}>(), { allowProducts: false });
defineEmits<{
  (event: 'resumeAnalysis', index: number): void;
  (event: 'toolClick', tool: ToolContent): void;
  (event: 'jupyterOpened', tool: ToolContent): void;
  (event: 'showProduct'): void;
  (event: 'followUp', question: string): void;
}>();
const { showFilePanel, beginFilePreview } = useFilePanel();
const previewLoading = ref(false);
const expanded = ref(new Set<number>());
const presentation = new ConversationPresentationIndex();
const displayEntries = computed(() => presentation.update(props.messages));
watch(() => props.sessionId, () => { expanded.value = new Set(); });
function toggleSummary(message: Message) {
  const key = props.messageKey(message);
  const next = new Set(expanded.value);
  if (next.has(key)) next.delete(key); else next.add(key);
  expanded.value = next;
}
function showMessage(message: Message, _index: number) {
  if (message.type !== 'step') return true;
  const summary = presentation.summaryFor(message);
  return !summary || expanded.value.has(props.messageKey(summary));
}
function previewUrl(file: FileInfo) {
  if (!file.file_url) return '';
  return /^https?:\/\//i.test(file.file_url) ? file.file_url : `${API_CONFIG.host}${file.file_url}`;
}
async function openShapefilePreview() {
  if (previewLoading.value) return;
  const files = currentTurnDeliveries(props.messages);
  const candidates = files.filter(file => /\.(shp|zip|rar)$/i.test(file.filename));
  const source = candidates[candidates.length - 1];
  if (!source) { showErrorToast('本轮没有可供地图预览的已交付文件。'); return; }
  const request = beginFilePreview();
  const currentSession = props.sessionId;
  previewLoading.value = true;
  try {
    if (/\.shp$/i.test(source.filename)) { request.show(source, files); return; }
    const prepared = await prepareShapefilePreview(source.file_id);
    const layer = prepared.layers.find(item => item.complete);
    const selected = layer?.components.find(file => /\.shp$/i.test(file.filename));
    if (currentSession !== props.sessionId) return;
    if (selected) request.show(selected, layer!.components);
    else if (request.isCurrent()) showErrorToast('没有找到完整的 Shapefile 文件组。');
  } catch (cause) {
    if (request.isCurrent() && currentSession === props.sessionId) showErrorToast(cause instanceof Error ? cause.message : '地图预览准备失败');
  } finally { previewLoading.value = false; }
}
function openMolecularPreview() {
  const files = currentTurnDeliveries(props.messages).filter(file => /\.(cif|pdb|ent|mol|sdf|xyz|mol2|vasp)$/i.test(file.filename) || /^(poscar|contcar)$/i.test(file.filename));
  const source = files[files.length - 1];
  if (source) showFilePanel(source, files);
  else showErrorToast('本轮没有可供分子结构预览的已交付文件。');
}
</script>

<style scoped>
.analysis-conversation, .analysis-message { overflow-x: clip; overflow-y: visible; }
.analysis-message { min-width: 0; max-width: 100%; overflow-wrap: anywhere; font-size: 14px; line-height: 1.65; }
.analysis-message :deep(.prose) { width: 100%; min-width: 0; max-width: 100% !important; overflow-wrap: anywhere; word-break: break-word; font-size: 14px !important; line-height: 1.7 !important; }
.analysis-message :deep(.prose p) { margin-top: .55em; margin-bottom: .55em; }
.analysis-message :deep(.prose pre) { max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; white-space: pre; overflow-wrap: normal; word-break: normal; }
.analysis-message :deep(.prose pre code) { white-space: inherit; overflow-wrap: inherit; word-break: inherit; }
.analysis-message :deep(.prose table) { display: block; width: 100%; max-width: 100%; overflow-x: auto; }
.analysis-message :deep(.prose a), .analysis-message :deep(.prose :not(pre) > code) { overflow-wrap: anywhere; word-break: break-word; }
.analysis-message :deep(.prose img), .analysis-message :deep(.prose video), .analysis-message :deep(.prose canvas), .analysis-message :deep(.prose svg) { max-width: 100%; height: auto; }
</style>
