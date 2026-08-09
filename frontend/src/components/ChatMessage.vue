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
    <div v-if="!hideAssistantHeader" class="flex items-center justify-between h-7 group">
      <div class="flex items-center gap-[3px]">
        <component v-if="assistantIcon" :is="assistantIcon" :size="24" class="w-6 h-6" />
        <ManusTextIcon v-else />
        <span v-if="assistantName" class="text-base text-[var(--text-primary)] tracking-tight leading-none ml-0.5">{{ assistantName }}</span>
      </div>
      <div class="flex items-center gap-[2px] invisible group-hover:visible">
        <div class="float-right transition text-[12px] text-[var(--text-tertiary)] invisible group-hover:visible">
          {{ relativeTime(message.content.timestamp) }}
        </div>
      </div>
    </div>
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
    <div v-else
      class="max-w-none p-0 m-0 prose prose-sm sm:prose-base dark:prose-invert [&_pre:not(.shiki)]:!bg-[var(--fill-tsp-white-light)] [&_pre:not(.shiki)]:text-[var(--text-primary)] text-base text-[var(--text-primary)]"
      v-html="renderMarkdown(visibleAssistantContent)"></div>
  </div>
  <ToolUse v-else-if="message.type === 'tool'" :tool="toolContent" @click="handleToolClick(toolContent)" />
  <div v-else-if="message.type === 'step'" class="flex flex-col">
    <div class="text-sm w-full clickable flex gap-2 justify-between group/header truncate text-[var(--text-primary)]"
      data-event-id="HNtP7XOMUOhPemItd2EkK2">
      <div class="flex flex-row gap-2 justify-center items-center truncate">
        <div v-if="stepContent.status !== 'completed'"
          class="w-4 h-4 flex-shrink-0 flex items-center justify-center border border-[var(--border-dark)] rounded-[15px]">
        </div>
        <div v-else
          class="w-4 h-4 flex-shrink-0 flex items-center justify-center border-[var(--border-dark)] rounded-[15px] bg-[var(--text-disable)] dark:bg-[var(--fill-tsp-white-dark)] border-0">
          <CheckIcon class="text-[var(--icon-white)] dark:text-[var(--icon-white-tsp)]" :size="10" />
        </div>
        <div class="truncate font-medium markdown-content"
          v-html="stepContent.description ? renderMarkdown(stepContent.description) : ''">
        </div>
        <span class="flex-shrink-0 flex" @click="isExpanded = !isExpanded;">
          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            class="lucide lucide-chevron-down transition-transform duration-300 w-4 h-4"
            :class="{ 'rotate-180': isExpanded }">
            <path d="m6 9 6 6 6-6"></path>
          </svg>
        </span>
      </div>
      <div class="float-right transition text-[12px] text-[var(--text-tertiary)] invisible group-hover/header:visible">
        {{ relativeTime(message.content.timestamp) }}
      </div>
    </div>
    <div class="flex">
      <div class="w-[24px] relative">
        <div class="border-l border-dashed border-[var(--border-dark)] absolute start-[8px] top-0 bottom-0"
          style="height: calc(100% + 14px);"></div>
      </div>
      <div
        class="flex flex-col gap-3 flex-1 min-w-0 overflow-hidden pt-2 transition-[max-height,opacity] duration-150 ease-in-out"
        :class="{ 'max-h-[100000px] opacity-100': isExpanded, 'max-h-0 opacity-0': !isExpanded }">
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
  <div v-else-if="message.type === 'attachments' && attachmentsContent.role === 'assistant'" class="flex flex-col gap-2 w-full group" :class="hideAssistantHeader ? 'mt-0' : 'mt-3'">
    <div v-if="!hideAssistantHeader" class="flex items-center justify-between h-7 group">
      <div class="flex items-center gap-[3px]">
        <component v-if="assistantIcon" :is="assistantIcon" :size="24" class="w-6 h-6" />
        <ManusTextIcon v-else />
        <span v-if="assistantName" class="text-base text-[var(--text-primary)] tracking-tight leading-none ml-0.5">{{ assistantName }}</span>
      </div>
      <div class="flex items-center gap-[2px] invisible group-hover:visible">
        <div class="float-right transition text-[12px] text-[var(--text-tertiary)] invisible group-hover:visible">
          {{ relativeTime(attachmentsContent.timestamp) }}
        </div>
      </div>
    </div>
    <AttachmentsMessage :content="attachmentsContent" :hideAllFilesButton="hideAllFilesButton"/>
  </div>
  <AttachmentsMessage v-else-if="message.type === 'attachments'" :content="attachmentsContent" :hideAllFilesButton="hideAllFilesButton"/>
</template>

<script setup lang="ts">
import ManusTextIcon from './icons/ManusTextIcon.vue';
import { Message, MessageContent, AttachmentsContent } from '../types/message';
import ToolUse from './ToolUse.vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { CheckIcon, Copy as CopyIcon, ShieldAlert } from 'lucide-vue-next';
import { computed, onUnmounted, ref, type Component } from 'vue';
import { ToolContent, StepContent } from '../types/message';
import { useRelativeTime } from '../composables/useTime';
import AttachmentsMessage from './AttachmentsMessage.vue';
import { copyToClipboard } from '../utils/dom';
import { stripHiddenDatasetResultNotices } from '../utils/datasetResultPresentation';
import { showErrorToast } from '../utils/toast';


const props = defineProps<{
  message: Message;
  sessionId?: string;
  assistantIcon?: Component;
  assistantName?: string;
  hideAllFilesButton?: boolean;
  hideHeader?: boolean;
}>();

const hideAssistantHeader = computed(() => props.hideHeader ?? false);

const emit = defineEmits<{
  (e: 'toolClick', tool: ToolContent): void;
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

onUnmounted(() => {
  if (copiedTimer) clearTimeout(copiedTimer);
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
const isExpanded = ref(true);

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
