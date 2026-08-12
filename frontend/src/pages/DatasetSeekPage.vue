<template>
  <div class="dataset-seek flex h-[100dvh] min-h-0 w-full max-w-full overflow-hidden bg-[var(--background-white-main)] text-[var(--text-primary)]">
    <button
      v-if="mobileCatalogOpen"
      type="button"
      class="fixed inset-0 z-30 bg-black/25 lg:hidden"
      aria-label="关闭数据集"
      @click="mobileCatalogOpen = false"
    />

    <aside
      class="fixed inset-y-0 left-0 z-40 flex w-[min(88vw,320px)] shrink-0 flex-col border-r border-[var(--border-main)] bg-[var(--background-menu-white)] transition-[width,transform] duration-200 lg:static lg:translate-x-0"
      :class="[
        mobileCatalogOpen ? 'translate-x-0' : 'catalog-mobile-collapsed',
        catalogCollapsed ? 'lg:w-14' : 'lg:w-[304px]',
      ]"
    >
      <header
        class="mobile-safe-top flex h-16 shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-3"
        :class="catalogCollapsed ? 'lg:justify-center lg:px-2' : ''"
      >
        <div
          class="flex min-w-0 flex-1 items-center gap-2"
          :class="[
            catalogCollapsed ? 'lg:hidden' : '',
            mobileCatalogOpen ? '' : 'catalog-mobile-header-hidden',
          ]"
        >
          <img src="/ai-dataseek-logo.png" alt="" class="size-5 shrink-0 object-contain" aria-hidden="true" />
          <h1 class="min-w-0 flex-1 truncate text-sm font-semibold">科学数据探查</h1>
        </div>
        <button
          type="button"
          class="icon-button ml-auto"
          :class="catalogCollapsed ? 'lg:mx-auto' : ''"
          :aria-label="catalogToggleLabel"
          :title="catalogToggleLabel"
          :aria-expanded="!catalogPanelCollapsed"
          aria-controls="dataset-catalog-panel"
          @click="toggleCatalogPanel"
        >
          <PanelLeftOpen v-if="catalogPanelCollapsed" class="size-5" />
          <PanelLeftClose v-else class="size-5" />
        </button>
      </header>

      <div
        id="dataset-catalog-panel"
        class="min-h-0 flex-1 overscroll-contain overflow-x-hidden overflow-y-auto p-4"
        :class="[
          catalogCollapsed ? 'lg:hidden' : '',
          mobileCatalogOpen ? '' : 'catalog-mobile-content-hidden',
        ]"
      >
        <div v-if="catalogLoading" class="flex h-40 items-center justify-center text-xs text-[var(--text-tertiary)]">
          <LoaderCircle class="mr-2 size-4 animate-spin" />正在加载数据集
        </div>

        <div v-else-if="dataset" class="space-y-5">
          <section>
            <h2 class="break-words text-base font-semibold leading-6">{{ dataset.name }}</h2>
          </section>

          <dl class="space-y-4 border-y border-[var(--border-main)] py-4">
            <div>
              <dt class="text-[11px] text-[var(--text-tertiary)]">数据集摘要</dt>
              <dd
                ref="datasetSummaryRef"
                class="mt-1.5 whitespace-pre-wrap break-words text-xs leading-5 text-[var(--text-secondary)]"
                :class="datasetSummaryExpanded ? '' : 'line-clamp-5'"
              >{{ dataset.description || '未提供摘要' }}</dd>
              <button
                v-if="datasetSummaryOverflowing"
                type="button"
                class="mt-1.5 inline-flex items-center gap-0.5 text-[11px] font-medium text-[#286d52] hover:text-[#194d39]"
                :aria-expanded="datasetSummaryExpanded"
                @click="toggleDatasetSummary"
              >
                {{ datasetSummaryExpanded ? '收起' : '展开' }}
                <ChevronDown class="size-3 transition-transform" :class="datasetSummaryExpanded ? 'rotate-180' : ''" />
              </button>
            </div>
          </dl>

          <section>
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-xs font-semibold">数据文件</h3>
              <span class="text-[10px] text-[var(--text-tertiary)]">{{ dataset.files.length }} 个</span>
            </div>
            <div v-if="dataset.files.length" class="mt-2.5 overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)]">
              <div
                v-for="node in datasetFileTreeRows"
                :key="node.path"
                class="group flex min-w-0 items-center gap-2 border-b border-[var(--border-main)] px-2 py-1.5 transition-colors last:border-b-0 hover:bg-[var(--fill-tsp-white-light)]"
                :style="{ paddingLeft: `${8 + node.depth * 16}px` }"
              >
                <button
                  v-if="node.kind === 'directory'"
                  type="button"
                  class="flex size-6 shrink-0 items-center justify-center rounded text-[var(--icon-secondary)] hover:bg-[var(--fill-tsp-white-dark)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2b7659]/40"
                  :aria-label="`${isDirectoryExpanded(node.path) ? '收起' : '展开'}目录 ${node.name}`"
                  :aria-expanded="isDirectoryExpanded(node.path)"
                  @click="toggleDirectory(node.path)"
                >
                  <ChevronDown v-if="isDirectoryExpanded(node.path)" class="size-3.5" />
                  <ChevronRight v-else class="size-3.5" />
                </button>
                <span v-else class="size-6 shrink-0" aria-hidden="true" />
                <FolderOpen v-if="node.kind === 'directory' && isDirectoryExpanded(node.path)" class="size-4 shrink-0 text-[#2b7659]" />
                <Folder v-else-if="node.kind === 'directory'" class="size-4 shrink-0 text-[#2b7659]" />
                <FileText v-else class="size-4 shrink-0 text-[#2b7659]" />
                <span class="min-w-0 flex-1 truncate text-xs" :class="node.kind === 'directory' ? 'font-medium text-[var(--text-primary)]' : 'text-[var(--text-secondary)]'" :title="node.path">
                  {{ node.name }}
                </span>
                <button
                  v-if="node.kind === 'file'"
                  type="button"
                  class="pointer-events-none -my-1.5 flex size-7 shrink-0 items-center justify-center rounded-md text-[var(--icon-secondary)] opacity-0 transition-[color,background-color,opacity] hover:bg-[var(--fill-tsp-white-dark)] hover:text-[var(--icon-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#2b7659]/40 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100 max-sm:pointer-events-auto max-sm:opacity-100"
                  :title="copiedDatasetFilePath === node.path ? `已复制 ${node.name}` : `复制文件名 ${node.name}`"
                  :aria-label="copiedDatasetFilePath === node.path ? `已复制 ${node.name}` : `复制文件名 ${node.name}`"
                  @click.stop="copyDatasetFileName(node.name, node.path)"
                >
                  <Check v-if="copiedDatasetFilePath === node.path" class="size-3.5 text-[#2b7659]" />
                  <Copy v-else class="size-3.5" />
                </button>
              </div>
            </div>
            <div v-if="!dataset.files.length" class="mt-2.5 rounded-lg border border-dashed border-[var(--border-main)] px-3 py-6 text-center text-xs text-[var(--text-tertiary)]">
              暂无可展示的文件名
            </div>
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

    <main
      class="flex min-h-0 min-w-0 max-w-full flex-1 flex-col overflow-hidden bg-[var(--background-gray-main)] transition-[padding] duration-200"
      :class="mobileCatalogOpen ? '' : 'catalog-main-with-rail'"
    >
      <div
        ref="timelineRef"
        class="timeline-scrollbar-hidden relative min-h-0 min-w-0 max-w-full flex-1 overscroll-contain overflow-x-hidden overflow-y-auto"
        @wheel.passive="handleTimelineWheel"
        @pointerdown="handleTimelinePointerDown"
        @pointerup="handleTimelinePointerUp"
        @pointercancel="handleTimelinePointerUp"
      >
        <div ref="historyMenuRef" class="relative shrink-0" @keydown.esc="historyOpen = false">
          <button
            type="button"
            class="history-header-button absolute right-3 top-3 z-20 sm:right-5 sm:top-5"
            :class="historyOpen ? 'bg-[var(--fill-tsp-white-main)] text-[var(--text-primary)]' : ''"
            aria-label="历史任务"
            title="历史任务"
            aria-haspopup="dialog"
            aria-controls="dataset-history-panel"
            :aria-expanded="historyOpen"
            @click="toggleHistory"
          >
            <History class="size-4 shrink-0" />
            <span class="hidden max-w-[160px] truncate text-sm font-medium sm:block">历史任务</span>
            <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="historyOpen ? 'rotate-180' : ''" />
          </button>

          <div
            v-if="historyOpen"
            id="dataset-history-panel"
            role="dialog"
            aria-label="历史任务"
            class="absolute right-3 top-12 z-50 flex max-h-[min(440px,70vh)] w-[calc(100vw-72px)] max-w-[360px] flex-col overflow-hidden rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-xl sm:right-5 sm:top-14"
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
        </div>
        <div ref="timelineContentRef" class="mx-auto flex min-h-full w-full min-w-0 max-w-[800px] flex-col px-4 pb-8 pt-5 sm:px-6 sm:pt-7">
          <div v-if="messages.length === 0" class="flex flex-1 flex-col justify-center py-8 sm:py-12">
            <div v-if="dataset" class="max-w-2xl">
              <img src="/ai-dataseek-logo.png" alt="" class="size-11 object-contain" aria-hidden="true" />
              <div class="mt-5 text-xs font-medium text-[#2b7659]">科学数据探查</div>
              <h1 class="mt-2 text-xl font-semibold leading-8 sm:text-2xl">围绕选定数据集开展智能问答</h1>
              <p class="mt-2.5 max-w-xl text-[13px] leading-6 text-[var(--text-secondary)]">DataSeek 将围绕当前选定数据集及其完整数据内容开展分析。</p>
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

          <div v-else class="dataset-chat-list flex min-w-0 max-w-full flex-col gap-2">
            <template v-for="(message, index) in messages" :key="`${message.type}-${index}`">
              <div class="dataset-chat-message min-w-0 max-w-full">
                <ChatMessage
                  :message="message"
                  :session-id="sessionId || undefined"
                  :hide-header="isConsecutiveAssistant(messages, index)"
                  @toolClick="handleToolClick"
                />
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
            <div
              v-if="completionAdvice && completionAdvice.recommendations.length && !isLoading"
              class="mt-4 rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4"
            >
              <div class="mb-2 text-sm font-medium">推荐追问</div>
              <div class="flex flex-col gap-2 text-sm text-[var(--text-secondary)]">
                <button
                  v-for="(item, index) in completionAdvice.recommendations"
                  :key="`${item}-${index}`"
                  type="button"
                  :disabled="isLoading"
                  class="group flex w-full items-center justify-between gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-2 text-left transition-colors hover:bg-[var(--fill-tsp-white-dark)] disabled:cursor-not-allowed disabled:opacity-60"
                  @click="askSuggestion(item)"
                >
                  <span class="min-w-0 flex-1">{{ item }}</span>
                  <ChevronRight class="size-4 shrink-0 text-[var(--icon-tertiary)] transition-transform group-hover:translate-x-0.5 group-hover:text-[var(--icon-secondary)]" />
                </button>
              </div>
              <div v-if="completionAdvice.is_skill_candidate && completionAdvice.skill_reason" class="mt-3 text-xs text-[var(--text-tertiary)]">
                {{ completionAdvice.skill_reason }}
              </div>
            </div>
            <div v-if="isLoading" class="mt-3 flex items-center gap-2 text-sm text-[var(--text-tertiary)]">
              <LoaderCircle class="size-4 animate-spin" />
              <span>{{ loadingStatus || 'DataSeek 正在分析数据集...' }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="mobile-safe-bottom shrink-0 border-t border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-3 sm:px-6">
        <div class="mx-auto w-full max-w-[800px]">
          <ChatBox
            class="!bg-transparent !pb-0"
            v-model="inputMessage"
            v-model:selected-skills="selectedSkills"
            :rows="1"
            :is-running="isLoading"
            :attachments="datasetChatAttachments"
            :disabled="isLoading || !dataset"
            :show-file-actions="false"
            :show-mcp-actions="false"
            :placeholder="DATASET_CHAT_PLACEHOLDER"
            skill-menu-placement="up"
            submit-on-enter
            compact-composer
            @submit="submit"
            @stop="stop"
          />
          <p class="mt-1.5 text-center text-[10px] text-[var(--text-tertiary)]">回答由 DataSeek 基于本次关联数据目录生成，分析结果应结合数据质量与来源信息进行验证。</p>
        </div>
      </div>
    </main>
    <ToolPanel
      ref="toolPanel"
      :session-id="sessionId || undefined"
      :real-time="toolPanelRealTime"
      :is-share="false"
      @jump-to-real-time="jumpToLatestTool"
    />
    <FilePanel resizable :reserved-width="catalogCollapsed ? 56 : 304" />
  </div>
  <SessionFileList :session-id="sessionId || undefined" />
  <SettingsDialog />
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { Check, ChevronDown, ChevronRight, CircleAlert, Clock3, Copy, Database, FileText, Folder, FolderOpen, History, Image as ImageIcon, LoaderCircle, PanelLeftClose, PanelLeftOpen, Plus, RefreshCw } from 'lucide-vue-next';
import ChatBox from '@/components/ChatBox.vue';
import ChatMessage from '@/components/ChatMessage.vue';
import FilePanel from '@/components/FilePanel.vue';
import SessionFileList from '@/components/SessionFileList.vue';
import SettingsDialog from '@/components/settings/SettingsDialog.vue';
import ToolPanel from '@/components/ToolPanel.vue';
import { createSession, getSession, chatWithSession, stopSession } from '@/api/agent';
import { API_CONFIG } from '@/api/client';
import { generateDatasetSuggestedQuestions, getDataCenterDataset, listDatasetChatSessions, type DataCenterDataset, type DataCenterDatasetFile, type DatasetChatSession } from '@/api/dataset';
import type { FileInfo } from '@/api/file';
import { getSkillPreferences } from '@/api/skill';
import { useAgentProfile } from '@/composables/useAgentProfile';
import { useFilePanel } from '@/composables/useFilePanel';
import { EVENT_SKILL_PREFERENCES_UPDATED } from '@/constants/event';
import { DATASET_CHAT_PLACEHOLDER, buildDatasetChatCapabilities } from '@/utils/datasetCapabilitySelection';
import { copyToClipboard } from '@/utils/dom';
import { eventBus } from '@/utils/eventBus';
import { showErrorToast, showSuccessToast } from '@/utils/toast';
import { failRunningSteps, findCurrentTurnRunningStep, findCurrentTurnStep, insertTaskExecutionSummary } from '@/utils/chatTimeline';
import { isConsecutiveAssistant, type AttachmentsContent, type Message, type MessageContent, type StepContent, type ToolContent } from '@/types/message';
import type { AgentSSEEvent, CompletionAdviceData, DoneEventData, ErrorEventData, MessageEventData, PlanEventData, StepEventData, TitleEventData, ToolEventData } from '@/types/event';
import { SessionStatus } from '@/types/response';

const DATASET_STORAGE_KEY_PREFIX = 'ai-dataseek:dataset-seek:session';

const route = useRoute();
const { selectedProfileId, refreshProfiles } = useAgentProfile();
const { showFilePanel } = useFilePanel();
const dataset = ref<DataCenterDataset>();
const selectedDatasetId = ref('');
const datasetSummaryRef = ref<HTMLElement | null>(null);
const datasetSummaryExpanded = ref(false);
const datasetSummaryOverflowing = ref(false);
const expandedDirectoryPaths = ref(new Set<string>());
const copiedDatasetFilePath = ref<string | null>(null);
const catalogLoading = ref(true);
const inputMessage = ref('');
const datasetChatAttachments: FileInfo[] = [];
const selectedSkills = ref<string[]>([]);
const autoEnabledSkillNames = ref(new Set<string>());
const messages = ref<Message[]>([]);
const sessionId = ref<string | null>(null);
const lastEventId = ref<string>();
const isLoading = ref(false);
const taskStartedAtMs = ref<number>();
const loadingStatus = ref('');
const mobileCatalogOpen = ref(false);
const catalogCollapsed = ref(false);
const desktopCatalogViewport = ref(false);
const catalogPanelCollapsed = computed(() => desktopCatalogViewport.value ? catalogCollapsed.value : !mobileCatalogOpen.value);
const catalogToggleLabel = computed(() => catalogPanelCollapsed.value ? '展开数据集详情' : '收起数据集详情');
const suggestedQuestions = ref<string[]>([]);
const suggestedQuestionsLoading = ref(false);
const suggestedQuestionsError = ref('');
const completionAdvice = ref<CompletionAdviceData>();
const historyOpen = ref(false);
const historyMenuRef = ref<HTMLElement>();
const historyLoading = ref(false);
const historySessions = ref<DatasetChatSession[]>([]);
const timelineRef = ref<HTMLElement>();
const timelineContentRef = ref<HTMLElement>();
const shouldFollowTimeline = ref(true);
const currentPlan = ref<PlanEventData>();
const lastTool = ref<ToolContent>();
const lastNoMessageTool = ref<ToolContent>();
const toolPanel = ref<InstanceType<typeof ToolPanel>>();
const toolPanelRealTime = ref(true);
let cancelChat: (() => void) | null = null;
let timelineResizeObserver: ResizeObserver | null = null;
let datasetSummaryResizeObserver: ResizeObserver | null = null;
let desktopCatalogMediaQuery: MediaQueryList | null = null;
let datasetFileCopyTimer: ReturnType<typeof setTimeout> | null = null;

const TIMELINE_BOTTOM_THRESHOLD = 120;

watch(() => dataset.value?.dataset_id, () => {
  expandedDirectoryPaths.value = new Set(datasetFileTree.value.map((node) => node.path));
  copiedDatasetFilePath.value = null;
  if (datasetFileCopyTimer) {
    clearTimeout(datasetFileCopyTimer);
    datasetFileCopyTimer = null;
  }
}, { flush: 'sync' });

watch(() => dataset.value?.name, (name) => {
  document.title = name ? `DataSeek-${name}` : 'DataSeek';
}, { immediate: true });

watch(() => dataset.value?.description, async () => {
  datasetSummaryExpanded.value = false;
  await nextTick();
  updateDatasetSummaryOverflow();
}, { flush: 'post' });

watch(datasetSummaryRef, async (element) => {
  datasetSummaryResizeObserver?.disconnect();
  if (element) datasetSummaryResizeObserver?.observe(element);
  await nextTick();
  updateDatasetSummaryOverflow();
}, { flush: 'post' });

function datasetStorageKey() {
  return `${DATASET_STORAGE_KEY_PREFIX}:${selectedDatasetId.value}`;
}

function scrollTimelineToBottom() {
  const timeline = timelineRef.value;
  if (!timeline || !shouldFollowTimeline.value) return;
  timeline.scrollTop = timeline.scrollHeight;
}

function updateTimelineFollowState() {
  const timeline = timelineRef.value;
  if (!timeline) return;
  const distanceToBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight;
  shouldFollowTimeline.value = distanceToBottom <= TIMELINE_BOTTOM_THRESHOLD;
}

function handleTimelineWheel(event: WheelEvent) {
  if (event.deltaY < 0) {
    shouldFollowTimeline.value = false;
    return;
  }
  requestAnimationFrame(updateTimelineFollowState);
}

function handleTimelinePointerDown() {
  shouldFollowTimeline.value = false;
}

function handleTimelinePointerUp() {
  updateTimelineFollowState();
}

watch(messages, async () => {
  if (!shouldFollowTimeline.value) return;
  await nextTick();
  scrollTimelineToBottom();
}, { deep: true });

function updateDatasetSummaryOverflow() {
  const element = datasetSummaryRef.value;
  if (!element || !dataset.value?.description) {
    datasetSummaryOverflowing.value = false;
    return;
  }
  if (datasetSummaryExpanded.value) {
    const lineHeight = Number.parseFloat(window.getComputedStyle(element).lineHeight);
    datasetSummaryOverflowing.value = Number.isFinite(lineHeight) && element.scrollHeight > lineHeight * 5 + 1;
    return;
  }
  datasetSummaryOverflowing.value = element.scrollHeight > element.clientHeight + 1;
}

async function toggleDatasetSummary() {
  datasetSummaryExpanded.value = !datasetSummaryExpanded.value;
  await nextTick();
  updateDatasetSummaryOverflow();
}

type DatasetFileTreeNode = {
  kind: 'directory' | 'file';
  name: string;
  path: string;
  children: DatasetFileTreeNode[];
  file?: DataCenterDatasetFile;
};

type DatasetFileTreeRow = Omit<DatasetFileTreeNode, 'children'> & { depth: number };

function normalizedDatasetPath(file: DataCenterDatasetFile) {
  return (file.path || file.name).replace(/\\/g, '/').split('/').filter(Boolean).join('/');
}

const datasetFileTree = computed<DatasetFileTreeNode[]>(() => {
  const root: DatasetFileTreeNode = { kind: 'directory', name: '', path: '', children: [] };
  const directories = new Map<string, DatasetFileTreeNode>([['', root]]);

  for (const file of dataset.value?.files || []) {
    const path = normalizedDatasetPath(file);
    if (!path) continue;
    const parts = path.split('/');
    let parent = root;
    for (let index = 0; index < parts.length - 1; index += 1) {
      const directoryPath = parts.slice(0, index + 1).join('/');
      let directory = directories.get(directoryPath);
      if (!directory) {
        directory = { kind: 'directory', name: parts[index], path: directoryPath, children: [] };
        directories.set(directoryPath, directory);
        parent.children.push(directory);
      }
      parent = directory;
    }
    parent.children.push({ kind: 'file', name: parts[parts.length - 1] || '未命名文件', path, children: [], file });
  }

  const sortTree = (nodes: DatasetFileTreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'directory' ? -1 : 1;
      return left.name.localeCompare(right.name, 'zh-CN', { numeric: true, sensitivity: 'base' });
    });
    nodes.forEach((node) => sortTree(node.children));
  };
  sortTree(root.children);
  return root.children;
});

