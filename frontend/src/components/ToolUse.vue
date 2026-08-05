<template>
  <p v-if="tool.name === 'message' && tool.args?.text" class="text-[var(--text-secondary)] text-[14px] overflow-hidden text-ellipsis whitespace-pre-line">
    {{ tool.args.text }}
  </p>
  <div v-else-if="toolInfo" class="flex items-start group gap-2">
    <div class="flex-1 min-w-0">
      <div @click="handleClick"
        class="rounded-[15px] items-center gap-2 px-[10px] py-[4px] border border-[var(--border-light)] bg-[var(--fill-tsp-gray-main)] inline-flex max-w-full clickable hover:bg-[var(--fill-tsp-gray-dark)] dark:hover:bg-white/[0.02]">
        <div class="w-[16px] inline-flex items-center text-[var(--text-primary)]">
          <component :is="toolInfo.icon" :size="21" />
        </div>
        <div class="flex-1 h-full min-w-0 flex">
          <div
            class="inline-flex items-center h-full rounded-full text-[14px] text-[var(--text-secondary)] max-w-[100%]">
            <div class="max-w-[100%] text-ellipsis overflow-hidden whitespace-nowrap text-[13px]"
              :title="toolInfo.functionArg ? `${toolInfo.function} ${toolInfo.functionArg}` : toolInfo.function">
              <div class="flex items-center">
                {{ toolInfo.function }}<span v-if="toolInfo.functionArgPreview"
                  class="flex-1 min-w-0 rounded-[6px] px-1 ml-1 relative top-[0px] text-[12px] font-mono max-w-full text-ellipsis overflow-hidden whitespace-nowrap text-[var(--text-tertiary)]"><code>{{ toolInfo.functionArgPreview }}</code></span>
                <button
                  v-if="toolInfo.functionArgExpandable"
                  type="button"
                  class="ml-1 inline-flex size-5 shrink-0 items-center justify-center rounded-full text-[var(--icon-tertiary)] hover:bg-[var(--fill-tsp-white-light)] hover:text-[var(--icon-primary)]"
                  :class="detailsExpanded ? 'rotate-180' : ''"
                  :aria-expanded="detailsExpanded"
                  :aria-label="detailsExpanded ? '收起完整内容' : '展开完整内容'"
                  :title="detailsExpanded ? '收起完整内容' : '展开完整内容'"
                  @click.stop="detailsExpanded = !detailsExpanded"
                >
                  <ChevronDown :size="14" />
                </button>
                <span v-if="summary" class="ml-2 rounded-full bg-[var(--fill-tsp-white-light)] px-2 py-[1px] text-[11px] text-[var(--text-tertiary)]">
                  {{ summary }}
                </span>
                <span v-if="collapsedCount > 1" class="ml-1 rounded-full border border-[var(--border-main)] px-1.5 py-[1px] text-[11px] text-[var(--text-tertiary)]">
                  x{{ collapsedCount }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div
        v-if="detailsExpanded && toolInfo.functionArg"
        class="mt-1.5 max-w-full rounded-xl border border-[var(--border-light)] bg-[var(--background-gray-main)] px-3 py-2 text-[12px] leading-5 text-[var(--text-secondary)]"
      >
        <div class="mb-1 text-[11px] text-[var(--text-tertiary)]">完整动作内容（敏感信息已隐藏）</div>
        <pre class="max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono">{{ toolInfo.functionArg }}</pre>
      </div>
    </div>
    <div class="float-right transition text-[12px] text-[var(--text-tertiary)] invisible group-hover:visible">
      {{ relativeTime(tool.timestamp) }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, toRef, watch } from "vue";
import { ChevronDown } from 'lucide-vue-next';
import { ToolContent } from "../types/message";
import { useToolInfo } from "../composables/useTool";
import { useRelativeTime } from "../composables/useTime";

const props = withDefaults(defineProps<{
  tool: ToolContent;
  summary?: string;
  collapsedCount?: number;
}>(), {
  collapsedCount: 1,
});

const emit = defineEmits<{
  (e: "click"): void;
}>();

const { relativeTime } = useRelativeTime();
const { toolInfo } = useToolInfo(toRef(props, 'tool'));
const detailsExpanded = ref(false);

watch(() => props.tool.tool_call_id, () => {
  detailsExpanded.value = false;
});

const handleClick = () => {
  emit("click");
};
</script>
