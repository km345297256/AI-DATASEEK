<template>
  <div v-if="message.type === 'user'" class="flex w-full flex-col items-end justify-end gap-1 group mt-3">
    <div class="flex items-end">
      <div class="flex items-center justify-end gap-[2px] invisible group-hover:visible">
        <div class="float-right transition text-[12px] text-[var(--text-tertiary)] invisible group-hover:visible">
          {{ relativeTime(message.content.timestamp) }}
        </div>
      </div>
    </div>
    <div class="flex max-w-[90%] relative flex-col gap-2 items-end">
      <div
        class="relative max-w-full whitespace-pre-wrap break-words rounded-[12px] bg-[var(--fill-white)] dark:bg-[var(--fill-tsp-white-main)] p-3 text-sm leading-6 text-[var(--text-primary)] ltr:rounded-br-none rtl:rounded-bl-none border border-[var(--border-main)] dark:border-0">
        {{ messageContent.content }}
      </div>
      <div class="flex h-7 w-full items-center justify-end opacity-0 pointer-events-none transition-opacity group-hover:opacity-100 group-hover:pointer-events-auto group-focus-within:opacity-100 group-focus-within:pointer-events-auto max-sm:opacity-100 max-sm:pointer-events-auto">
        <button
          type="button"
          class="flex h-7 w-7 items-center justify-center rounded-md text-[var(--icon-tertiary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-dark)]"
          :aria-label="copied ? '已复制' : '复制提问'"
          :title="copied ? '已复制' : '复制提问'"
          @click="copyUserMessage"
        >
          <CheckIcon v-if="copied" :size="15" />
          <CopyIcon v-else :size="15" />
        </button>
      </div>
    </div>
  </div>
  <div v-else-if="message.type === 'assistant'" class="flex flex-col gap-2 w-full group" :class="hideAssistantHeader ? 'mt-0' : 'mt-3'">
    <div v-if="safetyReview" class="w-full max-w-2xl border-l-2 border-amber-500 bg-amber-50/70 px-4 py-3 text-sm dark:bg-amber-950/20">
      <div class="flex items-start gap-2.5">
        <ShieldAlert class="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <div class="min-w-0 flex-1">
          <div class="font-medium text-[var(--text-primary)]">
            {{ safetyUnavailable ? '安全审核服务暂时不可用' : '请求未通过安全审核' }}
          </div>
          <div v-if="safetyReview.categories.length" class="mt-2 flex flex-wrap gap-1.5">
            <span v-for="category in safetyReview.categories" :key="category" class="rounded border border-amber-300/80 bg-white/70 px-1.5 py-0.5 text-[11px] text-amber-800 dark:border-amber-800 dark:bg-transparent dark:text-amber-300">
              {{ safetyCategoryLabel(category) }}
            </span>
          </div>
          <div class="mt-2 text-[13px] leading-5 text-[var(--text-secondary)]">
            <span class="font-medium text-[var(--text-primary)]">判定原因：</span>{{ safetyReview.reason || '请求命中了系统安全策略。' }}
          </div>
          <div class="mt-1 text-[13px] leading-5 text-[var(--text-secondary)]">
            <span class="font-medium text-[var(--text-primary)]">修改建议：</span>{{ safetyReview.suggestion || '请移除可能违规或越权的内容后重试。' }}
          </div>
        </div>
      </div>
    </div>
    <template v-else>
      <div
        class="max-w-none p-0 m-0 prose prose-sm sm:prose-base dark:prose-invert [&_pre:not(.shiki)]:!bg-[var(--fill-tsp-white-light)] [&_pre:not(.shiki)]:text-[var(--text-primary)] text-base text-[var(--text-primary)]"
        v-html="renderMarkdown(visibleAssistantContent)"></div>
      <div v-if="showAssistantActions" class="flex h-8 items-center gap-1">
        <button
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-md text-[var(--icon-secondary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-dark)]"
          :aria-label="assistantCopied ? '已复制回答' : '复制回答'"
          :title="assistantCopied ? '已复制' : '复制回答'"
          @click="copyAssistantMessage"
        >
          <CheckIcon v-if="assistantCopied" :size="17" />
          <CopyIcon v-else :size="17" />
        </button>
        <button
          v-if="feedbackPreference !== 'dislike'"
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-dark)]"
          :class="feedbackPreference === 'like' ? 'bg-[#e8f3ee] text-[#247357]' : 'text-[var(--icon-secondary)] hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)]'"
          :aria-label="feedbackPreference === 'like' ? '取消喜欢' : '喜欢'"
          :title="feedbackPreference === 'like' ? '取消喜欢' : '喜欢'"
          @click="toggleLike"
        >
          <ThumbsUp :size="17" :fill="feedbackPreference === 'like' ? 'currentColor' : 'none'" />
        </button>
        <button
          v-if="feedbackPreference !== 'like'"
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-dark)]"
          :class="feedbackPreference === 'dislike' ? 'bg-[#f7e9e9] text-[#b54141]' : 'text-[var(--icon-secondary)] hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)]'"
          :aria-label="feedbackPreference === 'dislike' ? '取消不喜欢' : '不喜欢'"
          :title="feedbackPreference === 'dislike' ? '取消不喜欢' : '不喜欢'"
          @click="toggleDislike"
        >
          <ThumbsDown :size="17" :fill="feedbackPreference === 'dislike' ? 'currentColor' : 'none'" />
        </button>
        <button
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-md text-[var(--icon-secondary)] transition-colors hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-dark)]"
          aria-label="分享任务"
          title="分享任务"
          @click="shareTask"
        >
          <Share2Icon :size="17" />
        </button>
      </div>
    </template>
    <Teleport to="body">
      <div v-if="feedbackDialogOpen" class="fixed inset-0 z-[100] flex items-center justify-center bg-black/55 p-4" @click.self="closeFeedbackDialog">
        <section class="w-full max-w-[560px] rounded-lg bg-[#242424] p-4 text-white shadow-2xl" role="dialog" aria-modal="true" aria-label="分享反馈">
          <div class="flex items-center justify-between gap-4">
            <h2 class="text-lg font-semibold">分享反馈</h2>
            <button type="button" class="flex size-8 items-center justify-center rounded text-zinc-300 hover:bg-white/10 hover:text-white" aria-label="关闭" @click="closeFeedbackDialog"><X :size="18" /></button>
          </div>
          <div class="mt-4 flex flex-wrap gap-2">
            <button v-for="reason in dislikeReasonOptions" :key="reason" type="button" class="rounded-full border px-3 py-1.5 text-sm transition-colors" :class="selectedDislikeReasons.includes(reason) ? 'border-[#3b82f6] bg-[#1d4f91]/40 text-white' : 'border-zinc-600 text-zinc-100 hover:bg-white/10'" @click="toggleDislikeReason(reason)">{{ reason }}</button>
          </div>
          <textarea v-model="dislikeDetail" rows="4" maxlength="2000" class="mt-4 w-full resize-none rounded-lg border border-[#3b82f6] bg-[#1d1d1d] px-3 py-2 text-sm text-white outline-none placeholder:text-zinc-400 focus:ring-1 focus:ring-[#60a5fa]" placeholder="分享详细信息（可选）" />
          <p class="mt-3 rounded-md bg-zinc-700 px-3 py-2 text-xs leading-5 text-zinc-200">你的对话内容将随反馈一并提交，以帮助改进 DataSeek。</p>
          <div class="mt-4 flex justify-end gap-2">
            <button type="button" class="rounded-md px-3 py-2 text-sm text-zinc-200 hover:bg-white/10" @click="closeFeedbackDialog">取消</button>
            <button type="button" :disabled="feedbackSaving" class="rounded-md bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 disabled:cursor-not-allowed disabled:opacity-50" @click="submitDislikeFeedback">提交</button>
          </div>
        </section>
      </div>
    </Teleport>
  </div>
  <ToolUse v-else-if="message.type === 'tool'" :tool="toolContent" @click="handleToolClick(toolContent)" />
  <div v-else-if="message.type === 'step'" class="flex flex-col">
    <div v-if="stepContent.status === 'running'" class="mb-2 flex items-center gap-2 text-sm text-[var(--text-secondary)]">
      <span class="size-3.5 animate-spin rounded-full border-2 border-[var(--border-dark)] border-t-transparent" aria-hidden="true" />
      <span>思考中...</span>
    </div>
    <div class="flex">
      <div class="w-[24px] relative">
        <div class="border-l border-dashed border-[var(--border-dark)] absolute start-[8px] top-0 bottom-0"
          style="height: calc(100% + 14px);"></div>
      </div>
      <div class="flex flex-col gap-3 flex-1 min-w-0 overflow-hidden pt-2">
        <div v-for="(item, index) in displayTools" :key="`${item.tool.tool_call_id}-${index}`" class="flex flex-col gap-2">
          <ToolUse
            :tool="item.tool"
            :summary="item.summary"
            :collapsed-count="item.count"
            @click="handleToolClick(item.panelTool)"
          />
          <div v-if="item.count > 1" class="ml-2 text-[12px] text-[var(--text-tertiary)]">
            已折叠 {{ item.count }} 次连续文件写入，点击可查看最后一次写入详情。
          </div>
        </div>
      </div>
    </div>
  </div>
  <TaskExecutionSummary
    v-else-if="message.type === 'task-summary'"
    :content="taskSummaryContent"
    :expanded="taskSummaryExpanded"
    @toggle="$emit('taskSummaryToggle')"
  />
  <div v-else-if="message.type === 'attachments' && attachmentsContent.role === 'assistant'" class="flex flex-col gap-2 w-full group" :class="hideAssistantHeader ? 'mt-0' : 'mt-3'">
    <AttachmentsMessage :content="attachmentsContent" :hideAllFilesButton="hideAllFilesButton"/>
  </div>
  <AttachmentsMessage v-else-if="message.type === 'attachments'" :content="attachmentsContent" :hideAllFilesButton="hideAllFilesButton"/>
