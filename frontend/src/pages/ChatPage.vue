<template>
  <SimpleBar ref="simpleBarRef" @scroll="handleScroll">
    <div ref="chatContainerRef" class="relative flex flex-col h-full flex-1 min-w-0 px-3 sm:px-5">
      <div ref="observerRef"
        class="mobile-safe-top sm:min-w-[390px] flex flex-row items-center justify-between pb-2 sm:pt-3 sm:pb-1 gap-2 sticky top-0 z-10 bg-[var(--background-gray-main)] flex-shrink-0">
        <div class="flex items-center sm:flex-1">
          <div class="relative flex items-center">
            <button type="button" @click="toggleLeftPanel" v-if="!isLeftPanelShow"
              class="flex h-11 w-11 items-center justify-center cursor-pointer rounded-lg hover:bg-[var(--fill-tsp-gray-main)] sm:hidden"
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
        <AnalysisInputsPanel
          :files="analysisInputs.files.value"
          :selected-ids="analysisInputs.selectedIds.value"
          :loading="analysisInputs.loading.value"
          :error="analysisInputs.error.value"
          :running="isLoading"
          @toggle="analysisInputs.toggle"
          @refresh="analysisInputs.refresh"
        />
        <div class="flex flex-col w-full gap-[12px] pb-[80px] pt-[12px] flex-1 overflow-y-auto">
          <button v-if="hasMoreHistory" type="button" :disabled="isLoadingHistory" class="mx-auto rounded-lg px-4 py-2 text-sm text-[var(--text-secondary)] disabled:opacity-50" @click="loadEarlierHistory">
            {{ isLoadingHistory ? '正在加载历史记录…' : '加载更早的对话' }}
          </button>
          <AnalysisConversation
            :messages="messages"
            :message-key="messageKey"
            :session-id="sessionId"
            :is-loading="isLoading"
            :completion-advice="completionAdvice"
            :can-resume-analysis="canResumeAnalysis"
            @resumeAnalysis="resumeAnalysis"
            @toolClick="handleToolClick"
            @jupyterOpened="handleJupyterOpened"
            @followUp="handleFollowUpClick"
          />

          <!-- Loading indicator -->
          <LoadingIndicator v-if="isLoading || isRestoringHistory" role="status" aria-live="polite" :text="isRestoringHistory ? '正在加载最近对话…' : connectionNotice || analysisProgress || $t('Thinking')" />
          <p v-else-if="connectionNotice" role="status" class="mt-3 text-sm text-[var(--text-tertiary)]">{{ connectionNotice }}</p>
        </div>

        <div class="mobile-safe-bottom flex flex-col bg-[var(--background-gray-main)] sticky bottom-0">
          <button @click="handleFollow" v-if="!follow"
            class="flex items-center justify-center w-[36px] h-[36px] rounded-full bg-[var(--background-white-main)] hover:bg-[var(--background-gray-main)] clickable border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] absolute -top-20 left-1/2 -translate-x-1/2">
            <ArrowDown class="text-[var(--icon-primary)]" :size="20" />
          </button>
          <PlanPanel v-if="plan && plan.steps.length > 0" :plan="plan" :messages="messages" />
          <ChatBox v-model="inputMessage" v-model:selected-skills="selectedSkills" v-model:selected-mcp-servers="selectedMcpServers" :rows="1" @submit="handleSubmit" :isRunning="isLoading" @stop="handleStop"
            :disabled="isRestoringHistory || isLoading || analysisInputs.loading.value || !!analysisInputs.error.value" :attachments="attachments"
            allow-send-files-only submit-on-enter compact-composer placeholder="上传数据或针对已选资料提问" />
          <p class="pb-1.5 text-center text-[10px] text-[var(--text-tertiary)]">DataSeek 也可能会犯错。请核查重要信息。</p>
        </div>
      </div>
    </div>
    <VersionBadge />
    <ToolPanel ref="toolPanel" :size="toolPanelSize" :sessionId="sessionId" :realTime="realTime"
      :isShare="false"
      @jumpToRealTime="jumpToRealTime" />
  </SimpleBar>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import { ref, onMounted, watch, nextTick, onUnmounted } from 'vue';
import { useRouter, onBeforeRouteUpdate } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import AnalysisConversation from '../components/AnalysisConversation.vue';
import AnalysisInputsPanel from '../components/AnalysisInputsPanel.vue';
import * as agentApi from '../api/agent';
import type { ToolContent } from '../types/message';
import VersionBadge from '../components/VersionBadge.vue';
import ToolPanel from '../components/ToolPanel.vue'
import PlanPanel from '../components/PlanPanel.vue';
import { ArrowDown, FileSearch, PanelLeft, Lock, Globe, Link, Check } from 'lucide-vue-next';
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
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import LoadingIndicator from '@/components/ui/LoadingIndicator.vue';
import { eventBus } from '../utils/eventBus';
import { EVENT_REFRESH_SESSION_LIST, EVENT_SESSION_RENAMED } from '../constants/event';
import { useAnalysisSession } from '../composables/useAnalysisSession';
import { useAnalysisInputs } from '../composables/useAnalysisInputs';
import { uploadAnalysisPrompt } from '../utils/analysisInputs';

