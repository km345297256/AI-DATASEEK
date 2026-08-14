<template>
  <SimpleBar ref="simpleBarRef" @scroll="handleScroll">
    <div ref="chatContainerRef" class="relative flex flex-col h-full flex-1 min-w-0 px-3 sm:px-5">
      <div ref="observerRef"
        class="mobile-safe-top sm:min-w-[390px] flex flex-row items-center justify-between pb-2 sm:pt-3 sm:pb-1 gap-2 sticky top-0 z-10 bg-[var(--background-gray-main)] flex-shrink-0">
        <div class="flex items-center sm:flex-1">
          <div class="relative flex items-center">
            <button type="button" @click="toggleLeftPanel" v-if="!isLeftPanelShow"
              class="flex h-11 w-11 items-center justify-center cursor-pointer rounded-lg hover:bg-[var(--fill-tsp-gray-main)] sm:h-7 sm:w-7 sm:rounded-md"
              :aria-label="t('Open navigation')">
              <PanelLeft class="size-5 text-[var(--icon-secondary)]" />
            </button>
          </div>
          <div class="hidden items-center gap-2 ml-1 sm:flex">
            <ManusLogoTextIcon :width="148" :height="30" />
            <AgentSelector />
          </div>
        </div>
        <div class="max-w-full sm:max-w-[768px] sm:min-w-[390px] flex min-w-0 flex-1 flex-col gap-[4px] overflow-hidden">
          <div
            class="text-[var(--text-primary)] text-lg font-medium w-full flex flex-row items-center justify-between flex-1 min-w-0 gap-2">
            <div class="flex flex-row items-center gap-[6px] flex-1 min-w-0">
              <span class="whitespace-nowrap text-ellipsis overflow-hidden">
                {{ title }}
              </span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <span class="relative flex-shrink-0" aria-expanded="false" aria-haspopup="dialog">
                <Popover>
                  <PopoverTrigger>
                    <button
                      class="h-10 w-10 px-0 sm:h-8 sm:w-auto sm:px-3 rounded-full inline-flex items-center justify-center gap-1 clickable outline outline-1 outline-offset-[-1px] outline-[var(--border-btn-main)] hover:bg-[var(--fill-tsp-white-light)] sm:me-1.5"
                      :aria-label="t('Share')">
                      <ShareIcon color="var(--icon-secondary)" />
                      <span class="hidden text-[var(--text-secondary)] text-sm font-medium sm:inline">{{ t('Share') }}</span>
                    </button>
                  </PopoverTrigger>
                  <PopoverContent>
                    <div
                      class="w-[400px] flex flex-col rounded-2xl bg-[var(--background-menu-white)] shadow-[0px_8px_32px_0px_var(--shadow-S),0px_0px_0px_1px_var(--border-light)]"
                      style="max-width: calc(-16px + 100vw);">
                      <div class="flex flex-col pt-[12px] px-[16px] pb-[16px]">
                        <!-- Private mode option -->
                        <div @click="handleShareModeChange('private')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'private' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Lock :size="16" :stroke="shareMode === 'private' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Private Only') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Only visible to you') }}</div>
                          </div><Check :size="20" :class="shareMode === 'private' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'private' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <!-- Public mode option -->
                        <div @click="handleShareModeChange('public')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'public' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Globe :size="16" :stroke="shareMode === 'public' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Public Access') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Anyone with the link can view') }}</div>
                          </div><Check :size="20" :class="shareMode === 'public' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'public' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <div class="border-t border-[var(--border-main)] mt-[4px]"></div>
                        
                        <!-- Show instant share button when in private mode -->
                        <div v-if="shareMode === 'private'">
                          <button @click.stop="handleInstantShare"
                            :disabled="sharingLoading"
                            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full disabled:opacity-50 disabled:cursor-not-allowed"
                            data-tabindex="" tabindex="-1">
                            <div v-if="sharingLoading" class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                            <Link v-else :size="16" stroke="currentColor" :stroke-width="2" />
                            {{ sharingLoading ? t('Sharing...') : t('Share Instantly') }}
                          </button>
                        </div>
                        
                        <!-- Show copy link button when in public mode -->
                        <div v-else>
                          <button @click.stop="handleCopyLink"
                            :class="linkCopied ? 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors active:opacity-80 bg-[var(--Button-primary-white)] text-[var(--text-primary)] hover:opacity-70 active:hover-60 h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full border border-[var(--border-btn-main)] shadow-none' : 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full'"
                            data-tabindex="" tabindex="-1">
                            <Link v-if="!linkCopied" :size="16" stroke="currentColor" :stroke-width="2" />
                            <Check v-else :size="16" color="var(--text-primary)" />
                            {{ linkCopied ? t('Link Copied') : t('Copy Link') }}
                          </button>
                        </div>
                      </div>
                    </div>
                  </PopoverContent>
                </Popover>
              </span>
              <button @click="handleFileListShow"
                class="flex h-10 w-10 items-center justify-center hover:bg-[var(--fill-tsp-white-dark)] rounded-full cursor-pointer sm:h-7 sm:w-7 sm:rounded-lg"
                :aria-label="t('Files')">
                <FileSearch class="text-[var(--icon-secondary)]" :size="18" />
              </button>
            </div>
          </div>
          <div class="w-full flex justify-between items-center">
          </div>
        </div>
        <div class="hidden flex-1 sm:block"></div>
      </div>
      <div class="mx-auto w-full max-w-full sm:max-w-[768px] sm:min-w-[390px] flex flex-col flex-1">
        <div class="flex flex-col w-full gap-[12px] pb-[80px] pt-[12px] flex-1 overflow-y-auto">
          <ChatMessage v-for="(message, index) in messages" :key="index" :message="message"
            :hideHeader="isConsecutiveAssistant(messages, index)"
            :session-id="sessionId"
            :show-assistant-actions="!isLoading && isLatestAssistantMessage(messages, index)"
            @toolClick="handleToolClick" />

          <div v-if="completionAdvice" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-white-main)] p-4">
            <div class="mb-2 text-sm font-medium text-[var(--text-primary)]">推荐追问</div>
            <div class="flex flex-col gap-2 text-sm text-[var(--text-secondary)]">
              <button
                v-for="(item, index) in completionAdvice.recommendations"
                :key="index"
                type="button"
                :disabled="isLoading"
                @click="handleFollowUpClick(item)"
                class="group flex w-full items-center justify-between gap-3 rounded-lg bg-[var(--background-gray-main)] px-3 py-2 text-left transition-colors hover:bg-[var(--fill-tsp-white-dark)] disabled:cursor-not-allowed disabled:opacity-60"
              >
                <span class="min-w-0 flex-1">{{ item }}</span>
                <ChevronRight class="size-4 shrink-0 text-[var(--icon-tertiary)] transition-transform group-hover:translate-x-0.5 group-hover:text-[var(--icon-secondary)]" />
              </button>
            </div>
            <div v-if="completionAdvice.is_skill_candidate" class="mt-3 text-xs text-[var(--text-tertiary)]">
              {{ completionAdvice.skill_reason }}
            </div>
          </div>

          <!-- Loading indicator -->
          <LoadingIndicator v-if="isLoading" :text="$t('Thinking')" />
        </div>

        <div class="mobile-safe-bottom flex flex-col bg-[var(--background-gray-main)] sticky bottom-0">
          <button @click="handleFollow" v-if="!follow"
            class="flex items-center justify-center w-[36px] h-[36px] rounded-full bg-[var(--background-white-main)] hover:bg-[var(--background-gray-main)] clickable border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] absolute -top-20 left-1/2 -translate-x-1/2">
            <ArrowDown class="text-[var(--icon-primary)]" :size="20" />
          </button>
          <PlanPanel v-if="plan && plan.steps.length > 0" :plan="plan" />
          <ChatBox v-model="inputMessage" v-model:selected-skills="selectedSkills" v-model:selected-mcp-servers="selectedMcpServers" :rows="1" @submit="handleSubmit" :isRunning="isLoading" @stop="handleStop"
            :attachments="attachments" />
          <p class="pb-1.5 text-center text-[10px] text-[var(--text-tertiary)]">DataSeek 也可能会犯错。请核查重要信息。</p>
        </div>
      </div>
    </div>
    <ToolPanel ref="toolPanel" :size="toolPanelSize" :sessionId="sessionId" :realTime="realTime"
      :isShare="false"
      @jumpToRealTime="jumpToRealTime" />
  </SimpleBar>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import { ref, onMounted, watch, nextTick, onUnmounted, reactive, toRefs } from 'vue';