</template>

<script setup lang="ts">
import { Message, MessageContent, AttachmentsContent, TaskSummaryContent } from '../types/message';
import ToolUse from './ToolUse.vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { CheckIcon, Copy as CopyIcon, Share2 as Share2Icon, ShieldAlert, ThumbsDown, ThumbsUp, X } from 'lucide-vue-next';
import { computed, onMounted, onUnmounted, ref, watch, type Component } from 'vue';
import { ToolContent, StepContent } from '../types/message';
import { useRelativeTime } from '../composables/useTime';
import AttachmentsMessage from './AttachmentsMessage.vue';
import TaskExecutionSummary from './TaskExecutionSummary.vue';
import { copyToClipboard } from '../utils/dom';
import { stripHiddenDatasetResultNotices } from '../utils/datasetResultPresentation';
import { showErrorToast, showSuccessToast } from '../utils/toast';
import { deleteTaskFeedback, getTaskFeedback, saveTaskFeedback, shareSession, type TaskFeedbackPreference } from '../api/agent';


const props = defineProps<{
  message: Message;
  sessionId?: string;
  assistantIcon?: Component;
  assistantName?: string;
  hideAllFilesButton?: boolean;
  hideHeader?: boolean;
  showAssistantActions?: boolean;
  taskSummaryExpanded?: boolean;
}>();

