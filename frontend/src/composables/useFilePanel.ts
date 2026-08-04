import { ref } from 'vue'
import type { FileInfo } from '../api/file'
import { eventBus } from '../utils/eventBus'
import { EVENT_SHOW_FILE_PANEL } from '../constants/event'

const isShow = ref(false)
const visible = ref(true)
const fileInfo = ref<FileInfo>()
const relatedFiles = ref<FileInfo[]>([])

export function useFilePanel() {
  const showFilePanel = (file: FileInfo, files: FileInfo[] = []) => {
    eventBus.emit(EVENT_SHOW_FILE_PANEL)
    visible.value = true
    fileInfo.value = file
    relatedFiles.value = files
    isShow.value = true
  }

  const hideFilePanel = () => {
    isShow.value = false
  }

  return {
    isShow,
    fileInfo,
    relatedFiles,
    visible,
    showFilePanel,
    hideFilePanel
  }
} 