const datasetFileTreeRows = computed<DatasetFileTreeRow[]>(() => {
  const rows: DatasetFileTreeRow[] = [];
  const visit = (nodes: DatasetFileTreeNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ ...node, depth });
      if (node.kind === 'directory' && expandedDirectoryPaths.value.has(node.path)) visit(node.children, depth + 1);
    }
  };
  visit(datasetFileTree.value, 0);
  return rows;
});

function isDirectoryExpanded(path: string) {
  return expandedDirectoryPaths.value.has(path);
}

function toggleDirectory(path: string) {
  const updated = new Set(expandedDirectoryPaths.value);
  if (updated.has(path)) updated.delete(path);
  else updated.add(path);
  expandedDirectoryPaths.value = updated;
}

async function copyDatasetFileName(name: string, path: string) {
  const copied = await copyToClipboard(name);
  if (!copied) {
    showErrorToast('复制文件名失败，请检查浏览器剪贴板权限');
    return;
  }

  copiedDatasetFilePath.value = path;
  if (datasetFileCopyTimer) clearTimeout(datasetFileCopyTimer);
  datasetFileCopyTimer = setTimeout(() => {
    copiedDatasetFilePath.value = null;
    datasetFileCopyTimer = null;
  }, 1500);
  showSuccessToast('文件名已复制');
}