import { useRouter, onBeforeRouteUpdate } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import ChatMessage from '../components/ChatMessage.vue';
import * as agentApi from '../api/agent';
import { Message, MessageContent, ToolContent, StepContent, AttachmentsContent, isConsecutiveAssistant } from '../types/message';
import {
  StepEventData,
  ToolEventData,
  MessageEventData,
  ErrorEventData,
  TitleEventData,
  PlanEventData,
  CompletionAdviceData,
  AgentSSEEvent,
} from '../types/event';
import ToolPanel from '../components/ToolPanel.vue'
import PlanPanel from '../components/PlanPanel.vue';
import { ArrowDown, FileSearch, PanelLeft, Lock, Globe, Link, Check, ChevronRight } from 'lucide-vue-next';
import AgentSelector from '../components/AgentSelector.vue';
import ManusLogoTextIcon from '../components/icons/ManusLogoTextIcon.vue';
import ShareIcon from '@/components/icons/ShareIcon.vue';
import { showErrorToast, showSuccessToast } from '../utils/toast';
import type { FileInfo } from '../api/file';
import { useLeftPanel } from '../composables/useLeftPanel'
import { useSessionFileList } from '../composables/useSessionFileList'
import { useFilePanel } from '../composables/useFilePanel'
import { consumePendingChat } from '../composables/usePendingChat'
import { useAgentProfile } from '../composables/useAgentProfile'
import { copyToClipboard } from '../utils/dom'
import {
  failRunningSteps,
  findCurrentTurnRunningStep,
  findCurrentTurnStep,
  insertTaskExecutionSummary,
  isLatestAssistantMessage,
} from '../utils/chatTimeline';
import { SessionStatus } from '../types/response';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import LoadingIndicator from '@/components/ui/LoadingIndicator.vue';
import { eventBus } from '../utils/eventBus';
import { EVENT_REFRESH_SESSION_LIST, EVENT_SESSION_RENAMED } from '../constants/event';

