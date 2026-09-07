import { getCurrentScope, onScopeDispose, ref } from 'vue'
import type { Router } from 'vue-router'
import type { FileInfo } from '../api/file'
import { eventBus } from '../utils/eventBus.ts'
import { EVENT_SHOW_FILE_PANEL } from '../constants/event.ts'

const isShow = ref(false)
const visible = ref(true)
const fileInfo = ref<FileInfo>()
const relatedFiles = ref<FileInfo[]>([])
let previewGeneration = 0

const resetFilePanel = () => {
  previewGeneration += 1
  isShow.value = false
  fileInfo.value = undefined
  relatedFiles.value = []
  visible.value = true
}

// Layout components may be replaced entirely during navigation. Their watchers
// cannot own the lifetime of this shared state.
export function installFilePanelRouteLifecycle(router: Router) {
  return router.afterEach((to, from, failure) => {
    if (!failure && to.fullPath !== from.fullPath) resetFilePanel()
  })
}

export function useFilePanel() {
  let active = true
  if (getCurrentScope()) onScopeDispose(() => { active = false })

  const hideFilePanel = () => {
    if (active) resetFilePanel()
  }

  const showFilePanel = (file: FileInfo, files: FileInfo[] = []) => {
    if (!active) return false
    previewGeneration += 1
    eventBus.emit(EVENT_SHOW_FILE_PANEL)
    visible.value = true
    fileInfo.value = file
    relatedFiles.value = files
    isShow.value = true
    return true
  }

  // Capture before asynchronous preparation. Closing, navigating, changing the
  // conversation or opening another file makes a late response obsolete.
  const beginFilePreview = () => {
    const generation = active ? ++previewGeneration : previewGeneration
    const isCurrent = () => active && generation === previewGeneration
    return {
      isCurrent,
      show: (file: FileInfo, files: FileInfo[] = []) => {
        if (!isCurrent()) return false
        return showFilePanel(file, files)
      },
    }
  }

  return {
    isShow,
    fileInfo,
    relatedFiles,
    visible,
    showFilePanel,
    beginFilePreview,
    hideFilePanel
  }
} 