function toggleCatalogPanel() {
  if (desktopCatalogViewport.value) {
    catalogCollapsed.value = !catalogCollapsed.value;
    return;
  }
  mobileCatalogOpen.value = !mobileCatalogOpen.value;
}

function handleCatalogViewportChange(event: MediaQueryListEvent) {
  desktopCatalogViewport.value = event.matches;
}

function startUserTurn() {
  closeToolPanel();
  shouldFollowTimeline.value = true;
  failRunningSteps(messages.value, false);
  currentPlan.value = undefined;
  lastTool.value = undefined;
  lastNoMessageTool.value = undefined;
  completionAdvice.value = undefined;
}

function closeToolPanel() {
  toolPanel.value?.hideToolPanel();
  toolPanelRealTime.value = false;
}

function handleToolClick(tool: ToolContent) {
  if (!sessionId.value) return;
  toolPanelRealTime.value = false;
  toolPanel.value?.showToolPanel(tool, false);
}

function jumpToLatestTool() {
  toolPanelRealTime.value = true;
  if (!lastNoMessageTool.value) {
    toolPanel.value?.hideToolPanel();
    return;
  }
  toolPanel.value?.showToolPanel(
    lastNoMessageTool.value,
    lastNoMessageTool.value.status === 'calling',
  );
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
  if (tool.name !== 'message') {
    lastNoMessageTool.value = lastTool.value;
  }
}