const router = useRouter()
const { t } = useI18n()
const { toggleLeftPanel, isLeftPanelShow } = useLeftPanel()
const { showSessionFileList } = useSessionFileList()
const { hideFilePanel } = useFilePanel()
const { selectedProfileId } = useAgentProfile()

// Create initial state factory
const createInitialState = () => ({
  inputMessage: '',
  isLoading: false,
  sessionId: undefined as string | undefined,
  messages: [] as Message[],
  toolPanelSize: 0,
  realTime: true,
  follow: true,
  title: t('New Chat'),
  titleManuallySet: false,
  plan: undefined as PlanEventData | undefined,
  lastNoMessageTool: undefined as ToolContent | undefined,
  lastMessageTool: undefined as ToolContent | undefined,
  lastTool: undefined as ToolContent | undefined,
  lastEventId: undefined as string | undefined,
  cancelCurrentChat: null as (() => void) | null,
  attachments: [] as FileInfo[],
  selectedSkills: [] as string[],
  selectedMcpServers: [] as string[],
  shareMode: 'private' as 'private' | 'public', // Default to private mode
  linkCopied: false,
  sharingLoading: false, // Loading state for share operations
  completionAdvice: undefined as CompletionAdviceData | undefined,
  taskStartedAtMs: undefined as number | undefined,
});

// Create reactive state
const state = reactive(createInitialState());

