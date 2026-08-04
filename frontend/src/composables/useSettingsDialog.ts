import { ref } from 'vue'

// Global state for settings dialog
const isSettingsDialogOpen = ref(false)
const defaultTab = ref<string>('settings')
const settingsDialogVersion = ref(0)

export function useSettingsDialog() {
  const openSettingsDialog = (tabId?: string) => {
    if (tabId) {
      defaultTab.value = tabId
    }
    settingsDialogVersion.value += 1
    isSettingsDialogOpen.value = true
  }

  const closeSettingsDialog = () => {
    isSettingsDialogOpen.value = false
  }

  const toggleSettingsDialog = () => {
    isSettingsDialogOpen.value = !isSettingsDialogOpen.value
  }

  const setDefaultTab = (tabId: string) => {
    defaultTab.value = tabId
  }

  return {
    isSettingsDialogOpen,
    defaultTab,
    settingsDialogVersion,
    openSettingsDialog,
    closeSettingsDialog,
    toggleSettingsDialog,
    setDefaultTab
  }
}
