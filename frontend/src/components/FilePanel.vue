<template>
  <div
    ref="filePanelRef"
    v-show="visible"
    :class="{
      'h-full w-full top-0 ltr:right-0 rtl:left-0 z-50 fixed': isShow,
      'sm:sticky sm:top-0 sm:h-[100vh]': isShow && !props.resizable,
      'lg:sticky lg:top-0 lg:h-[100vh]': isShow && props.resizable,
      'h-full overflow-hidden': !isShow 
    }"
    :style="{
      width: isShow ? `${panelWidth}px` : '0px',
      opacity: isShow ? '1' : '0',
      transition: isResizing ? 'none' : 'width 0.2s ease-in-out, opacity 0.2s ease-in-out',
    }"
  >
    <div
      v-if="isShow && props.resizable && !compactViewport"
      class="group absolute inset-y-0 left-0 z-20 hidden w-3 -translate-x-1/2 cursor-col-resize touch-none select-none lg:block"
      role="separator"
      aria-label="调整对话与文件预览宽度"
      aria-orientation="vertical"
      :aria-valuemin="minimumPanelWidth"
      :aria-valuemax="maximumPanelWidth"
      :aria-valuenow="panelWidth"
      tabindex="0"
      @pointerdown="startResize"
      @keydown="handleResizeKeydown"
    >
      <span
        class="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-[var(--border-main)] transition-[width,background-color] group-hover:w-0.5 group-hover:bg-[#4f8a70] group-focus-visible:w-0.5 group-focus-visible:bg-[#4f8a70]"
        :class="isResizing ? 'w-0.5 bg-[#2b7659]' : ''"
      />
      <span class="absolute left-1/2 top-1/2 h-12 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--background-menu-white)] shadow-sm ring-1 ring-[var(--border-main)]" />
    </div>
    <div class="h-full" :style="{ 'width': isShow ? '100%' : '0px' }">
      <div v-if="isShow && fileInfo && fileType" class="bg-[var(--background-gray-main)] overflow-hidden shadow-[0px_0px_8px_0px_rgba(0,0,0,0.02)] ltr:border-l rtl:border-r border-black/8 dark:border-[var(--border-light)] flex flex-col h-full w-full">
        <div
          class="px-4 pt-2 pb-4 gap-4 flex items-center justify-between flex-shrink-0 border-b border-[var(--border-main)] flex-col-reverse md:flex-row md:py-4">
          <div class="flex justify-between self-stretch flex-1 truncate">
            <div
              class="flex flex-row gap-1 items-center text-[var(--text-secondary)] font-medium truncate [&amp;_svg]:flex-shrink-0">
              <a href="" class="p-1 flex-shrink-0 cursor-default" target="_blank">
                <div class="relative flex items-center justify-center">
                  <component :is="fileType.icon" />
                </div>
              </a>
              <div class="truncate flex flex-col"><span class="truncate" :title="fileInfo.filename">{{ fileInfo.filename }}</span></div>
            </div>
          </div>
          <div class="flex items-center justify-between gap-2 w-full py-3 md:w-auto md:py-0 select-none">
            <div v-if="relatedFiles.length > 1" class="flex items-center gap-1 text-[11px] text-[var(--text-tertiary)]">
              <button
                type="button"
                class="flex h-7 w-7 items-center justify-center rounded-md hover:bg-[var(--fill-tsp-gray-main)] disabled:cursor-not-allowed disabled:opacity-35"
                :disabled="!hasPreviousFile"
                aria-label="上一个成果物"
                title="上一个成果物"
                @click="navigateFile(-1)"
              ><ChevronLeft class="size-4" /></button>
              <span class="min-w-10 text-center">{{ relatedIndex + 1 }} / {{ relatedFiles.length }}</span>
              <button
                type="button"
                class="flex h-7 w-7 items-center justify-center rounded-md hover:bg-[var(--fill-tsp-gray-main)] disabled:cursor-not-allowed disabled:opacity-35"
                :disabled="!hasNextFile"
                aria-label="下一个成果物"
                title="下一个成果物"
                @click="navigateFile(1)"
              ><ChevronRight class="size-4" /></button>
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="flex h-7 w-7 items-center justify-center rounded-md hover:bg-[var(--fill-tsp-gray-main)]"
                aria-label="下载成果物"
                title="下载成果物"
                @click="download"
              >
                <Download class="text-[var(--icon-secondary)] size-[18px]" />
              </button>
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="flex h-7 w-7 items-center justify-center rounded-md hover:bg-[var(--fill-tsp-gray-main)]"
                aria-label="关闭文件预览"
                title="关闭文件预览"
                @click="hideFilePanel"
              >
                <X class="size-5 text-[var(--icon-secondary)]" />
              </button>
            </div>
          </div>
        </div>
        <component :is="fileType.preview" :file="fileInfo" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { ChevronLeft, ChevronRight, Download, X } from 'lucide-vue-next'
import { useFilePanel } from '../composables/useFilePanel'
import { getFileDownloadUrl } from '../api/file'
import { getFileType } from '../utils/fileType'
import { useResizeObserver } from '../composables/useResizeObserver'
import { eventBus } from '../utils/eventBus'
import { EVENT_SHOW_TOOL_PANEL } from '../constants/event'