// Destructure refs from reactive state
const {
  inputMessage,
  isLoading,
  sessionId,
  messages,
  toolPanelSize,
  realTime,
  follow,
  title,
  titleManuallySet,
  plan,
  lastNoMessageTool,
  lastTool,
  lastEventId,
  cancelCurrentChat,
  attachments,
  selectedSkills,
  selectedMcpServers,
  shareMode,
  linkCopied,
  sharingLoading,
  completionAdvice,
  taskStartedAtMs,
} = toRefs(state);

// Non-state refs that don't need reset
const toolPanel = ref<InstanceType<typeof ToolPanel>>()
const simpleBarRef = ref<InstanceType<typeof SimpleBar>>();
const observerRef = ref<HTMLDivElement>();
const chatContainerRef = ref<HTMLDivElement>();

// Reset all refs to their initial values
const resetState = () => {
  // Cancel any existing chat connection
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
  }

  // Reset reactive state to initial values
  Object.assign(state, createInitialState());
};

// Watch message changes and automatically scroll to bottom
watch(messages, async () => {
  await nextTick();
  if (follow.value) {
    simpleBarRef.value?.scrollToBottom();
  }
}, { deep: true });

watch(completionAdvice, async () => {
  await nextTick();
  if (follow.value) {
    simpleBarRef.value?.scrollToBottom();
  }
});



const failActiveSteps = (currentTurnOnly = true) => {
  const failedSteps = failRunningSteps(messages.value, currentTurnOnly);
  if (!plan.value || failedSteps.length === 0) return;
  const failedStepIds = new Set(failedSteps.map(step => step.id));
  for (const step of plan.value.steps) {
    if (failedStepIds.has(step.id) && step.status === 'running') {
      step.status = 'failed';
    }
  }
};

const startUserTurn = () => {
  toolPanel.value?.hideToolPanel();
  realTime.value = false;
  failActiveSteps(false);
  plan.value = undefined;
  lastTool.value = undefined;
  lastNoMessageTool.value = undefined;
};

// Handle message event
const handleMessageEvent = (messageData: MessageEventData) => {
  if (messageData.role === 'user') {
    startUserTurn();
  }
  messages.value.push({
    type: messageData.role,
    content: {
      ...messageData
    } as MessageContent,
  });

  if ((messageData.attachments?.length ?? 0) > 0) {
    messages.value.push({
      type: 'attachments',
      content: {
        ...messageData
      } as AttachmentsContent,
    });
  }
}

// Handle tool event
const handleToolEvent = (toolData: ToolEventData) => {
  const lastStep = findCurrentTurnRunningStep(messages.value);
  let toolContent: ToolContent = {
    ...toolData
  }
  if (lastTool.value && lastTool.value.tool_call_id === toolContent.tool_call_id) {
    Object.assign(lastTool.value, toolContent);
  } else {
    if (lastStep?.status === 'running') {
      lastStep.tools.push(toolContent);
    } else {
      messages.value.push({
        type: 'tool',
        content: toolContent,
      });
    }
    lastTool.value = toolContent;
  }
  if (toolContent.name !== 'message') {
    lastNoMessageTool.value = toolContent;
  }
}

// Handle step event
const handleStepEvent = (stepData: StepEventData) => {
  syncPlanStepStatus(stepData);
  const existingStep = findCurrentTurnStep(messages.value, stepData.id);
  if (stepData.status === 'running') {
    if (existingStep) {
      existingStep.status = stepData.status;
      existingStep.description = stepData.description;
    } else {
      messages.value.push({
        type: 'step',
        content: {
          ...stepData,
          started_at: stepData.timestamp,
          tools: []
        } as StepContent,
      });
    }
  } else if (stepData.status === 'completed') {
    if (existingStep) {
      existingStep.status = stepData.status;
      existingStep.description = stepData.description;
      existingStep.ended_at = stepData.timestamp;
    }
  } else if (stepData.status === 'failed') {
    if (existingStep) {
      existingStep.status = stepData.status;
      existingStep.description = stepData.description;
      existingStep.ended_at = stepData.timestamp;
    }
    isLoading.value = false;
  }
}