const hideAssistantHeader = computed(() => props.hideHeader ?? false);

const emit = defineEmits<{
  (e: 'toolClick', tool: ToolContent): void;
  (e: 'taskSummaryToggle'): void;
}>();

const handleToolClick = (tool: ToolContent) => {
  emit('toolClick', tool);
};

const copied = ref(false);
let copiedTimer: ReturnType<typeof setTimeout> | null = null;

const copyUserMessage = async () => {
  const text = messageContent.value.content;
  if (!text) return;
  const success = await copyToClipboard(text);
  if (!success) {
    showErrorToast('复制失败，请检查浏览器剪贴板权限');
    return;
  }
  copied.value = true;
  if (copiedTimer) clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => {
    copied.value = false;
    copiedTimer = null;
  }, 1500);
};

const assistantCopied = ref(false);
let assistantCopiedTimer: ReturnType<typeof setTimeout> | null = null;

const copyAssistantMessage = async () => {
  const text = visibleAssistantContent.value;
  if (!text) return;
  const success = await copyToClipboard(text);
  if (!success) {
    showErrorToast('复制失败，请检查浏览器剪贴板权限');
    return;
  }
  assistantCopied.value = true;
  if (assistantCopiedTimer) clearTimeout(assistantCopiedTimer);
  assistantCopiedTimer = setTimeout(() => {
    assistantCopied.value = false;
    assistantCopiedTimer = null;
  }, 1500);
};

