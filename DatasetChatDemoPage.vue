<template>
  <div class="dataset-demo flex h-[100dvh] min-h-0 w-full overflow-hidden bg-[var(--background-white-main)] text-[var(--text-primary)]">
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
        <Database class="size-4 text-[#226b51]" />
        <h1 class="text-sm font-semibold">数据集</h1>
        <span class="ml-auto text-[11px] text-[var(--text-tertiary)]">{{ datasets.length }}</span>
        <button type="button" class="icon-button lg:hidden" aria-label="关闭数据集" @click="mobileCatalogOpen = false"><X class="size-4" /></button>
      </header>

      <div class="min-h-0 flex-1 overflow-y-auto p-2.5">
        <div v-if="catalogLoading" class="flex h-40 items-center justify-center text-xs text-[var(--text-tertiary)]">
          <LoaderCircle class="mr-2 size-4 animate-spin" />正在加载数据集
        </div>

        <button
          v-for="item in datasets"
          :key="item.dataset_id"
          type="button"
          class="mb-1.5 flex w-full gap-3 rounded-lg border p-2 text-left transition-colors last:mb-0"
          :class="item.dataset_id === selectedDatasetId ? 'border-[#8eb9a7] bg-[#f1f7f4] dark:bg-[#27372f]' : 'border-transparent hover:bg-[var(--fill-tsp-white-light)]'"
          @click="selectDataset(item.dataset_id)"
        >
          <div class="h-[72px] w-[86px] shrink-0 overflow-hidden rounded-md bg-[#e7eeea]">
            <img :src="item.preview_url" :alt="`${item.name}预览`" class="h-full w-full object-cover contrast-125" />
          </div>
          <div class="min-w-0 flex-1 py-0.5">
            <div class="flex items-start gap-1.5">
              <div class="line-clamp-2 flex-1 text-[13px] font-medium leading-[18px]">{{ item.name }}</div>
              <CheckCircle2 v-if="item.dataset_id === selectedDatasetId" class="mt-0.5 size-3.5 shrink-0 text-[#2b7659]" />
            </div>
            <p class="mt-1 line-clamp-2 text-[11px] leading-4 text-[var(--text-tertiary)]">{{ item.description }}</p>
          </div>
        </button>
      </div>
    </aside>

    <main class="flex min-w-0 flex-1 flex-col bg-[var(--background-gray-main)]">
      <header class="mobile-safe-top flex h-16 shrink-0 items-center gap-3 border-b border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 sm:px-5">
        <button type="button" class="icon-button lg:hidden" aria-label="打开数据集" @click="mobileCatalogOpen = true"><PanelLeft class="size-5" /></button>
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-semibold">{{ dataset?.name || '数据集智能问答' }}</div>
          <div class="mt-0.5 flex items-center gap-2 text-[10px] text-[var(--text-tertiary)]">
            <span class="truncate">{{ dataset?.data_type || '正在加载数据集' }}</span><span>·</span><span class="truncate">{{ selectedProfile?.name || 'FairStackAI 默认 Agent' }}</span>
          </div>
        </div>
        <AgentSelector class="hidden sm:block" />
        <button v-if="sessionId" type="button" class="secondary-button hidden sm:inline-flex" @click="openMainChat"><ExternalLink class="size-3.5" />在完整任务中打开</button>
        <button type="button" class="icon-button" aria-label="新建数据集问答" title="新建数据集问答" @click="resetConversation"><SquarePen class="size-4" /></button>
      </header>

      <div ref="timelineRef" class="min-h-0 flex-1 overflow-y-auto scroll-smooth">
        <div class="mx-auto flex min-h-full w-full max-w-[800px] flex-col px-4 pb-8 pt-5 sm:px-6 sm:pt-7">
          <div v-if="messages.length === 0" class="flex flex-1 flex-col justify-center py-8 sm:py-12">
            <div v-if="dataset" class="max-w-2xl">
              <div class="flex size-11 items-center justify-center rounded-lg bg-[#e4f0ea] text-[#226b51]"><ScanSearch class="size-5" /></div>
              <div class="mt-5 text-xs font-medium text-[#2b7659]">数据集智能问答</div>
              <h1 class="mt-2 text-xl font-semibold leading-8 sm:text-2xl">围绕选定数据集开展智能问答</h1>
              <p class="mt-2.5 max-w-xl text-[13px] leading-6 text-[var(--text-secondary)]">Agent 将围绕“{{ dataset.name }}”及其完整数据内容开展分析。</p>
              <div class="mt-7 grid gap-2 sm:grid-cols-2">
                <button v-for="question in suggestedQuestions" :key="question" type="button" class="min-h-16 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 py-3 text-left text-sm leading-5 transition-colors hover:border-[#6b927f] hover:bg-[#f6faf8] dark:hover:bg-[#27342f]" @click="askSuggestion(question)">{{ question }}</button>
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
                @keydown.enter.exact.prevent="submit"
              />
              <button v-if="isLoading" type="button" class="send-button" aria-label="停止任务" title="停止任务" @click="stop"><Square class="size-4 fill-current" /></button>
              <button v-else type="button" class="send-button" :disabled="!inputMessage.trim() || !dataset" aria-label="发送" title="发送" @click="submit"><ArrowUp class="size-4" /></button>
            </div>
          </div>
          <p class="mt-1.5 text-center text-[10px] text-[var(--text-tertiary)]">回答由 Agent 基于数据中心正式编目资源生成，关键统计结果应结合数据质量与空间参考进行验证。</p>
        </div>
      </div>
    </main>
  </div>
  <FilePanel />
  <SessionFileList :session-id="sessionId || undefined" />
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowUp, CheckCircle2, ChevronRight, Clock3, Database, ExternalLink, History, Image as ImageIcon, LoaderCircle, PanelLeft, Plus, ScanSearch, Square, SquarePen, X } from 'lucide-vue-next';
import AgentSelector from '@/components/AgentSelector.vue';
import ChatMessage from '@/components/ChatMessage.vue';
import FilePanel from '@/components/FilePanel.vue';
import SessionFileList from '@/components/SessionFileList.vue';
import { createSession, getSession, chatWithSession, stopSession } from '@/api/agent';
import { API_CONFIG } from '@/api/client';
import { listDataCenterDatasets, listDatasetChatSessions, type DataCenterDataset, type DatasetChatSession } from '@/api/dataset';
import type { FileInfo } from '@/api/file';
import { useAgentProfile } from '@/composables/useAgentProfile';
import { useFilePanel } from '@/composables/useFilePanel';
import { showErrorToast } from '@/utils/toast';
import { failRunningSteps, findCurrentTurnRunningStep, findCurrentTurnStep } from '@/utils/chatTimeline';
import { isConsecutiveAssistant, type AttachmentsContent, type Message, type MessageContent, type StepContent, type ToolContent } from '@/types/message';
import type { AgentSSEEvent, ErrorEventData, MessageEventData, PlanEventData, StepEventData, TitleEventData, ToolEventData } from '@/types/event';
import { SessionStatus } from '@/types/response';