// Handle error event
const handleErrorEvent = (errorData: ErrorEventData) => {
  isLoading.value = false;
  taskStartedAtMs.value = undefined;
  failActiveSteps();
  eventBus.emit(EVENT_REFRESH_SESSION_LIST);
  messages.value.push({
    type: 'assistant',
    content: {
      content: errorData.error,
      timestamp: errorData.timestamp
    } as MessageContent,
  });
}

// Handle title event
const handleTitleEvent = (titleData: TitleEventData) => {
  if (titleManuallySet.value) return;
  title.value = titleData.title;
}

const handleSessionRenamed = (payload: unknown) => {
  const renamed = payload as { sessionId?: string; title?: string };
  if (!renamed.sessionId || !renamed.title) return;
  if (renamed.sessionId === sessionId.value) {
    title.value = renamed.title;
    titleManuallySet.value = true;
  }
}

// Handle plan event
const handlePlanEvent = (planData: PlanEventData) => {
  plan.value = mergePlanStepStatus(planData);
}

const syncPlanStepStatus = (stepData: StepEventData) => {
  const targetStep = plan.value?.steps.find(step => step.id === stepData.id);
  if (!targetStep) return;
  if (isTerminalStepStatus(targetStep.status) && !isTerminalStepStatus(stepData.status)) return;
  targetStep.status = stepData.status;
  targetStep.description = stepData.description;
}

const mergePlanStepStatus = (planData: PlanEventData): PlanEventData => {
  const previousStepsById = new Map(plan.value?.steps.map(step => [step.id, step]));
  return {
    ...planData,
    steps: planData.steps.map(step => {
      const previousStep = previousStepsById.get(step.id);
      if (previousStep && isTerminalStepStatus(previousStep.status) && !isTerminalStepStatus(step.status)) {
        return {
          ...step,
          status: previousStep.status,
        };
      }
      return step;
    }),
  };
}

const isTerminalStepStatus = (status: StepEventData['status']) => {
  return status === 'completed' || status === 'failed';
}

// Main event handler function
const handleEvent = (event: AgentSSEEvent) => {
  if (event.event === 'message') {
    handleMessageEvent(event.data as MessageEventData);
  } else if (event.event === 'tool') {
    handleToolEvent(event.data as ToolEventData);
  } else if (event.event === 'step') {
    handleStepEvent(event.data as StepEventData);
  } else if (event.event === 'done') {
    isLoading.value = false;
    eventBus.emit(EVENT_REFRESH_SESSION_LIST);
    const elapsedMs = taskStartedAtMs.value === undefined
      ? undefined
      : performance.now() - taskStartedAtMs.value;
    insertTaskExecutionSummary(messages.value, event.data.timestamp, elapsedMs);
    taskStartedAtMs.value = undefined;
    completionAdvice.value = (event.data as any)?.advice;
  } else if (event.event === 'wait') {
    isLoading.value = false;
    taskStartedAtMs.value = undefined;
    eventBus.emit(EVENT_REFRESH_SESSION_LIST);
  } else if (event.event === 'error') {
    handleErrorEvent(event.data as ErrorEventData);
  } else if (event.event === 'title') {
    handleTitleEvent(event.data as TitleEventData);
  } else if (event.event === 'plan') {
    handlePlanEvent(event.data as PlanEventData);
  }
  lastEventId.value = event.data.event_id;
}

const isCurrentSession = (targetSessionId: string) => {
  return sessionId.value === targetSessionId;
}