const router = useRouter()
const { t } = useI18n()
const { toggleLeftPanel, isLeftPanelShow } = useLeftPanel()
const { showSessionFileList } = useSessionFileList()
const { hideFilePanel } = useFilePanel()
const { selectedProfileId } = useAgentProfile()

// Page-only layout/composer state; the session lifecycle belongs to one controller.
const inputMessage = ref('');
const attachments = ref<FileInfo[]>([]);
const toolPanelSize = ref(0);
const realTime = ref(true);
const title = ref(t('New Chat'));
const titleManuallySet = ref(false);
const shareMode = ref<'private' | 'public'>('private');
const linkCopied = ref(false);
const sharingLoading = ref(false);
const toolPanel = ref<InstanceType<typeof ToolPanel>>();
const simpleBarRef = ref<InstanceType<typeof SimpleBar>>();
const observerRef = ref<HTMLDivElement>();
const chatContainerRef = ref<HTMLDivElement>();
const analysisSession = useAnalysisSession({
  api: agentApi,
  getViewport: () => chatContainerRef.value?.closest('[data-simplebar]')?.querySelector<HTMLElement>('.simplebar-content-wrapper'),
  onNewTurn: () => { toolPanel.value?.hideToolPanel(); realTime.value = false; },
  onReset: () => { toolPanel.value?.hideToolPanel(); hideFilePanel(); },
  onTerminal: () => eventBus.emit(EVENT_REFRESH_SESSION_LIST),
  onTitle: value => { if (!titleManuallySet.value) title.value = value; },
  onHistoryError: () => showErrorToast('历史记录加载失败，请重试。'),
  onSessionRestored: session => {
    shareMode.value = session.is_shared ? 'public' : 'private';
    titleManuallySet.value = session.title_manually_set;
    title.value = session.title || t('New Chat');
    realTime.value = true;
    void agentApi.clearUnreadMessageCount(session.session_id);
  },
});
const { sessionId, messages, isLoading, connectionNotice, analysisProgress, hasMoreHistory,
  isLoadingHistory, isRestoringHistory, timelineRevision, follow, plan, lastNoMessageTool,
  selectedSkills, selectedMcpServers, completionAdvice, messageKey,
  canResumeAnalysis, resumeAnalysis, loadEarlierHistory, stop: handleStop } = analysisSession;
const analysisInputs = useAnalysisInputs(sessionId, isLoading);

const resetState = () => {
  analysisSession.reset();
  inputMessage.value = '';
  attachments.value = [];
  toolPanelSize.value = 0;
  realTime.value = true;
  title.value = t('New Chat');
  titleManuallySet.value = false;
  shareMode.value = 'private';
  linkCopied.value = false;
  sharingLoading.value = false;
};

// Watch message changes and automatically scroll to bottom
watch([() => messages.value.length, timelineRevision], async () => {
  await nextTick();
  if (follow.value) {
    simpleBarRef.value?.scrollToBottom();
  }
});

watch(completionAdvice, async () => {
  await nextTick();
  if (follow.value) {
    simpleBarRef.value?.scrollToBottom();
  }
});



const handleSessionRenamed = (payload: unknown) => {
  const renamed = payload as { sessionId?: string; title?: string };
  if (renamed.sessionId === sessionId.value && renamed.title) {
    title.value = renamed.title;
    titleManuallySet.value = true;
  }
};

const handleSubmit = () => {
  if (analysisInputs.loading.value || analysisInputs.error.value) return;
  void chat(inputMessage.value, attachments.value, selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
};

const handleFollowUpClick = (question: string) => {
  if (analysisInputs.loading.value || analysisInputs.error.value) return;
  void chat(question, [], selectedSkills.value, selectedMcpServers.value, selectedProfileId.value);
};

const chat = async (
  message = '', files: FileInfo[] = [], skills: string[] = [], mcpServers: string[] = [],
  agentProfileId: string | null = selectedProfileId.value,
) => {
  if (isLoading.value || isRestoringHistory.value) return;
  const question = uploadAnalysisPrompt(message, files);
  if (!question) return;
  const pending = analysisSession.send({ message: question, files, skills, mcpServers, agentProfileId,
    inputFileIds: analysisInputs.selectedIds.value });
  inputMessage.value = '';
  attachments.value = [];
  await pending;
};

const restoreSession = async () => {
  const id = sessionId.value;
  if (!id) return;
  await analysisSession.restore(id).catch(() => showErrorToast('历史任务加载失败'));
};

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
  analysisSession.dispose();
  eventBus.off(EVENT_SESSION_RENAMED, handleSessionRenamed);
});

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

const handleJupyterOpened = (tool: ToolContent) => {
  realTime.value = true;
  toolPanel.value?.showToolPanel(tool, true);
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