const feedbackPreference = ref<TaskFeedbackPreference | null>(null);
const feedbackDialogOpen = ref(false);
const feedbackSaving = ref(false);
const dislikeDetail = ref('');
const selectedDislikeReasons = ref<string[]>([]);
const dislikeReasonOptions = ['不正确或不完整', '与期望不符', '速度慢或存在问题', '风格或语气', '安全或法律疑虑', '其他'];

const loadTaskFeedback = async () => {
  if (!props.sessionId) return;
  try {
    const feedback = await getTaskFeedback(props.sessionId);
    feedbackPreference.value = feedback.preference;
    selectedDislikeReasons.value = feedback.dislike_reasons;
    dislikeDetail.value = feedback.detail;
  } catch {
    // Feedback availability must not block reading a response.
  }
};

const toggleLike = async () => {
  if (!props.sessionId || feedbackSaving.value) return;
  feedbackSaving.value = true;
  try {
    if (feedbackPreference.value === 'like') {
      await deleteTaskFeedback(props.sessionId);
      feedbackPreference.value = null;
    } else {
      await saveTaskFeedback(props.sessionId, { preference: 'like' });
      feedbackPreference.value = 'like';
      selectedDislikeReasons.value = [];
      dislikeDetail.value = '';
    }
  } catch {
    showErrorToast('保存反馈失败，请稍后重试');
  } finally {
    feedbackSaving.value = false;
  }
};

const toggleDislike = async () => {
  if (!props.sessionId || feedbackSaving.value) return;
  if (feedbackPreference.value === 'dislike') {
    feedbackSaving.value = true;
    try {
      await deleteTaskFeedback(props.sessionId);
      feedbackPreference.value = null;
    } catch {
      showErrorToast('取消反馈失败，请稍后重试');
    } finally {
      feedbackSaving.value = false;
    }
    return;
  }
  feedbackDialogOpen.value = true;
};

const toggleDislikeReason = (reason: string) => {
  selectedDislikeReasons.value = selectedDislikeReasons.value.includes(reason)
    ? selectedDislikeReasons.value.filter((item) => item !== reason)
    : [...selectedDislikeReasons.value, reason];
};

const closeFeedbackDialog = () => { feedbackDialogOpen.value = false; };

const submitDislikeFeedback = async () => {
  if (!props.sessionId || feedbackSaving.value) return;
  feedbackSaving.value = true;
  try {
    await saveTaskFeedback(props.sessionId, {
      preference: 'dislike',
      dislike_reasons: selectedDislikeReasons.value,
      detail: dislikeDetail.value,
    });
    feedbackPreference.value = 'dislike';
    feedbackDialogOpen.value = false;
  } catch {
    showErrorToast('提交反馈失败，请稍后重试');
  } finally {
    feedbackSaving.value = false;
  }
};

const shareTask = async () => {
  if (!props.sessionId) return;
  const url = `${window.location.origin}/share/${props.sessionId}`;
  try {
    await shareSession(props.sessionId);
  } catch {
    showErrorToast('创建任务分享链接失败，请稍后重试');
    return;
  }
  if (navigator.share) {
    try {
      await navigator.share({ title: 'DataSeek 任务', url });
      return;
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
    }
  }
  const copiedToClipboard = await copyToClipboard(url);
  if (copiedToClipboard) {
    showSuccessToast('任务分享链接已复制');
    return;
  }
  showErrorToast('复制任务分享链接失败，请检查浏览器剪贴板权限');
};

onMounted(() => {
  if (props.showAssistantActions) void loadTaskFeedback();
});