const handleSubmit = () => {
  chat(inputMessage.value, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
}

const handleFollowUpClick = (question: string) => {
  const message = question.trim();
  if (!message || isLoading.value) return;
  chat(message, [], selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
}

const chat = async (
  message: string = '',
  files: FileInfo[] = [],
  skills: string[] = [],
  mcpServers: string[] = [],
  agentProfileId: string | null = selectedProfileId.value,
) => {
  if (!sessionId.value) return;
  const activeSessionId = sessionId.value;

  // Cancel any existing chat connection before starting a new one
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
    cancelCurrentChat.value = null;
  }

  if (message.trim()) {
    startUserTurn();
    // Add user message to conversation list
    messages.value.push({
      type: 'user',
      content: {
        content: message,
        timestamp: Math.floor(Date.now() / 1000)
      } as MessageContent,
    });
  }

  if (files.length > 0) {
    messages.value.push({
      type: 'attachments',
      content: {
        role: 'user',
        attachments: files
      } as AttachmentsContent,
    });
  }

  // Automatically enable follow mode when sending message
  follow.value = true;

  // Clear input field and attachments (keep skill/MCP selections for next turn)
  inputMessage.value = '';
  attachments.value = [];
  completionAdvice.value = undefined;
  taskStartedAtMs.value = performance.now();
  isLoading.value = true;

  try {
    // Use the split event handler function and store the cancel function
    cancelCurrentChat.value = await agentApi.chatWithSession(
      activeSessionId,
      message,
      lastEventId.value,
      files
        .filter((file: FileInfo) => file.file_id && !file.file_id.startsWith('temp-'))
        .map((file: FileInfo) => ({file_id : file.file_id, 
                                    filename : file.filename})),
      skills,
      mcpServers,
      agentProfileId,
      {
        onOpen: () => {
          if (!isCurrentSession(activeSessionId)) return;
          console.log('Chat opened');
          isLoading.value = true;
        },
        onMessage: ({ event, data }) => {
          if (!isCurrentSession(activeSessionId)) return;
          handleEvent({
            event: event as AgentSSEEvent['event'],
            data: data as AgentSSEEvent['data']
          });
        },
        onClose: () => {
          if (!isCurrentSession(activeSessionId)) return;
          console.log('Chat closed');
          isLoading.value = false;
          taskStartedAtMs.value = undefined;
          eventBus.emit(EVENT_REFRESH_SESSION_LIST);
          // Clear the cancel function when connection is closed normally
          if (cancelCurrentChat.value) {
            cancelCurrentChat.value = null;
          }
        },
        onError: (error) => {
          if (!isCurrentSession(activeSessionId)) return;
          console.error('Chat error:', error);
          isLoading.value = false;
          taskStartedAtMs.value = undefined;
          failActiveSteps();
          eventBus.emit(EVENT_REFRESH_SESSION_LIST);
          // Clear the cancel function when there's an error
          if (cancelCurrentChat.value) {
            cancelCurrentChat.value = null;
          }
        }
      }
    );
  } catch (error) {
    if (!isCurrentSession(activeSessionId)) return;
    console.error('Chat error:', error);
    isLoading.value = false;
    taskStartedAtMs.value = undefined;
    failActiveSteps();
    cancelCurrentChat.value = null;
  }
}

const restoreSession = async () => {
  if (!sessionId.value) {
    showErrorToast(t('Session not found'));
    return;
  }
  const activeSessionId = sessionId.value;
  const session = await agentApi.getSession(activeSessionId);
  if (!isCurrentSession(activeSessionId)) return;
  // Initialize share mode based on session state
  shareMode.value = session.is_shared ? 'public' : 'private';
  titleManuallySet.value = session.title_manually_set;
  realTime.value = false;
  let restoredSkills: string[] = [];
  let restoredMcpServers: string[] = [];
  for (const event of session.events) {
    if (event.event === 'message') {
      const messageData = event.data as MessageEventData;
      if (messageData.role === 'user') {
        restoredSkills = messageData.metadata?.skills ?? restoredSkills;
        restoredMcpServers = messageData.metadata?.mcp_servers ?? restoredMcpServers;
      }
    }
    handleEvent(event);
  }
  title.value = session.title || t('New Chat');
  selectedSkills.value = restoredSkills;
  selectedMcpServers.value = restoredMcpServers;
  realTime.value = true;
  if (session.status === SessionStatus.RUNNING || session.status === SessionStatus.PENDING) {
    await chat();
  }
  agentApi.clearUnreadMessageCount(activeSessionId);
}

onBeforeRouteUpdate(async (to, _, next) => {
  toolPanel.value?.hideToolPanel();
  hideFilePanel();
  resetState();
  if (to.params.sessionId) {
    messages.value = [];
    sessionId.value = String(to.params.sessionId) as string;
    await restoreSession();
  }
  next();
})

// Initialize active conversation
onMounted(() => {
  eventBus.on(EVENT_SESSION_RENAMED, handleSessionRenamed);
  hideFilePanel();
  const routeParams = router.currentRoute.value.params;
  if (routeParams.sessionId) {
    // If sessionId is included in URL, use it directly
    sessionId.value = String(routeParams.sessionId) as string;
    const pendingChat = consumePendingChat(sessionId.value);
    if (pendingChat?.message) {
      if (pendingChat.skills?.length) selectedSkills.value = pendingChat.skills;
      if (pendingChat.mcpServers?.length) selectedMcpServers.value = pendingChat.mcpServers;
      chat(pendingChat.message, pendingChat.files, pendingChat.skills, pendingChat.mcpServers, pendingChat.agentProfileId ?? null);
    } else {
      restoreSession();
    }
  }


});

onUnmounted(() => {
  eventBus.off(EVENT_SESSION_RENAMED, handleSessionRenamed);
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
    cancelCurrentChat.value = null;
  }
})