function handleStep(data: StepEventData) {
  const existing = findCurrentTurnStep(messages.value, data.id);
  if (existing) {
    Object.assign(existing, { status: data.status, description: data.description });
    if (data.status === 'completed' || data.status === 'failed') existing.ended_at = data.timestamp;
  } else if (data.status === 'running') {
    messages.value.push({ type: 'step', content: { ...data, started_at: data.timestamp, tools: [] } as StepContent });
  }
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
    taskStartedAtMs.value = undefined;
  } else if (event.event === 'done') {
    isLoading.value = false;
    const elapsedMs = taskStartedAtMs.value === undefined
      ? undefined
      : performance.now() - taskStartedAtMs.value;
    insertTaskExecutionSummary(messages.value, event.data.timestamp, elapsedMs);
    taskStartedAtMs.value = undefined;
    completionAdvice.value = (event.data as DoneEventData).advice;
  } else if (event.event === 'wait') {
    isLoading.value = false;
    taskStartedAtMs.value = undefined;
  }
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

function handleHistoryPointerDown(event: PointerEvent) {
  if (!historyOpen.value) return;
  const target = event.target as Node;
  if (!historyMenuRef.value?.contains(target)) historyOpen.value = false;
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

function setAutoEnabledSkillNames(names: string[]) {
  const normalized = new Set(names.map((name) => name.trim().toLocaleLowerCase()));
  autoEnabledSkillNames.value = normalized;
  selectedSkills.value = selectedSkills.value.filter(
    (name) => !normalized.has(name.trim().toLocaleLowerCase()),
  );
}

function handleSkillPreferencesUpdated(payload: unknown) {
  if (Array.isArray(payload) && payload.every((name) => typeof name === 'string')) {
    setAutoEnabledSkillNames(payload);
  }
}

async function loadAutoEnabledSkillNames() {
  try {
    setAutoEnabledSkillNames(await getSkillPreferences());
  } catch (error) {
    console.error('Failed to load automatic Skill preferences', error);
    autoEnabledSkillNames.value = new Set();
  }
}

async function submit() {
  const question = inputMessage.value.trim();
  const selected = dataset.value;
  if (!question || !selected || isLoading.value) return;
  inputMessage.value = '';
  startUserTurn();
  messages.value.push({ type: 'user', content: { content: question, timestamp: Math.floor(Date.now() / 1000) } as MessageContent });
  taskStartedAtMs.value = performance.now();
  isLoading.value = true;
  loadingStatus.value = '正在关联数据集...';
  try {
    const activeSessionId = await ensureSession();
    const capabilities = buildDatasetChatCapabilities(selected.dataset_id, selectedSkills.value);
    cancelChat = await chatWithSession(
      activeSessionId,
      question,
      lastEventId.value,
      capabilities.attachments,
      capabilities.skills,
      capabilities.mcpServers,
      selectedProfileId.value,
      {
        onMessage: ({ event, data }) => handleEvent({ event: event as AgentSSEEvent['event'], data: data as AgentSSEEvent['data'] }),
        onClose: () => { isLoading.value = false; taskStartedAtMs.value = undefined; loadingStatus.value = ''; cancelChat = null; },
        onError: (error) => { console.error(error); isLoading.value = false; taskStartedAtMs.value = undefined; loadingStatus.value = ''; failRunningSteps(messages.value); },
      },
      capabilities.datasetIds,
    );
    loadingStatus.value = 'DataSeek 正在读取数据集...';
  } catch (error: any) {
    console.error(error);
    isLoading.value = false;
    taskStartedAtMs.value = undefined;
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
  closeToolPanel();
  shouldFollowTimeline.value = true;
  lastEventId.value = undefined;
  messages.value = [];
  currentPlan.value = undefined;
  lastTool.value = undefined;
  lastNoMessageTool.value = undefined;
  isLoading.value = false;
  taskStartedAtMs.value = undefined;
  loadingStatus.value = '';
  completionAdvice.value = undefined;
}

async function loadConversation(targetSessionId: string) {
  cancelChat?.();
  cancelChat = null;
  clearConversationState();
  selectedSkills.value = [];
  try {
    const session = await getSession(targetSessionId);
    sessionId.value = session.session_id;
    localStorage.setItem(datasetStorageKey(), session.session_id);
    let restoredSkills: string[] = [];
    for (const event of session.events) {
      if (event.event === 'message') {
        const message = event.data as MessageEventData;
        if (message.role === 'user') {
          restoredSkills = (message.metadata?.skills || []).filter(
            (name) => !autoEnabledSkillNames.value.has(name.trim().toLocaleLowerCase()),
          );
        }
      }
      handleEvent(event);
    }
    selectedSkills.value = [...new Set(restoredSkills)];
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
    selectedSkills.value = [];
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
  selectedSkills.value = [];
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
  eventBus.on(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
  document.addEventListener('pointerdown', handleHistoryPointerDown);
  desktopCatalogMediaQuery = window.matchMedia('(min-width: 1024px)');
  desktopCatalogViewport.value = desktopCatalogMediaQuery.matches;
  desktopCatalogMediaQuery.addEventListener('change', handleCatalogViewportChange);
  if (typeof ResizeObserver !== 'undefined' && timelineContentRef.value) {
    timelineResizeObserver = new ResizeObserver(() => scrollTimelineToBottom());
    timelineResizeObserver.observe(timelineContentRef.value);
  }
  if (typeof ResizeObserver !== 'undefined') {
    datasetSummaryResizeObserver = new ResizeObserver(updateDatasetSummaryOverflow);
    if (datasetSummaryRef.value) datasetSummaryResizeObserver.observe(datasetSummaryRef.value);
  }
  await refreshProfiles().catch(() => undefined);
  await loadAutoEnabledSkillNames();
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

onUnmounted(() => {
  document.title = 'DataSeek';
  cancelChat?.();
  eventBus.off(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
  document.removeEventListener('pointerdown', handleHistoryPointerDown);
  if (datasetFileCopyTimer) clearTimeout(datasetFileCopyTimer);
  closeToolPanel();
  timelineResizeObserver?.disconnect();
  datasetSummaryResizeObserver?.disconnect();
  desktopCatalogMediaQuery?.removeEventListener('change', handleCatalogViewportChange);
});
</script>

<style scoped>
.icon-button { @apply flex size-9 shrink-0 items-center justify-center rounded-md text-[var(--icon-secondary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)]; }
.secondary-button { @apply h-9 shrink-0 items-center gap-1.5 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]; }
.history-header-button { @apply flex h-9 max-w-48 items-center gap-1.5 rounded-lg bg-[var(--background-gray-main)] px-2 text-[var(--text-secondary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--text-primary)] sm:h-7; }
.dataset-chat-list,
.dataset-chat-message { overflow-x: clip; overflow-y: visible; }
.dataset-chat-message { min-width: 0; max-width: 100%; overflow-wrap: anywhere; font-size: 14px; line-height: 1.65; }
.dataset-chat-message :deep(.prose) { width: 100%; min-width: 0; max-width: 100% !important; overflow-wrap: anywhere; word-break: break-word; font-size: 14px !important; line-height: 1.7 !important; }
.dataset-chat-message :deep(.prose p) { margin-top: 0.55em; margin-bottom: 0.55em; }
.dataset-chat-message :deep(.prose li) { margin-top: 0.2em; margin-bottom: 0.2em; }
.dataset-chat-message :deep(.prose pre) { max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; white-space: pre; overflow-wrap: normal; word-break: normal; }
.dataset-chat-message :deep(.prose pre code) { white-space: inherit; overflow-wrap: inherit; word-break: inherit; }
.dataset-chat-message :deep(.prose table) { display: block; width: 100%; max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; }
.dataset-chat-message :deep(.prose a),
.dataset-chat-message :deep(.prose :not(pre) > code) { overflow-wrap: anywhere; word-break: break-word; }
.dataset-chat-message :deep(.prose img),
.dataset-chat-message :deep(.prose video),
.dataset-chat-message :deep(.prose canvas),
.dataset-chat-message :deep(.prose svg) { max-width: 100%; height: auto; }
.dataset-chat-message :deep(.text-base) { font-size: 14px !important; line-height: 20px !important; }
.dataset-chat-message :deep(.markdown-content) { min-width: 0; max-width: 100%; overflow: hidden; font-size: 13px; line-height: 20px; }
.timeline-scrollbar-hidden { -ms-overflow-style: none; scrollbar-width: none; }
.timeline-scrollbar-hidden::-webkit-scrollbar { display: none; width: 0; height: 0; }

@media (max-width: 1023px) {
  .catalog-mobile-collapsed { transform: translateX(calc(-100% + 52px)); }
  .catalog-mobile-header-hidden { display: none; }
  .catalog-mobile-content-hidden { visibility: hidden; pointer-events: none; }
  .catalog-main-with-rail { padding-left: 52px; }
}
</style>