const props = withDefaults(defineProps<{
  resizable?: boolean
  reservedWidth?: number
  minPanelWidth?: number
  minContentWidth?: number
}>(), {
  resizable: false,
  reservedWidth: 0,
  minPanelWidth: 360,
  minContentWidth: 420,
})

const {
  isShow,
  fileInfo,
  relatedFiles,
  visible,
  showFilePanel,
  hideFilePanel
} = useFilePanel()

const filePanelRef = ref<HTMLElement>()
const { size: parentSize } = useResizeObserver(filePanelRef, {
  target: 'parent',
  property: 'width'
})

const requestedPanelWidth = ref<number | null>(null)
const isResizing = ref(false)
let resizeStartX = 0
let resizeStartWidth = 0
let resizePointerId: number | null = null
let resizePointerTarget: HTMLElement | null = null
let previousBodyCursor = ''
let previousBodyUserSelect = ''

const compactViewport = computed(() => props.resizable && parentSize.value < 1024)
const maximumPanelWidth = computed(() => {
  if (!props.resizable) return Math.max(0, parentSize.value / 2)
  if (compactViewport.value) return Math.max(0, parentSize.value)
  return Math.max(240, parentSize.value - props.reservedWidth - props.minContentWidth)
})
const minimumPanelWidth = computed(() => Math.min(props.minPanelWidth, maximumPanelWidth.value))
const panelWidth = computed(() => {
  if (!isShow.value) return 0
  if (!props.resizable) return Math.max(0, Math.round(parentSize.value / 2))
  if (compactViewport.value) return Math.max(0, Math.round(parentSize.value))
  const preferredWidth = requestedPanelWidth.value ?? parentSize.value / 2
  return Math.round(Math.min(maximumPanelWidth.value, Math.max(minimumPanelWidth.value, preferredWidth)))
})

const fileType = computed(() => {
  if (!fileInfo.value) return null
  return getFileType(fileInfo.value.filename)
})

const relatedIndex = computed(() => {
  if (!fileInfo.value || !relatedFiles.value.length) return -1
  return relatedFiles.value.findIndex(file => file.file_id === fileInfo.value?.file_id)
})
const hasPreviousFile = computed(() => relatedIndex.value > 0)
const hasNextFile = computed(() => relatedIndex.value >= 0 && relatedIndex.value < relatedFiles.value.length - 1)

const navigateFile = (offset: -1 | 1) => {
  const nextFile = relatedFiles.value[relatedIndex.value + offset]
  if (nextFile) fileInfo.value = nextFile
}

const setPanelWidth = (width: number) => {
  requestedPanelWidth.value = Math.min(maximumPanelWidth.value, Math.max(minimumPanelWidth.value, width))
}

const handleResizeMove = (event: PointerEvent) => {
  if (!isResizing.value) return
  setPanelWidth(resizeStartWidth + resizeStartX - event.clientX)
}

const stopResize = () => {
  if (!isResizing.value) return
  isResizing.value = false
  window.removeEventListener('pointermove', handleResizeMove)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
  window.removeEventListener('blur', stopResize)
  if (resizePointerTarget && resizePointerId !== null && resizePointerTarget.hasPointerCapture(resizePointerId)) {
    resizePointerTarget.releasePointerCapture(resizePointerId)
  }
  resizePointerTarget = null
  resizePointerId = null
  document.body.style.cursor = previousBodyCursor
  document.body.style.userSelect = previousBodyUserSelect
}

const startResize = (event: PointerEvent) => {
  if (!props.resizable || compactViewport.value || event.button !== 0) return
  event.preventDefault()
  resizeStartX = event.clientX
  resizeStartWidth = panelWidth.value
  isResizing.value = true
  resizePointerId = event.pointerId
  resizePointerTarget = event.currentTarget as HTMLElement
  resizePointerTarget.setPointerCapture(event.pointerId)
  previousBodyCursor = document.body.style.cursor
  previousBodyUserSelect = document.body.style.userSelect
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', handleResizeMove)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
  window.addEventListener('blur', stopResize)
}

const handleResizeKeydown = (event: KeyboardEvent) => {
  if (!props.resizable || compactViewport.value) return
  if (event.key === 'ArrowLeft') {
    event.preventDefault()
    setPanelWidth(panelWidth.value + 24)
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    setPanelWidth(panelWidth.value - 24)
  } else if (event.key === 'Home') {
    event.preventDefault()
    setPanelWidth(minimumPanelWidth.value)
  } else if (event.key === 'End') {
    event.preventDefault()
    setPanelWidth(maximumPanelWidth.value)
  }
}

const download = async () => {
  if (!fileInfo.value) return
  const url = await getFileDownloadUrl(fileInfo.value)
  window.open(url, '_blank')
}

watch([isShow, visible], ([shown, rendered]) => {
  if (!shown || !rendered) stopResize()
})

onMounted(() => {
  eventBus.on(EVENT_SHOW_TOOL_PANEL, () => {
    isShow.value = false
    visible.value = false
  })
})

onUnmounted(() => {
  stopResize()
  eventBus.off(EVENT_SHOW_TOOL_PANEL)
})

defineExpose({
  showFilePanel,
  hideFilePanel,
  isShow
})
</script>