const isLastNoMessageTool = (tool: ToolContent) => {
  return tool.tool_call_id === lastNoMessageTool.value?.tool_call_id;
}

const isLiveTool = (tool: ToolContent) => {
  if (tool.status === 'calling') {
    return true;
  }
  if (!isLastNoMessageTool(tool)) {
    return false;
  }
  if (tool.timestamp > Date.now() - 5 * 60 * 1000) {
    return true;
  }
  return false;
}

const handleToolClick = (tool: ToolContent) => {
  realTime.value = false;
  if (sessionId.value) {
    toolPanel.value?.showToolPanel(tool, false);
  }
}

const jumpToRealTime = () => {
  realTime.value = true;
  if (lastNoMessageTool.value) {
    toolPanel.value?.showToolPanel(lastNoMessageTool.value, isLiveTool(lastNoMessageTool.value));
  }
}

const handleFollow = () => {
  follow.value = true;
  simpleBarRef.value?.scrollToBottom();
}

const handleScroll = (_: Event) => {
  follow.value = simpleBarRef.value?.isScrolledToBottom() ?? false;
}

const handleStop = async () => {
  if (sessionId.value) {
    isLoading.value = false;
    await agentApi.stopSession(sessionId.value);
    eventBus.emit(EVENT_REFRESH_SESSION_LIST);
  }
}

const handleFileListShow = () => {
  showSessionFileList()
}

// Share functionality handlers
const handleShareModeChange = async (mode: 'private' | 'public') => {
  if (!sessionId.value || sharingLoading.value) return;
  
  // If mode is same as current, no need to call API
  if (shareMode.value === mode) {
    linkCopied.value = false;
    return;
  }
  
  try {
    sharingLoading.value = true;
    
    if (mode === 'public') {
      await agentApi.shareSession(sessionId.value);
    } else {
      await agentApi.unshareSession(sessionId.value);
    }
    
    shareMode.value = mode;
    linkCopied.value = false;
  } catch (error) {
    console.error('Error changing share mode:', error);
    showErrorToast(t('Failed to change sharing settings'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleInstantShare = async () => {
  if (!sessionId.value) return;
  
  try {
    sharingLoading.value = true;
    await agentApi.shareSession(sessionId.value);
    shareMode.value = 'public';
    linkCopied.value = false;
  } catch (error) {
    console.error('Error sharing session:', error);
    showErrorToast(t('Failed to share session'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleCopyLink = async () => {
  if (!sessionId.value) return;
  
  const shareUrl = `${window.location.origin}/share/${sessionId.value}`;
  
  try {
    const success = await copyToClipboard(shareUrl);
    
    if (success) {
      linkCopied.value = true;
      setTimeout(() => {
        linkCopied.value = false;
      }, 3000);
      showSuccessToast(t('Link copied to clipboard'));
    } else {
      showErrorToast(t('Failed to copy link'));
    }
  } catch (error) {
    console.error('Error copying share link:', error);
    showErrorToast(t('Failed to copy link'));
  }
}
</script>

<style scoped>
</style>
