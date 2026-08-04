<template>
  <Dialog v-model:open="isSettingsDialogOpen">
    <DialogContent class="w-[380px] md:w-[95vw] md:max-w-[920px]">
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

      </SettingsTabs>
      
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Settings2, Puzzle } from 'lucide-vue-next'
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
  }
]

const allowedTabs = new Set(tabs.map((tab) => tab.id))
const normalizedDefaultTab = computed(() => allowedTabs.has(defaultTab.value) ? defaultTab.value : 'settings')
</script>
