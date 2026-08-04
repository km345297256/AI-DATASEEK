<template>
  <SimpleBar>
    <div
      class="flex flex-col h-full flex-1 min-w-0 mx-auto w-full sm:min-w-[390px] px-3 sm:px-5 justify-center items-start gap-2 relative max-w-full sm:max-w-full">
      <div class="mobile-safe-top w-full pb-3 sm:py-4 bg-[var(--background-gray-main)] sticky top-0 z-10">
        <div class="flex items-center w-full min-w-0">
          <div class="h-8 relative z-20 overflow-hidden flex gap-2 items-center min-w-0 flex-1">
            <div class="relative flex items-center">
              <button type="button" @click="toggleLeftPanel" v-if="!isLeftPanelShow"
                class="flex h-11 w-11 items-center justify-center cursor-pointer rounded-lg hover:bg-[var(--fill-tsp-gray-main)] sm:h-7 sm:w-7 sm:rounded-md"
                :aria-label="$t('Open navigation')">
                <PanelLeft class="size-5 text-[var(--icon-secondary)]" />
              </button>
            </div>
            <div class="flex items-center gap-2 min-w-0 flex-1">
              <ManusLogoTextIcon class="hidden sm:inline-flex" :width="148" :height="30" />
              <ManusIcon class="sm:hidden shrink-0" />
              <AgentSelector class="min-w-0 max-w-full" />
            </div>
          </div>
        </div>
      </div>
      <div class="flex w-full max-w-full flex-1 flex-col justify-center py-6 sm:block sm:max-w-[768px] sm:min-w-[390px] sm:flex-none sm:mx-auto sm:mt-[180px] sm:mb-auto sm:py-0">
        <div class="w-full flex pl-2 sm:pl-4 items-center justify-start pb-4">
          <span class="text-[var(--text-primary)] text-start font-serif text-[32px] leading-[40px]" :style="{
            fontFamily:
              'ui-serif, Georgia, Cambria, &quot;Times New Roman&quot;, Times, serif',
          }">
            {{ $t('Hello') }}, {{ currentUser?.fullname }}
            <br />
            <span class="text-[var(--text-tertiary)]">
              {{ $t('What can I do for you?') }}
            </span>
          </span>
        </div>
        <div class="flex flex-col gap-1 w-full">
          <div class="flex flex-col bg-[var(--background-gray-main)] w-full">
            <div class="[&amp;:not(:empty)]:pb-2 bg-[var(--background-gray-main)] rounded-[22px_22px_0px_0px]">
            </div>
            <ChatBox
              :rows="2"
              v-model="message"
              v-model:selected-skills="selectedSkills"
              v-model:selected-mcp-servers="selectedMcpServers"
              skill-menu-placement="down"
              @submit="handleSubmit"
              :isRunning="false"
              :attachments="attachments"
            />
          </div>
        </div>
      </div>
    </div>
  </SimpleBar>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import AgentSelector from '../components/AgentSelector.vue';
import { createSession } from '../api/agent';
import { showErrorToast } from '../utils/toast';
import { PanelLeft } from 'lucide-vue-next';
import ManusLogoTextIcon from '../components/icons/ManusLogoTextIcon.vue';
import ManusIcon from '../components/icons/ManusIcon.vue';
import type { FileInfo } from '../api/file';
import { useLeftPanel } from '../composables/useLeftPanel';
import { useFilePanel } from '../composables/useFilePanel';
import { useAuth } from '../composables/useAuth';
import { useAgentProfile } from '../composables/useAgentProfile';
import { savePendingChat } from '../composables/usePendingChat';

const { t } = useI18n();
const router = useRouter();
const message = ref('');
const isSubmitting = ref(false);
const attachments = ref<FileInfo[]>([]);
const selectedSkills = ref<string[]>([]);
const selectedMcpServers = ref<string[]>([]);
const { toggleLeftPanel, isLeftPanelShow } = useLeftPanel();
const { hideFilePanel } = useFilePanel();
const { currentUser } = useAuth();
const { selectedProfile } = useAgentProfile();

onMounted(() => {
  hideFilePanel();
})

const handleSubmit = async () => {
  if (message.value.trim() && !isSubmitting.value) {
    isSubmitting.value = true;

    try {
      // Create new Agent
      const session = await createSession(selectedProfile.value?.id ?? null);
      const sessionId = session.session_id;
      savePendingChat({
        sessionId,
        message: message.value,
        skills: selectedSkills.value,
        mcpServers: selectedMcpServers.value,
        agentProfileId: selectedProfile.value?.id ?? null,
        files: attachments.value.map((file: FileInfo) => ({
          file_id: file.file_id,
          filename: file.filename,
          content_type: file.content_type,
          size: file.size,
          upload_date: file.upload_date
        })),
      });

      // Navigate to new route with session_id, passing initial message via state
      router.push({
        path: `/chat/${sessionId}`
      });
    } catch (error) {
      console.error('Failed to create session:', error);
      showErrorToast(t('Failed to create session, please try again later'));
      isSubmitting.value = false;
    }
  }
};
</script>
