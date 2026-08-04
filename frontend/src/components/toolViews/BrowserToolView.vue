<template>
  <div class="flex h-9 items-center border-b border-[var(--border-main)] bg-[var(--background-gray-main)] px-3">
    <div class="flex-1 truncate text-center text-sm font-medium text-[var(--text-tertiary)]">
      {{ toolContent?.args?.url || 'Sandbox Browser' }}
    </div>
  </div>
  <div class="min-h-0 flex-1 overflow-y-auto">
    <div class="relative flex h-full flex-col px-0 py-0">
      <div class="relative flex h-full w-full items-center justify-center bg-[var(--fill-white)] object-cover">
        <div class="h-full w-full">
          <VNCViewer
            v-if="live"
            :session-id="sessionId"
            :enabled="live"
            :view-only="true"
            @connected="onVNCConnected"
            @disconnected="onVNCDisconnected"
            @credentials-required="onVNCCredentialsRequired"
          />
          <img v-else-if="imageUrl" alt="Browser screenshot" class="h-full w-full object-contain" referrerpolicy="no-referrer" :src="imageUrl" />
          <div v-else class="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--text-tertiary)]">
            {{ t('No browser image') }}
          </div>
        </div>
        <button
          v-if="!isShare"
          class="group absolute bottom-[10px] right-[10px] z-10 flex h-10 min-w-10 cursor-pointer items-center justify-center rounded-full border border-[var(--border-main)] bg-[var(--background-white-main)] text-[var(--text-primary)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] backdrop-blur-3xl transition-all duration-300 hover:bg-[var(--text-brand)] hover:px-4 hover:text-[var(--text-white)]"
          @click="takeOver"
        >
          <TakeOverIcon />
          <span class="max-w-0 overflow-hidden whitespace-nowrap text-sm opacity-0 transition-all duration-300 group-hover:ml-1 group-hover:max-w-[200px] group-hover:text-[var(--text-white)] group-hover:opacity-100">{{ t('Take Over') }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ToolContent } from '@/types/message';
import VNCViewer from '@/components/VNCViewer.vue';
import TakeOverIcon from '@/components/icons/TakeOverIcon.vue';

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
  isShare: boolean;
}>();

const { t } = useI18n();
const imageUrl = ref('');

watch(
  () => props.toolContent?.content?.screenshot,
  (screenshot) => { imageUrl.value = typeof screenshot === 'string' ? screenshot : ''; },
  { immediate: true },
);

function onVNCConnected() { console.debug('Sandbox VNC connected'); }
function onVNCDisconnected(reason?: unknown) { console.debug('Sandbox VNC disconnected', reason); }
function onVNCCredentialsRequired() { console.debug('Sandbox VNC credentials required'); }

function takeOver() {
  window.dispatchEvent(new CustomEvent('takeover', { detail: { sessionId: props.sessionId, active: true } }));
}
</script>
