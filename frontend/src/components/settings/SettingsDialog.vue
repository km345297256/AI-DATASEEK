<template>
  <Dialog v-model:open="isSettingsDialogOpen">
    <DialogContent class="w-[calc(100vw-24px)] md:w-[95vw] md:max-w-[920px]">
      <DialogTitle></DialogTitle>
      <DialogDescription></DialogDescription>
      
      <SettingsTabs 
        :key="settingsDialogVersion"
        :tabs="tabs" 
        :default-tab="normalizedDefaultTab">

        <template #settings>
          <GeneralSettings />
        </template>

        <template #skills>
          <SkillSettings />
        </template>

        <template #agent-profiles>
          <AgentProfileRuntimeSettings />
        </template>

      </SettingsTabs>
      
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Settings2, Puzzle, BotMessageSquare } from 'lucide-vue-next'
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { useSettingsDialog } from '@/composables/useSettingsDialog'
import SettingsTabs from './SettingsTabs.vue'
import GeneralSettings from './GeneralSettings.vue'
import SkillSettings from './SkillSettings.vue'
import AgentProfileRuntimeSettings from './AgentProfileRuntimeSettings.vue'
import type { TabItem } from './SettingsTabs.vue'

// Use global settings dialog state
const { isSettingsDialogOpen, defaultTab, settingsDialogVersion } = useSettingsDialog()

// Tab configuration
const tabs: TabItem[] = [
  {
    id: 'settings',
    label: 'Settings',
    icon: Settings2
  },
  {
    id: 'skills',
    label: 'Skills',
    icon: Puzzle
  },
  {
    id: 'agent-profiles',
    label: 'Agent Profiles',
    icon: BotMessageSquare
  }
]

const allowedTabs = new Set(tabs.map((tab) => tab.id))
const normalizedDefaultTab = computed(() => allowedTabs.has(defaultTab.value) ? defaultTab.value : 'settings')
</script>
