<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]"
  >
    <div class="flex-1 flex items-center justify-center">
      <div
        class="max-w-[250px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center"
      >
        {{ title }}
      </div>
    </div>
  </div>
  <div v-if="isFindByName" class="flex-1 min-h-0 w-full overflow-y-auto p-4">
    <div class="mb-3 rounded-lg bg-[var(--fill-tsp-gray-main)] px-3 py-2 text-xs text-[var(--text-tertiary)]">
      {{ searchPath }} · {{ globPattern }}
    </div>
    <div v-if="foundFiles.length" class="flex flex-col gap-2">
      <div
        v-for="file in foundFiles"
        :key="file"
        class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-2 font-mono text-xs text-[var(--text-secondary)]"
      >
        {{ file }}
      </div>
    </div>
    <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">
      {{ $t('No files found') }}
    </div>
  </div>
  <div v-else-if="isFindInContent" class="flex-1 min-h-0 w-full overflow-y-auto p-4">
    <div class="mb-3 rounded-lg bg-[var(--fill-tsp-gray-main)] px-3 py-2 text-xs text-[var(--text-tertiary)]">
      {{ filePath }} · {{ regexPattern }}
    </div>
    <div v-if="contentMatches.length" class="flex flex-col gap-2">
      <div
        v-for="match in contentMatches"
        :key="`${match.line}:${match.text}`"
        class="rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-3 py-2"
      >
        <div class="mb-1 text-[11px] text-[var(--text-tertiary)]">Line {{ match.line }}</div>
        <pre class="whitespace-pre-wrap font-mono text-xs text-[var(--text-secondary)]">{{ match.text }}</pre>
      </div>
    </div>
    <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">
      {{ $t('No matches found') }}
    </div>
  </div>
  <div v-else class="flex-1 min-h-0 w-full overflow-y-auto">
    <div
      dir="ltr"
      data-orientation="horizontal"
      class="flex flex-col min-h-0 h-full relative"
    >
      <div
        data-state="active"
        data-orientation="horizontal"
        role="tabpanel"
        :id="panelId"
        tabindex="0"
        class="focus-visible:outline-none data-[state=inactive]:hidden flex-1 min-h-0 h-full text-sm flex flex-col py-0 outline-none overflow-auto"
      >
        <section
          style="
            display: flex;
            position: relative;
            text-align: initial;
            width: 100%;
            height: 100%;
          "
        >
          <MonacoEditor
            :value="fileContent"
            :filename="fileName"
            :read-only="true"
            theme="vs"
            :line-numbers="'off'"
            :word-wrap="'on'"
            :minimap="false"
            :scroll-beyond-last-line="false"
            :automatic-layout="true"
          />
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch, onUnmounted } from "vue";
import { ToolContent } from "@/types/message";
import { viewFile } from "@/api/agent";
import MonacoEditor from "@/components/ui/MonacoEditor.vue";
//import { showErrorToast } from "../utils/toast";
//import { useI18n } from "vue-i18n";

//const { t } = useI18n();

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

defineExpose({
  loadContent: () => {
    loadFileContent();
  },
});

const fileContent = ref("");
const refreshTimer = ref<ReturnType<typeof setInterval> | null>(null);
const functionName = computed(() => props.toolContent.function || "");
const isFindByName = computed(() => functionName.value === "file_find_by_name");
const isFindInContent = computed(() => functionName.value === "file_find_in_content");
const isFilePreviewTool = computed(() => !isFindByName.value && !isFindInContent.value);

const filePath = computed(() => {
  if (props.toolContent && props.toolContent.args.file) {
    return props.toolContent.args.file;
  }
  return "";
});

const searchPath = computed(() => props.toolContent.args?.path || "");
const globPattern = computed(() => props.toolContent.args?.glob_pattern || props.toolContent.args?.glob || "");
const regexPattern = computed(() => props.toolContent.args?.regex || "");

const fileName = computed(() => {
  if (filePath.value) {
    return filePath.value.split("/").pop() || "";
  }
  return "";
});

const title = computed(() => {
  if (isFindByName.value) return searchPath.value || "Find files";
  if (isFindInContent.value) return fileName.value || "Search file content";
  return fileName.value;
});

const panelId = computed(() => `file-tool-content-${title.value || "empty"}`.replace(/[^a-zA-Z0-9_-]/g, "-"));

const parsedToolContent = computed(() => {
  const rawContent = props.toolContent.content?.content || "";
  if (!rawContent) return null;
  try {
    return JSON.parse(rawContent);
  } catch {
    return null;
  }
});

const resultData = computed(() => parsedToolContent.value?.data || parsedToolContent.value || {});
const foundFiles = computed<string[]>(() => resultData.value?.files || []);
const contentMatches = computed(() => {
  const matches: string[] = resultData.value?.matches || [];
  const lineNumbers: number[] = resultData.value?.line_numbers || [];
  return matches.map((text, index) => ({
    text,
    line: lineNumbers[index] ?? index + 1,
  }));
});

// Load file content
const loadFileContent = async () => {
  if (!isFilePreviewTool.value) {
    fileContent.value = "";
    return;
  }
  if (!props.live) {
    fileContent.value = props.toolContent.content?.content || "";
    return;
  }
  
  if (!filePath.value) return;
  
  try {
    const response = await viewFile(props.sessionId, filePath.value);
    fileContent.value = response.content;
  } catch (error) {
    console.error("Failed to load file content:", error);
  }
};

// Start auto-refresh timer
const startAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
  }
  
  if (props.live && filePath.value && isFilePreviewTool.value) {
    refreshTimer.value = setInterval(() => {
      loadFileContent();
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

// Watch for filename changes to reload content
watch(filePath, (newVal: string) => {
  if (newVal && isFilePreviewTool.value) {
    loadFileContent();
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});

watch(() => props.toolContent, () => {
  loadFileContent();
});

watch(() => props.toolContent.timestamp, () => {
  loadFileContent();
});

// Watch for live prop changes
watch(() => props.live, (live: boolean) => {
  if (live) {
    loadFileContent();
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});

// Load content when component is mounted
onMounted(() => {
  loadFileContent();
  startAutoRefresh();
});

onUnmounted(() => {
  stopAutoRefresh();
});
</script>
