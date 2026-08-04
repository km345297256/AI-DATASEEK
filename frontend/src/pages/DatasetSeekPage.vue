<template>
  <div class="dataset-seek flex h-[100dvh] min-h-0 w-full overflow-hidden bg-[var(--background-white-main)] text-[var(--text-primary)]">
    <button
      v-if="mobileCatalogOpen"
      type="button"
      class="fixed inset-0 z-30 bg-black/25 lg:hidden"
      aria-label="关闭数据集"
      @click="mobileCatalogOpen = false"
    />

    <aside
      class="fixed inset-y-0 left-0 z-40 flex w-[min(88vw,320px)] shrink-0 flex-col border-r border-[var(--border-main)] bg-[var(--background-menu-white)] transition-transform duration-200 lg:static lg:w-[304px] lg:translate-x-0"
      :class="mobileCatalogOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <header class="mobile-safe-top flex h-16 shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-4">
        <ScanSearch class="size-5 shrink-0 text-[#226b51]" aria-hidden="true" />
        <h1 class="min-w-0 flex-1 truncate text-sm font-semibold">科学数据探查</h1>
      </header>

      <div class="min-h-0 flex-1 overflow-y-auto p-4">
        <div v-if="catalogLoading" class="flex h-40 items-center justify-center text-xs text-[var(--text-tertiary)]">
          <LoaderCircle class="mr-2 size-4 animate-spin" />正在加载数据集
        </div>

        <div v-else-if="dataset" class="space-y-5">
          <figure class="aspect-[16/10] w-full overflow-hidden rounded-xl border border-[var(--border-main)] bg-[#dfeae5] shadow-sm">
            <img
              :src="datasetCoverUrl"
              :alt="`${dataset.name}数据集封面`"
              class="h-full w-full object-cover"
              @error="handleDatasetCoverError"
            />
          </figure>

          <section>
            <div class="text-[10px] font-medium uppercase tracking-[0.14em] text-[#2b7659]">Dataset</div>
            <h2 class="mt-1.5 break-words text-base font-semibold leading-6">{{ dataset.name }}</h2>
          </section>

          <dl class="space-y-4 border-y border-[var(--border-main)] py-4">
            <div>
              <dt class="text-[11px] text-[var(--text-tertiary)]">外部数据集 ID</dt>
              <dd class="mt-1 break-all font-mono text-xs text-[var(--text-secondary)]">{{ dataset.external_id || '未提供' }}</dd>
            </div>
            <div>
              <dt class="text-[11px] text-[var(--text-tertiary)]">数据集摘要</dt>
              <dd class="mt-1.5 whitespace-pre-wrap break-words text-xs leading-5 text-[var(--text-secondary)]">{{ dataset.description || '未提供摘要' }}</dd>
            </div>
            <div>
              <dt class="text-[11px] text-[var(--text-tertiary)]">关键词</dt>
              <dd v-if="dataset.tags.length" class="mt-2 flex flex-wrap gap-1.5">
                <span
                  v-for="keyword in dataset.tags"
                  :key="keyword"
                  class="max-w-full truncate rounded-md bg-[#e8f3ee] px-2 py-1 text-[10px] text-[#286d52] dark:bg-[#26372f] dark:text-[#9bc8b4]"
                >
                  {{ keyword }}
                </span>
              </dd>
              <dd v-else class="mt-1 text-xs text-[var(--text-tertiary)]">未提供关键词</dd>
            </div>
          </dl>

          <section>
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-xs font-semibold">数据文件</h3>
              <span class="text-[10px] text-[var(--text-tertiary)]">{{ dataset.files.length }} 个</span>
            </div>
            <div v-if="dataset.files.length" class="mt-2.5 overflow-hidden rounded-lg border border-[var(--border-main)]">
              <div
                v-for="(file, index) in visibleDatasetFiles"
                :key="`${displayFileName(file.name)}-${index}`"
                class="flex items-center gap-2.5 border-b border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 py-2.5 last:border-b-0"
              >
                <FileText class="size-4 shrink-0 text-[#2b7659]" />
                <span class="min-w-0 flex-1 truncate text-xs font-medium" :title="displayFileName(file.name)">
                  {{ displayFileName(file.name) }}
                </span>
              </div>
            </div>
            <button
              v-if="hasMoreDatasetFiles"
              type="button"
              class="secondary-button mt-2.5 flex w-full justify-center"
              @click="loadMoreDatasetFiles"
            >
              <ChevronDown class="size-3.5" />
              加载更多（已显示 {{ visibleDatasetFiles.length }} / {{ dataset.files.length }}）
            </button>
            <div v-if="!dataset.files.length" class="mt-2.5 rounded-lg border border-dashed border-[var(--border-main)] px-3 py-6 text-center text-xs text-[var(--text-tertiary)]">
              暂无可展示的文件名
            </div>
            <p class="mt-2 text-[10px] leading-4 text-[var(--text-tertiary)]">仅显示文件名，服务器存储路径不会在页面中公开。</p>
          </section>
        </div>

        <div v-else class="flex h-48 flex-col items-center justify-center gap-3 text-center">
          <Database class="size-6 text-[var(--icon-tertiary)]" />
          <div>
            <p class="text-sm font-medium">无法读取本次数据集</p>
            <p class="mt-1 text-xs text-[var(--text-tertiary)]">请通过第三方系统重新提交数据集</p>
          </div>
        </div>
      </div>
    </aside>

    <main class="flex min-w-0 flex-1 flex-col bg-[var(--background-gray-main)]">
      <header class="mobile-safe-top flex h-16 shrink-0 items-center gap-3 border-b border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 sm:px-5">
        <button type="button" class="icon-button lg:hidden" aria-label="打开数据集" @click="mobileCatalogOpen = true"><PanelLeft class="size-5" /></button>
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-semibold">{{ dataset?.name || '科学数据探查' }}</div>
          <div class="mt-0.5 flex items-center gap-2 text-[10px] text-[var(--text-tertiary)]">
            <span class="truncate">{{ dataset?.data_type || '正在加载数据集' }}</span><span>·</span><span class="truncate">{{ selectedProfile?.name || 'AI-DataSeek 默认 Agent' }}</span>
          </div>
        </div>
        <AgentSelector class="hidden sm:block" />
      </header>

      <div ref="timelineRef" class="min-h-0 flex-1 overflow-y-auto scroll-smooth">
        <div class="mx-auto flex min-h-full w-full max-w-[800px] flex-col px-4 pb-8 pt-5 sm:px-6 sm:pt-7">
          <div v-if="messages.length === 0" class="flex flex-1 flex-col justify-center py-8 sm:py-12">
            <div v-if="dataset" class="max-w-2xl">
              <div class="flex size-11 items-center justify-center rounded-lg bg-[#e4f0ea] text-[#226b51]"><ScanSearch class="size-5" /></div>
              <div class="mt-5 text-xs font-medium text-[#2b7659]">科学数据探查</div>
              <h1 class="mt-2 text-xl font-semibold leading-8 sm:text-2xl">围绕选定数据集开展智能问答</h1>
              <p class="mt-2.5 max-w-xl text-[13px] leading-6 text-[var(--text-secondary)]">Agent 将围绕“{{ dataset.name }}”及其完整数据内容开展分析。</p>
              <div class="mt-7">
                <div class="mb-2.5 flex items-center justify-between gap-3">
                  <span class="text-xs font-medium text-[var(--text-secondary)]">推荐问题</span>
                  <span v-if="suggestedQuestionsLoading" class="flex items-center gap-1.5 text-[10px] text-[var(--text-tertiary)]">
                    <LoaderCircle class="size-3 animate-spin" />正在生成
                  </span>
                </div>

                <div v-if="suggestedQuestionsLoading" class="grid gap-2 sm:grid-cols-2" aria-label="正在生成推荐问题">
                  <div
                    v-for="index in 4"
                    :key="index"
                    class="min-h-16 animate-pulse rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3"
                  >
                    <div class="h-3 w-4/5 rounded bg-[var(--fill-tsp-white-dark)]" />
                    <div class="mt-2 h-3 w-2/5 rounded bg-[var(--fill-tsp-white-dark)]" />
                  </div>
                </div>

                <div
                  v-else-if="suggestedQuestionsError"
                  class="flex min-h-28 flex-col items-center justify-center rounded-lg border border-dashed border-[var(--border-main)] bg-[var(--background-menu-white)] px-5 py-5 text-center"
                >
                  <CircleAlert class="size-5 text-amber-600" />
                  <p class="mt-2 text-xs text-[var(--text-secondary)]">{{ suggestedQuestionsError }}</p>
                  <button type="button" class="secondary-button mt-3 flex" @click="loadSuggestedQuestions">
                    <RefreshCw class="size-3.5" />重新生成
                  </button>
                </div>

                <div v-else-if="suggestedQuestions.length === 4" class="grid gap-2 sm:grid-cols-2">
                  <button
                    v-for="question in suggestedQuestions"
                    :key="question"
                    type="button"
                    class="min-h-16 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm leading-5 transition-colors hover:border-[#6b927f] hover:bg-[#f6faf8] dark:hover:bg-[#27342f]"
                    @click="askSuggestion(question)"
                  >
                    {{ question }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div v-else class="flex flex-col gap-2">
            <template v-for="(message, index) in messages" :key="`${message.type}-${index}`">
              <div class="dataset-chat-message">
                <ChatMessage :message="message" :session-id="sessionId || undefined" :hide-header="isConsecutiveAssistant(messages, index)" />
              </div>
              <div
                v-if="imageArtifacts(message).length"
                class="mb-3 mt-1 overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)]"
              >
                <div class="flex items-center justify-between border-b border-[var(--border-main)] px-3.5 py-2.5">
                  <div class="flex items-center gap-2 text-xs font-medium">
                    <ImageIcon class="size-3.5 text-[#2b7659]" />
                    可视化成果
                  </div>
                  <span class="text-[10px] text-[var(--text-tertiary)]">点击图片查看与下载</span>
                </div>
                <div class="grid gap-3 p-3" :class="imageArtifacts(message).length > 1 ? 'sm:grid-cols-2' : 'grid-cols-1'">
                  <button
                    v-for="file in imageArtifacts(message)"
                    :key="file.file_id"
                    type="button"
                    class="group/artifact overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] text-left"
                    @click="showFilePanel(file, attachmentFiles(message))"
                  >
                    <img
                      v-if="artifactPreviewUrl(file)"
                      :src="artifactPreviewUrl(file)"
                      :alt="file.filename"
                      class="max-h-[520px] w-full bg-white object-contain transition-transform duration-200 group-hover/artifact:scale-[1.01]"
                    />
                    <div class="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                      <span class="truncate font-medium">{{ file.filename }}</span>
                      <span class="shrink-0 text-[var(--text-tertiary)]">查看成果</span>
                    </div>
                  </button>
                </div>
              </div>
            </template>
            <div v-if="isLoading" class="mt-3 flex items-center gap-2 text-sm text-[var(--text-tertiary)]">
              <LoaderCircle class="size-4 animate-spin" />
              <span>{{ loadingStatus || 'Agent 正在分析数据集...' }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="mobile-safe-bottom shrink-0 border-t border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-3 sm:px-6">
        <div class="mx-auto w-full max-w-[800px]">
          <div v-if="dataset" class="mb-2 flex items-center gap-2 text-[11px] text-[var(--text-tertiary)]">
            <Database class="size-3.5" />
            <span class="shrink-0 font-medium text-[var(--text-secondary)]">当前数据集</span>
            <span class="truncate">{{ dataset.name }}</span>
            <span class="ml-auto hidden shrink-0 rounded bg-[#e8f3ee] px-1.5 py-0.5 text-[#286d52] sm:block">已选择</span>
          </div>
          <div class="relative flex items-end gap-2">
            <button
              type="button"
              class="history-button"
              :class="historyOpen ? 'border-[#6b927f] bg-[#eef6f2] text-[#225f48] dark:bg-[#26372f]' : ''"
              aria-label="历史任务"
              title="历史任务"
              @click="toggleHistory"
            >
              <History class="size-4" />
              <span class="hidden text-xs font-medium sm:inline">历史任务</span>
            </button>

            <div
              v-if="historyOpen"
              class="absolute bottom-[calc(100%+10px)] left-0 z-30 flex max-h-[420px] w-[min(88vw,360px)] flex-col overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-xl"
            >
              <div class="flex items-center justify-between border-b border-[var(--border-main)] px-4 py-3">
                <div>
                  <div class="text-sm font-semibold">历史任务</div>
                  <div class="mt-0.5 text-[10px] text-[var(--text-tertiary)]">当前数据集的分析记录</div>
                </div>
                <button type="button" class="inline-flex h-8 items-center gap-1 rounded-md bg-[#225f48] px-2.5 text-xs text-white hover:bg-[#194d39]" @click="newConversationFromHistory">
                  <Plus class="size-3.5" />新任务
                </button>
              </div>
              <div class="min-h-0 flex-1 overflow-y-auto p-2">
                <div v-if="historyLoading" class="flex h-28 items-center justify-center text-xs text-[var(--text-tertiary)]">
                  <LoaderCircle class="mr-2 size-4 animate-spin" />正在读取历史任务
                </div>
                <div v-else-if="historySessions.length === 0" class="flex h-28 flex-col items-center justify-center gap-2 text-xs text-[var(--text-tertiary)]">
                  <History class="size-5" />暂无历史任务
                </div>
                <template v-else>
                  <button
                    v-for="item in historySessions"
                    :key="item.session_id"
                    type="button"
                    class="group/history mb-1 w-full rounded-lg border px-3 py-2.5 text-left transition-colors last:mb-0"
                    :class="item.session_id === sessionId ? 'border-[#9bbdad] bg-[#f1f7f4] dark:bg-[#26372f]' : 'border-transparent hover:bg-[var(--fill-tsp-white-light)]'"
                    @click="openHistorySession(item.session_id)"
                  >
                    <div class="flex items-start gap-2.5">
                      <div class="mt-1.5 size-2 shrink-0 rounded-full" :class="historyStatusClass(item.status)" />
                      <div class="min-w-0 flex-1">
                        <div class="truncate text-xs font-medium">{{ item.title || '数据集分析任务' }}</div>
                        <p class="mt-1 line-clamp-2 text-[11px] leading-4 text-[var(--text-tertiary)]">{{ item.latest_message || '任务已创建，暂无回答' }}</p>
                        <div class="mt-1.5 flex items-center gap-1 text-[10px] text-[var(--text-tertiary)]">
                          <Clock3 class="size-3" />{{ formatHistoryTime(item.latest_message_at) }}
                        </div>
                      </div>
                      <ChevronRight class="mt-2 size-3.5 shrink-0 text-[var(--icon-tertiary)] transition-transform group-hover/history:translate-x-0.5" />
                    </div>
                  </button>
                </template>
              </div>
            </div>

            <div class="flex min-h-[54px] min-w-0 flex-1 items-end gap-2 rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 py-2 shadow-sm focus-within:border-[#6b927f]">
              <textarea
                v-model="inputMessage"
                rows="1"
                class="max-h-36 min-h-9 min-w-0 flex-1 resize-none bg-transparent py-1.5 text-sm leading-6 outline-none placeholder:text-[var(--text-tertiary)]"
                placeholder="针对当前数据集提问..."
                :disabled="isLoading || !dataset"
                @compositionstart="inputComposing = true"
                @compositionend="inputComposing = false"
                @keydown="handleInputKeydown"
              />
              <button v-if="isLoading" type="button" class="send-button" aria-label="停止任务" title="停止任务" @click="stop"><Square class="size-4 fill-current" /></button>
              <button v-else type="button" class="send-button" :disabled="!inputMessage.trim() || !dataset" aria-label="发送" title="发送" @click="submit"><ArrowUp class="size-4" /></button>
            </div>
          </div>
          <p class="mt-1.5 text-center text-[10px] text-[var(--text-tertiary)]">回答由 Agent 基于本次关联数据目录生成，分析结果应结合数据质量与来源信息进行验证。</p>
        </div>
      </div>
    </main>
  </div>
  <FilePanel />
  <SessionFileList :session-id="sessionId || undefined" />
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { ArrowUp, ChevronDown, ChevronRight, CircleAlert, Clock3, Database, FileText, History, Image as ImageIcon, LoaderCircle, PanelLeft, Plus, RefreshCw, ScanSearch, Square } from 'lucide-vue-next';
import datasetDefaultCover from '@/assets/dataset-default-cover.png';
import AgentSelector from '@/components/AgentSelector.vue';
import ChatMessage from '@/components/ChatMessage.vue';
import FilePanel from '@/components/FilePanel.vue';
import SessionFileList from '@/components/SessionFileList.vue';
import { createSession, getSession, chatWithSession, stopSession } from '@/api/agent';
import { API_CONFIG } from '@/api/client';
import { generateDatasetSuggestedQuestions, getDataCenterDataset, listDatasetChatSessions, type DataCenterDataset, type DatasetChatSession } from '@/api/dataset';
import type { FileInfo } from '@/api/file';
import { useAgentProfile } from '@/composables/useAgentProfile';
import { useFilePanel } from '@/composables/useFilePanel';
import { showErrorToast } from '@/utils/toast';
import { failRunningSteps, findCurrentTurnRunningStep, findCurrentTurnStep } from '@/utils/chatTimeline';
import { isConsecutiveAssistant, type AttachmentsContent, type Message, type MessageContent, type StepContent, type ToolContent } from '@/types/message';
import type { AgentSSEEvent, ErrorEventData, MessageEventData, PlanEventData, StepEventData, TitleEventData, ToolEventData } from '@/types/event';
import { SessionStatus } from '@/types/response';

const DATASET_STORAGE_KEY_PREFIX = 'ai-dataseek:dataset-seek:session';
const DATASET_FILE_BATCH_SIZE = 200;

const route = useRoute();
const { selectedProfileId, selectedProfile, refreshProfiles } = useAgentProfile();
const { showFilePanel } = useFilePanel();
const dataset = ref<DataCenterDataset>();
const selectedDatasetId = ref('');
const visibleFileCount = ref(DATASET_FILE_BATCH_SIZE);
const visibleDatasetFiles = computed(() => (dataset.value?.files || []).slice(0, visibleFileCount.value));
const hasMoreDatasetFiles = computed(() => visibleDatasetFiles.value.length < (dataset.value?.files.length || 0));
const inputComposing = ref(false);
const datasetCoverFailed = ref(false);
const datasetCoverUrl = computed(() => {
  if (!dataset.value?.preview_url || datasetCoverFailed.value) return datasetDefaultCover;
  return dataset.value.preview_url;
});
const catalogLoading = ref(true);
const inputMessage = ref('');
const messages = ref<Message[]>([]);
const sessionId = ref<string | null>(null);
const lastEventId = ref<string>();
const isLoading = ref(false);
const loadingStatus = ref('');
const mobileCatalogOpen = ref(false);
const suggestedQuestions = ref<string[]>([]);
const suggestedQuestionsLoading = ref(false);
const suggestedQuestionsError = ref('');
const historyOpen = ref(false);
const historyLoading = ref(false);
const historySessions = ref<DatasetChatSession[]>([]);
const timelineRef = ref<HTMLElement>();
const currentPlan = ref<PlanEventData>();
const lastTool = ref<ToolContent>();
let cancelChat: (() => void) | null = null;

watch(() => dataset.value?.preview_url, () => {
  datasetCoverFailed.value = false;
}, { flush: 'sync' });

watch(() => dataset.value?.dataset_id, () => {
  visibleFileCount.value = DATASET_FILE_BATCH_SIZE;
}, { flush: 'sync' });

function datasetStorageKey() {
  return `${DATASET_STORAGE_KEY_PREFIX}:${selectedDatasetId.value}`;
}

watch(messages, async () => {
  await nextTick();
  timelineRef.value?.scrollTo({ top: timelineRef.value.scrollHeight, behavior: 'smooth' });
}, { deep: true });

function handleDatasetCoverError() {
  if (datasetCoverUrl.value !== datasetDefaultCover) datasetCoverFailed.value = true;
}

function displayFileName(name: string) {
  const parts = name.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts[parts.length - 1] || '未命名文件';
}

function loadMoreDatasetFiles() {
  const total = dataset.value?.files.length || 0;
  visibleFileCount.value = Math.min(visibleFileCount.value + DATASET_FILE_BATCH_SIZE, total);
}

function startUserTurn() {
  failRunningSteps(messages.value, false);
  currentPlan.value = undefined;
  lastTool.value = undefined;
}

function handleMessage(data: MessageEventData) {
  if (data.role === 'user') startUserTurn();
  messages.value.push({ type: data.role, content: { ...data } as MessageContent });
  if (data.attachments?.length) {
    messages.value.push({ type: 'attachments', content: { ...data } as AttachmentsContent });
  }
}

function handleTool(data: ToolEventData) {
  const tool = { ...data } as ToolContent;
  if (lastTool.value?.tool_call_id === tool.tool_call_id) Object.assign(lastTool.value, tool);
  else {
    const runningStep = findCurrentTurnRunningStep(messages.value);
    if (runningStep) runningStep.tools.push(tool);
    else messages.value.push({ type: 'tool', content: tool });
    lastTool.value = tool;
  }
}

function handleStep(data: StepEventData) {
  const existing = findCurrentTurnStep(messages.value, data.id);
  if (existing) Object.assign(existing, { status: data.status, description: data.description });
  else if (data.status === 'running') messages.value.push({ type: 'step', content: { ...data, tools: [] } as StepContent });
}

function handleEvent(event: AgentSSEEvent) {
  if (event.event === 'message') handleMessage(event.data as MessageEventData);
  else if (event.event === 'tool') handleTool(event.data as ToolEventData);
  else if (event.event === 'step') handleStep(event.data as StepEventData);
  else if (event.event === 'plan') currentPlan.value = event.data as PlanEventData;
  else if (event.event === 'error') {
    const data = event.data as ErrorEventData;
    messages.value.push({ type: 'assistant', content: { content: data.error, timestamp: data.timestamp } as MessageContent });
    failRunningSteps(messages.value);
    isLoading.value = false;
  } else if (event.event === 'done' || event.event === 'wait') isLoading.value = false;
  else if (event.event === 'title') void (event.data as TitleEventData);
  lastEventId.value = event.data.event_id;
  if (event.event === 'done' || event.event === 'wait' || event.event === 'error') {
    void refreshHistory();
  }
}

function attachmentFiles(message: Message): FileInfo[] {
  if (message.type !== 'attachments') return [];
  return (message.content as AttachmentsContent).attachments || [];
}

function imageArtifacts(message: Message): FileInfo[] {
  if (message.type !== 'attachments' || (message.content as AttachmentsContent).role !== 'assistant') return [];
  return attachmentFiles(message).filter(file => /\.(png|jpe?g|gif|webp|svg)$/i.test(file.filename));
}

function artifactPreviewUrl(file: FileInfo): string {
  if (!file.file_url) return '';
  if (/^https?:\/\//i.test(file.file_url)) return file.file_url;
  return `${API_CONFIG.host}${file.file_url}`;
}

async function loadSuggestedQuestions() {
  if (!selectedDatasetId.value || suggestedQuestionsLoading.value) return;
  suggestedQuestionsLoading.value = true;
  suggestedQuestionsError.value = '';
  suggestedQuestions.value = [];
  try {
    const response = await generateDatasetSuggestedQuestions(selectedDatasetId.value);
    const normalized = Array.isArray(response)
      ? [...new Set(response.map((question) => question.trim()).filter(Boolean))]
      : [];
    if (normalized.length !== 4) throw new Error('Suggested question response must contain exactly four unique questions');
    suggestedQuestions.value = normalized;
  } catch (error) {
    console.error('Failed to generate dataset suggested questions', error);
    suggestedQuestionsError.value = '推荐问题生成失败，请稍后重试';
  } finally {
    suggestedQuestionsLoading.value = false;
  }
}

async function refreshHistory() {
  if (!selectedDatasetId.value) {
    historySessions.value = [];
    return;
  }
  historyLoading.value = true;
  try {
    historySessions.value = await listDatasetChatSessions(selectedDatasetId.value);
  } catch (error) {
    console.error('Failed to load dataset chat history', error);
  } finally {
    historyLoading.value = false;
  }
}

function toggleHistory() {
  historyOpen.value = !historyOpen.value;
  if (historyOpen.value) void refreshHistory();
}

function historyStatusClass(status: DatasetChatSession['status']) {
  if (status === 'running' || status === 'pending') return 'bg-amber-500';
  if (status === 'waiting') return 'bg-sky-500';
  return 'bg-emerald-500';
}

function formatHistoryTime(timestamp: number | null) {
  if (!timestamp) return '暂无时间';
  const date = new Date(timestamp * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return new Intl.DateTimeFormat('zh-CN', sameDay
    ? { hour: '2-digit', minute: '2-digit' }
    : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
  ).format(date);
}

async function ensureSession() {
  if (sessionId.value) return sessionId.value;
  loadingStatus.value = '正在创建数据分析会话...';
  const session = await createSession(selectedProfileId.value);
  sessionId.value = session.session_id;
  localStorage.setItem(datasetStorageKey(), session.session_id);
  return session.session_id;
}

function handleInputKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter') return;
  if (event.isComposing || inputComposing.value || event.keyCode === 229) return;
  if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
  event.preventDefault();
  void submit();
}

async function submit() {
  const question = inputMessage.value.trim();
  const selected = dataset.value;
  if (!question || !selected || isLoading.value) return;
  inputMessage.value = '';
  startUserTurn();
  messages.value.push({ type: 'user', content: { content: question, timestamp: Math.floor(Date.now() / 1000) } as MessageContent });
  isLoading.value = true;
  loadingStatus.value = '正在关联数据集...';
  try {
    const activeSessionId = await ensureSession();
    cancelChat = await chatWithSession(
      activeSessionId,
      question,
      lastEventId.value,
      [],
      [],
      [],
      selectedProfileId.value,
      {
        onMessage: ({ event, data }) => handleEvent({ event: event as AgentSSEEvent['event'], data: data as AgentSSEEvent['data'] }),
        onClose: () => { isLoading.value = false; loadingStatus.value = ''; cancelChat = null; },
        onError: (error) => { console.error(error); isLoading.value = false; loadingStatus.value = ''; failRunningSteps(messages.value); },
      },
      [selected.dataset_id],
    );
    loadingStatus.value = 'Agent 正在读取数据集...';
  } catch (error: any) {
    console.error(error);
    isLoading.value = false;
    loadingStatus.value = '';
    messages.value.push({ type: 'assistant', content: { content: `数据探查启动失败：${error?.message || '未知错误'}`, timestamp: Math.floor(Date.now() / 1000) } as MessageContent });
    showErrorToast(error?.message || '数据探查启动失败');
  }
}

function askSuggestion(question: string) {
  inputMessage.value = question;
  void submit();
}

function clearConversationState() {
  lastEventId.value = undefined;
  messages.value = [];
  currentPlan.value = undefined;
  lastTool.value = undefined;
  isLoading.value = false;
  loadingStatus.value = '';
}

async function loadConversation(targetSessionId: string) {
  cancelChat?.();
  cancelChat = null;
  clearConversationState();
  try {
    const session = await getSession(targetSessionId);
    sessionId.value = session.session_id;
    localStorage.setItem(datasetStorageKey(), session.session_id);
    for (const event of session.events) handleEvent(event);
    if (session.status === SessionStatus.RUNNING || session.status === SessionStatus.PENDING) {
      isLoading.value = true;
      cancelChat = await chatWithSession(session.session_id, '', lastEventId.value, [], [], [], selectedProfileId.value, {
        onMessage: ({ event, data }) => handleEvent({ event: event as AgentSSEEvent['event'], data: data as AgentSSEEvent['data'] }),
        onClose: () => { isLoading.value = false; cancelChat = null; },
        onError: () => { isLoading.value = false; failRunningSteps(messages.value); },
      }, dataset.value ? [dataset.value.dataset_id] : []);
    }
  } catch (error) {
    console.error('Failed to restore dataset chat session', error);
    localStorage.removeItem(datasetStorageKey());
    sessionId.value = null;
    clearConversationState();
    throw error;
  }
}

async function restoreConversation() {
  const storedSessionId = localStorage.getItem(datasetStorageKey());
  if (!storedSessionId) return;
  await loadConversation(storedSessionId).catch(() => undefined);
}

async function openHistorySession(targetSessionId: string) {
  historyOpen.value = false;
  if (targetSessionId === sessionId.value) return;
  await loadConversation(targetSessionId).catch(() => {
    showErrorToast('历史任务加载失败');
  });
}

function newConversationFromHistory() {
  cancelChat?.();
  cancelChat = null;
  historyOpen.value = false;
  localStorage.removeItem(datasetStorageKey());
  sessionId.value = null;
  clearConversationState();
}

async function stop() {
  cancelChat?.();
  cancelChat = null;
  if (sessionId.value) await stopSession(sessionId.value).catch(() => undefined);
  isLoading.value = false;
  loadingStatus.value = '';
  failRunningSteps(messages.value);
}

onMounted(async () => {
  await refreshProfiles().catch(() => undefined);
  const routeDatasetId = Array.isArray(route.params.datasetId) ? route.params.datasetId[0] : route.params.datasetId;
  if (!routeDatasetId) {
    catalogLoading.value = false;
    showErrorToast('缺少数据集参数，请重新提交');
    return;
  }
  try {
    dataset.value = await getDataCenterDataset(routeDatasetId);
    selectedDatasetId.value = dataset.value.dataset_id;
    void loadSuggestedQuestions();
  } catch (error: any) {
    showErrorToast(error?.message || '无法读取数据集');
  } finally {
    catalogLoading.value = false;
  }
  await refreshHistory();
  await restoreConversation();
});

onUnmounted(() => { cancelChat?.(); });
</script>

<style scoped>
.icon-button { @apply flex size-9 shrink-0 items-center justify-center rounded-md text-[var(--icon-secondary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)]; }
.secondary-button { @apply h-9 shrink-0 items-center gap-1.5 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]; }
.history-button { @apply flex h-[54px] shrink-0 items-center justify-center gap-1.5 rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-[var(--text-secondary)] shadow-sm transition-colors hover:border-[#8eaa9c] hover:text-[#225f48]; }
.send-button { @apply flex size-9 shrink-0 items-center justify-center rounded-lg bg-[#225f48] text-white transition-colors hover:bg-[#194d39] disabled:cursor-not-allowed disabled:bg-[var(--fill-tsp-white-dark)] disabled:text-[var(--text-disable)]; }
.dataset-chat-message { font-size: 14px; line-height: 1.65; }
.dataset-chat-message :deep(.prose) { max-width: none; font-size: 14px !important; line-height: 1.7 !important; }
.dataset-chat-message :deep(.prose p) { margin-top: 0.55em; margin-bottom: 0.55em; }
.dataset-chat-message :deep(.prose li) { margin-top: 0.2em; margin-bottom: 0.2em; }
.dataset-chat-message :deep(.text-base) { font-size: 14px !important; line-height: 20px !important; }
.dataset-chat-message :deep(.markdown-content) { font-size: 13px; line-height: 20px; }
</style>
