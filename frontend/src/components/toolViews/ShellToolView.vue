<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]">
    <div class="flex-1 flex items-center justify-center">
      <div class="max-w-[250px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center">{{
        shellSessionId }}
      </div>
    </div>
  </div>
  <div class="flex-1 min-h-0 w-full overflow-y-auto">
    <div dir="ltr" data-orientation="horizontal" class="flex flex-col flex-1 min-h-0">
      <div data-state="active" data-orientation="horizontal" role="tabpanel"
        id="radix-:r5m:-content-setup" tabindex="0"
        class="py-2 focus-visible:outline-none data-[state=inactive]:hidden flex-1 font-mono text-sm leading-relaxed px-3 outline-none overflow-auto whitespace-pre-wrap break-all"
        style="animation-duration: 0s;">
        <code v-html="shell"></code>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch, onUnmounted } from 'vue';
import { viewShellSession } from '@/api/agent';
import { ToolContent } from '@/types/message';
import { sanitizeToolDisplayText } from '@/utils/toolDisplay';
//import { showErrorToast } from '@/utils/toast';

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

defineExpose({
  loadContent: () => {
    loadShellContent();
  }
});

const shell = ref('');
const refreshTimer = ref<ReturnType<typeof setInterval> | null>(null);
const loadVersion = ref(0);

// Get shellSessionId from toolContent
const shellSessionId = computed(() => {
  if (props.toolContent && props.toolContent.args.id) {
    return props.toolContent.args.id;
  }
  return '';
});

const toolIdentity = computed(() => [
  props.toolContent.tool_call_id,
  props.toolContent.status,
  props.live ? 'live' : 'snapshot',
].join(':'));

const shouldLoadLiveShellSession = computed(() => props.live && props.toolContent.function === 'shell_view');

const escapeHtml = (value: unknown) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

const updateShellContent = (console: any) => {
  if (!console) {
    shell.value = '';
    return;
  }

  if (typeof console === 'string') {
    shell.value = escapeHtml(sanitizeToolDisplayText(console));
    return;
  }

  if (!Array.isArray(console)) {
    shell.value = escapeHtml(sanitizeToolDisplayText(console?.output ?? ''));
    return;
  }

  let newShell = '';
  for (const e of console) {
    const prompt = escapeHtml(sanitizeToolDisplayText(e?.ps1));
    const command = escapeHtml(sanitizeToolDisplayText(e?.command));
    const output = escapeHtml(sanitizeToolDisplayText(e?.output));
    if (prompt || command) {
      newShell += `<span style="color: rgb(0, 187, 0);">${prompt}</span>${command ? `<span> ${command}</span>` : ''}\n`;
    }
    if (output) {
      newShell += `<span>${output}</span>\n`;
    }
  }
  if (newShell !== shell.value) {
    shell.value = newShell;
  }
}

const showPendingShellContent = () => {
  const command = sanitizeToolDisplayText(props.toolContent.args?.command);
  shell.value = command
    ? `<span style="color: rgb(0, 187, 0);">$</span><span> ${escapeHtml(command)}</span>\n<span>Waiting for command output...</span>`
    : 'Waiting for command output...';
};

const showCommandSnapshotFallback = () => {
  const command = sanitizeToolDisplayText(props.toolContent.args?.command);
  if (!command) {
    shell.value = '';
    return;
  }
  const statusText = props.toolContent.status === 'called'
    ? 'Command completed with no console output.'
    : 'Waiting for command output...';
  shell.value = `<span style="color: rgb(0, 187, 0);">$</span><span> ${escapeHtml(command)}</span>\n<span>${statusText}</span>`;
};

// Function to load Shell session content
const loadShellContent = async () => {
  const version = ++loadVersion.value;
  if (!shouldLoadLiveShellSession.value) {
    updateShellContent(props.toolContent.content?.console);
    if (!shell.value) {
      showCommandSnapshotFallback();
    }
    return;
  }
  
  if (!shellSessionId.value) {
    shell.value = '';
    return;
  }

  try {
    const response = await viewShellSession(props.sessionId, shellSessionId.value);
    if (version !== loadVersion.value) return;
    updateShellContent(response.console);
    if (!shell.value && props.toolContent.status === 'calling') {
      showPendingShellContent();
    }
  } catch (error) {
    if (version !== loadVersion.value) return;
    console.error("Failed to load shell content:", error);
  }
};

// Start auto-refresh timer
const startAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
  }
  
  if (shouldLoadLiveShellSession.value && shellSessionId.value) {
    refreshTimer.value = setInterval(() => {
      loadShellContent();
    }, 5000);
  }
};

// Stop auto-refresh timer
const stopAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
    refreshTimer.value = null;
  }
};

watch(toolIdentity, () => {
  shell.value = '';
  loadVersion.value += 1;
  loadShellContent();
  startAutoRefresh();
});

watch(() => props.toolContent.timestamp, () => {
  loadShellContent();
});

// Watch for live prop changes
watch(() => props.live, (live: boolean) => {
  if (live && shouldLoadLiveShellSession.value) {
    loadShellContent();
    startAutoRefresh();
  } else {
    loadShellContent();
    stopAutoRefresh();
  }
});

// Load content and set up refresh timer when component is mounted
onMounted(() => {
  loadShellContent();
  startAutoRefresh();
});

// Clear timer when component is unmounted
onUnmounted(() => {
  stopAutoRefresh();
});
</script>