watch(() => props.showAssistantActions, (visible, wasVisible) => {
  if (visible && !wasVisible) void loadTaskFeedback();
});

onUnmounted(() => {
  if (copiedTimer) clearTimeout(copiedTimer);
  if (assistantCopiedTimer) clearTimeout(assistantCopiedTimer);
});

// For backward compatibility, provide the original computed properties
const stepContent = computed(() => props.message.content as StepContent);
const messageContent = computed(() => props.message.content as MessageContent);
const visibleAssistantContent = computed(() => stripHiddenDatasetResultNotices(messageContent.value.content));
const safetyReview = computed(() => messageContent.value.metadata?.safety_review);
const safetyUnavailable = computed(() => safetyReview.value?.categories.includes('safety_review_unavailable') ?? false);

const safetyCategoryLabels: Record<string, string> = {
  malware_or_dangerous_execution: '恶意软件或危险执行',
  prompt_injection_or_jailbreak: '提示词注入或越狱',
  credential_or_secret_theft: '凭证或敏感信息获取',
  cyber_abuse: '网络攻击或滥用',
  sexual_or_obscene: '色情或淫秽内容',
  political_or_sensitive: '政治或敏感内容',
  policy_violation: '安全策略风险',
  safety_review_unavailable: '审核服务异常',
};

const safetyCategoryLabel = (category: string) => safetyCategoryLabels[category] || category;
const toolContent = computed(() => props.message.content as ToolContent);
const attachmentsContent = computed(() => props.message.content as AttachmentsContent);
const taskSummaryContent = computed(() => props.message.content as TaskSummaryContent);

type DisplayToolItem = {
  tool: ToolContent;
  panelTool: ToolContent;
  count: number;
  summary?: string;
};

const fileMutationFunctions = new Set(['file_write', 'file_str_replace']);

const getToolFilePath = (tool: ToolContent): string => {
  return tool.args?.file || '';
};

const shouldGroupFileMutation = (previous: ToolContent, current: ToolContent): boolean => {
  if (previous.name !== 'file' || current.name !== 'file') return false;
  if (!fileMutationFunctions.has(previous.function) || !fileMutationFunctions.has(current.function)) return false;
  const previousFile = getToolFilePath(previous);
  return !!previousFile && previousFile === getToolFilePath(current);
};

const createGroupedTool = (tools: ToolContent[]): DisplayToolItem => {
  const latest = tools[tools.length - 1];
  const first = tools[0];
  const filePath = getToolFilePath(latest);
  return {
    tool: {
      ...latest,
      tool_call_id: `${first.tool_call_id}-group-${tools.length}`,
      function: 'file_write',
      args: { ...latest.args, file: filePath },
      timestamp: latest.timestamp,
    },
    panelTool: latest,
    count: tools.length,
    summary: `连续写入 ${tools.length} 次`,
  };
};

const displayTools = computed<DisplayToolItem[]>(() => {
  const items: DisplayToolItem[] = [];
  let group: ToolContent[] = [];

  const flushGroup = () => {
    if (group.length === 0) return;
    items.push(group.length > 1 ? createGroupedTool(group) : { tool: group[0], panelTool: group[0], count: 1 });
    group = [];
  };

  for (const tool of stepContent.value.tools || []) {
    if (group.length === 0) {
      group.push(tool);
      continue;
    }
    if (shouldGroupFileMutation(group[group.length - 1], tool)) {
      group.push(tool);
    } else {
      flushGroup();
      group.push(tool);
    }
  }
  flushGroup();
  return items;
});

// Control content expand/collapse state
const { relativeTime } = useRelativeTime();

const renderer = new marked.Renderer();
renderer.link = ({ href, title, text }: { href: string; title?: string | null; text: string }) => {
  const titleAttr = title ? ` title="${title}"` : '';
  return `<a href="${href}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`;
};

const renderMarkdown = (text: string) => {
  if (typeof text !== 'string') return '';
  const html = marked(text, { renderer }) as string;
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
};
</script>

<style>
.duration-300 {
  animation-duration: .3s;
}

.duration-300 {
  transition-duration: .3s;
}
</style>