const DATASET_STORAGE_KEY = 'fairstackai:dataset-demo:session';
const suggestedQuestions = [
  '这个数据集的主题、时空范围和内部结构是什么？',
  '读取真实栅格并分析降水量的数值分布与异常值。',
  '生成该数据集的空间分布概览。',
  '检查数据质量并给出可复用性建议。',
];

const router = useRouter();
const { selectedProfileId, selectedProfile, refreshProfiles } = useAgentProfile();
const { showFilePanel } = useFilePanel();
const datasets = ref<DataCenterDataset[]>([]);
const selectedDatasetId = ref('');
const dataset = computed(() => datasets.value.find(item => item.dataset_id === selectedDatasetId.value) || datasets.value[0]);
const catalogLoading = ref(true);
const inputMessage = ref('');
const messages = ref<Message[]>([]);
const sessionId = ref<string | null>(null);
const lastEventId = ref<string>();
const isLoading = ref(false);
const loadingStatus = ref('');
const mobileCatalogOpen = ref(false);
const historyOpen = ref(false);
const historyLoading = ref(false);
const historySessions = ref<DatasetChatSession[]>([]);
const timelineRef = ref<HTMLElement>();
const currentPlan = ref<PlanEventData>();
const lastTool = ref<ToolContent>();
let cancelChat: (() => void) | null = null;

watch(messages, async () => {
  await nextTick();
  timelineRef.value?.scrollTo({ top: timelineRef.value.scrollHeight, behavior: 'smooth' });
}, { deep: true });

function selectDataset(datasetId: string) {
  selectedDatasetId.value = datasetId;
  mobileCatalogOpen.value = false;
  historyOpen.value = false;
  void refreshHistory();
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
  const session = await createSession(selectedProfileId.value, 'sandbox');
  sessionId.value = session.session_id;
  localStorage.setItem(DATASET_STORAGE_KEY, session.session_id);
  return session.session_id;
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
    messages.value.push({ type: 'assistant', content: { content: `数据集问答启动失败：${error?.message || '未知错误'}`, timestamp: Math.floor(Date.now() / 1000) } as MessageContent });
    showErrorToast(error?.message || '数据集问答启动失败');
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
    localStorage.setItem(DATASET_STORAGE_KEY, session.session_id);
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
    localStorage.removeItem(DATASET_STORAGE_KEY);
    sessionId.value = null;
    clearConversationState();
    throw error;
  }
}

async function restoreConversation() {
  const storedSessionId = localStorage.getItem(DATASET_STORAGE_KEY);
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
  localStorage.removeItem(DATASET_STORAGE_KEY);
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

function resetConversation() {
  void stop();
  localStorage.removeItem(DATASET_STORAGE_KEY);
  sessionId.value = null;
  clearConversationState();
}

function openMainChat() {
  if (!sessionId.value) return;
  cancelChat?.();
  cancelChat = null;
  void router.push(`/chat/${sessionId.value}`);
}

onMounted(async () => {
  await refreshProfiles().catch(() => undefined);
  try {
    datasets.value = await listDataCenterDatasets();
    selectedDatasetId.value = datasets.value[0]?.dataset_id || '';
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
