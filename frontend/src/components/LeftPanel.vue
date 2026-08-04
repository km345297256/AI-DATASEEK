<template>
  <div
    class="h-full flex flex-col shrink-0 overflow-hidden transition-[width] duration-300 max-sm:fixed max-sm:inset-y-0 max-sm:left-0 max-sm:z-50"
    :class="isLeftPanelShow
      ? 'w-[300px] max-sm:w-[min(88vw,320px)]'
      : 'w-[52px] max-sm:w-0 max-sm:pointer-events-none'">
    <div
      v-if="isLeftPanelShow"
      class="flex w-full flex-col overflow-visible bg-[var(--background-nav)] h-full opacity-100 translate-x-0 shadow-[0px_8px_32px_0px_rgba(0,0,0,0.16)] sm:shadow-none">

      <!-- 顶部折叠按钮 -->
      <div class="mobile-safe-top flex min-h-[52px] items-center px-3 flex-shrink-0">
        <div class="flex justify-between w-full px-1 pt-2">
          <div class="relative flex">
            <div
              class="flex h-11 w-11 items-center justify-center cursor-pointer hover:bg-[var(--fill-tsp-gray-main)] rounded-lg sm:h-7 sm:w-7 sm:rounded-md"
              @click="toggleLeftPanel">
              <PanelLeft class="h-5 w-5 text-[var(--icon-secondary)]" />
            </div>
          </div>
        </div>
      </div>

      <!-- 快捷入口区域 -->
      <div class="flex flex-col flex-1 min-h-0 px-[8px] pb-0 gap-px">

        <!-- 新建任务 -->
        <div
          @click="handleNewTaskClick"
          class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-11 sm:h-[36px] ps-[9px] pe-[2px]"
          :class="route.path === '/' ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">
          <div class="shrink-0 size-[18px] flex items-center justify-center">
            <SquarePen :size="18" class="text-[var(--text-primary)]" />
          </div>
          <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
            <span class="truncate">{{ t('New Task') }}</span>
          </div>
          <div class="shrink-0 flex items-center gap-1 pe-[6px]">
            <span class="flex text-[var(--text-tertiary)] justify-center items-center h-5 px-1 rounded-[4px] bg-[var(--fill-tsp-white-light)] border border-[var(--border-light)]">
              <Command :size="12" />
            </span>
            <span class="flex justify-center items-center w-5 h-5 px-1 rounded-[4px] bg-[var(--fill-tsp-white-light)] border border-[var(--border-light)] text-xs text-[var(--text-tertiary)]">
              K
            </span>
          </div>
        </div>

        <!-- 插件管理 -->
        <div
          @click="handlePluginsClick"
          class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-11 sm:h-[36px] ps-[9px] pe-[2px]"
          :class="route.path === '/chat/plugins' ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">
          <div class="shrink-0 size-[18px] flex items-center justify-center">
            <Puzzle :size="18" class="text-[var(--text-primary)]" />
          </div>
          <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
            <span class="truncate">{{ t('Plugins') }}</span>
          </div>
        </div>

        <!-- 科学数据探查 -->
        <div
          @click="handleDatasetChatClick"
          class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-11 sm:h-[36px] ps-[9px] pe-[2px]"
          :class="route.path.startsWith('/dataset/') ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">
          <div class="shrink-0 size-[18px] flex items-center justify-center">
            <MessageSquareText :size="18" class="text-[var(--text-primary)]" />
          </div>
          <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
            <span class="truncate">{{ t('Scientific Data Exploration') }}</span>
          </div>
        </div>

        <div
          v-if="isAdmin"
          @click="handleAdminClick"
          class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-11 sm:h-[36px] ps-[9px] pe-[2px]"
          :class="route.path === '/chat/admin' ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">
          <div class="shrink-0 size-[18px] flex items-center justify-center">
            <ShieldCheck :size="18" class="text-[var(--text-primary)]" />
          </div>
          <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
            <span class="truncate">{{ t('System Admin') }}</span>
          </div>
        </div>

        <!-- 独立数据集管理入口 -->
        <div
          v-if="isAdmin"
          @click="handleDatasetAdminClick"
          class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-11 sm:h-[36px] ps-[9px] pe-[2px]"
          :class="route.path === '/chat/datasets' ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">
          <div class="shrink-0 size-[18px] flex items-center justify-center">
            <Database :size="18" class="text-[var(--text-primary)]" />
          </div>
          <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
            <span class="truncate">{{ t('Dataset Management') }}</span>
          </div>
        </div>

        <!-- 所有任务分组标题 + 会话列表 -->
        <div class="flex flex-col flex-1 min-h-0 -mx-[8px] mt-[4px] overflow-hidden">
          <div class="w-full border-t border-[var(--border-main)] transition-opacity duration-200" :class="isListScrolled ? 'opacity-100' : 'opacity-0'"></div>

          <!-- 滚动容器：标题 + 列表一起滚动 -->
          <div ref="scrollContainerRef" class="flex flex-col flex-1 min-h-0 overflow-y-auto overflow-x-hidden pb-5 px-[8px]" @scroll="handleListScroll">

            <!-- 分组标题 -->
            <div
              class="group flex items-center justify-between ps-[10px] pe-[2px] py-[2px] h-11 sm:h-[36px] gap-[12px] flex-shrink-0 cursor-pointer hover:bg-[var(--fill-tsp-white-light)] transition-colors rounded-[10px]"
              @click="isAllTasksCollapsed = !isAllTasksCollapsed">
              <div class="flex items-center flex-1 min-w-0 gap-0.5">
                <span class="text-[13px] leading-[18px] text-[var(--text-tertiary)] font-medium min-w-0 truncate tracking-[-0.091px]">
                  {{ t('All Tasks') }}
                </span>
                <ChevronUp
                  :size="14"
                  class="shrink-0 transition-all opacity-0 group-hover:opacity-100"
                  :class="isAllTasksCollapsed ? 'rotate-180' : 'rotate-90'"
                  stroke="var(--icon-tertiary)" />
              </div>
            </div>

            <!-- 会话列表 -->
            <template v-if="!isAllTasksCollapsed">
              <div v-if="sessions.length > 0" class="flex flex-col gap-px">
                <SessionItem
                  v-for="session in sessions"
                  :key="session.session_id"
                  :session="session"
                  @deleted="handleSessionDeleted"
                  @renamed="handleSessionRenamed" />
              </div>
              <div v-else class="flex flex-col items-center justify-center gap-4 py-8">
                <div class="flex flex-col items-center gap-2 text-[var(--text-tertiary)]">
                  <MessageSquareDashed :size="38" />
                  <span class="text-sm font-medium">{{ t('Create a task to get started') }}</span>
                </div>
              </div>
            </template>

          </div>
        </div>

        <!-- 底部用户入口 -->
        <div class="flex-shrink-0 border-t border-[var(--border-main)] px-[8px] py-[8px]">
          <div class="relative" ref="userMenuAnchorRef">
            <div
              class="flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-[40px] ps-[9px] pe-[9px] hover:bg-[var(--fill-tsp-white-light)]"
              @click="toggleUserMenu">
              <div
                class="flex-shrink-0 flex items-center justify-center rounded-full font-bold"
                style="width: 26px; height: 26px; font-size: 13px; color: rgba(255, 255, 255, 0.9); background-color: rgb(59, 130, 246);">
                {{ avatarLetter }}
              </div>
              <span class="flex-1 min-w-0 text-[14px] text-[var(--text-primary)] truncate">{{ currentUser?.fullname }}</span>
            </div>
            <!-- UserMenu 弹出（向上展开） -->
            <div
              v-if="showUserMenu"
              class="absolute bottom-full left-0 mb-1 z-50"
              @mouseleave="showUserMenu = false">
              <UserMenu />
            </div>
          </div>
        </div>

      </div>
    </div>

    <div
      v-else
      class="hidden h-full flex-col gap-2 overflow-visible rounded-r-xl border border-[var(--border-main)] bg-[var(--background-nav)] px-1 py-2 shadow-[0px_8px_32px_0px_rgba(0,0,0,0.16),0px_0px_0px_1px_rgba(0,0,0,0.06)] sm:flex"
      style="width: 52px;">
      <div class="flex items-center justify-center">
        <div
          class="flex h-10 w-10 items-center justify-center cursor-pointer rounded-md hover:bg-[var(--fill-tsp-gray-main)]"
          @click="toggleLeftPanel">
          <PanelLeft class="h-5 w-5 rotate-180 text-[var(--icon-secondary)]" />
        </div>
      </div>
      <div class="flex flex-col gap-2">
        <button class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]" :class="route.path === '/' ? 'bg-[var(--fill-tsp-white-main)]' : ''" @click="handleNewTaskClick" :title="t('New Task')">
          <SquarePen :size="18" class="text-[var(--text-primary)]" />
        </button>
        <button class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]" :class="route.path === '/chat/plugins' ? 'bg-[var(--fill-tsp-white-main)]' : ''" @click="handlePluginsClick" title="Plugins">
          <Puzzle :size="18" class="text-[var(--text-primary)]" />
        </button>
        <button class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]" :class="route.path.startsWith('/dataset/') ? 'bg-[var(--fill-tsp-white-main)]' : ''" @click="handleDatasetChatClick" :title="t('Scientific Data Exploration')">
          <MessageSquareText :size="18" class="text-[var(--text-primary)]" />
        </button>
        <button v-if="isAdmin" class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]" :class="route.path === '/chat/admin' ? 'bg-[var(--fill-tsp-white-main)]' : ''" @click="handleAdminClick" title="System Admin">
          <ShieldCheck :size="18" class="text-[var(--text-primary)]" />
        </button>
        <button v-if="isAdmin" class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]" :class="route.path === '/chat/datasets' ? 'bg-[var(--fill-tsp-white-main)]' : ''" @click="handleDatasetAdminClick" :title="t('Dataset Management')">
          <Database :size="18" class="text-[var(--text-primary)]" />
        </button>
      </div>
      <div class="mt-auto flex items-center justify-center pb-1">
        <div class="relative">
          <button
            class="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-white-light)]"
            :title="currentUser?.fullname || t('Unknown User')"
            @click="toggleUserMenu">
            <span
              class="flex items-center justify-center rounded-full font-bold"
              style="width: 26px; height: 26px; font-size: 13px; color: rgba(255, 255, 255, 0.9); background-color: rgb(59, 130, 246);">
              {{ avatarLetter }}
            </span>
          </button>
          <div
            v-if="showUserMenu"
            class="fixed inset-x-3 bottom-[max(12px,env(safe-area-inset-bottom))] z-50 sm:absolute sm:inset-x-auto sm:bottom-0 sm:left-full sm:ml-2"
            @mouseleave="showUserMenu = false">
            <UserMenu />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { PanelLeft, SquarePen, Command, MessageSquareDashed, ChevronUp, Puzzle, ShieldCheck, Database, MessageSquareText } from 'lucide-vue-next';
import SessionItem from './SessionItem.vue';
import UserMenu from './UserMenu.vue';
import { useLeftPanel } from '../composables/useLeftPanel';
import { useAuth } from '../composables/useAuth';
import { ref, computed, onMounted, watch, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getSessions } from '../api/agent';
import { ListSessionItem } from '../types/response';
import { useI18n } from 'vue-i18n';
import { eventBus } from '../utils/eventBus';
import { EVENT_REFRESH_SESSION_LIST } from '../constants/event';

const { t } = useI18n()
const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel()
const { currentUser } = useAuth()
const route = useRoute()
const router = useRouter()

const sessions = ref<ListSessionItem[]>([])
let updateSessionsPromise: Promise<void> | null = null
const isAllTasksCollapsed = ref(false)
const isListScrolled = ref(false)
const scrollContainerRef = ref<HTMLElement | null>(null)
const showUserMenu = ref(false)

const avatarLetter = computed(() => currentUser.value?.fullname?.charAt(0)?.toUpperCase() || 'A')
const isAdmin = computed(() => currentUser.value?.role === 'admin')

const toggleUserMenu = () => {
  showUserMenu.value = !showUserMenu.value
}

const handleListScroll = () => {
  if (scrollContainerRef.value) {
    isListScrolled.value = scrollContainerRef.value.scrollTop > 0
  }
}

// Function to fetch sessions data
const updateSessions = async () => {
  if (updateSessionsPromise) {
    return updateSessionsPromise
  }
  updateSessionsPromise = (async () => {
    try {
      const response = await getSessions()
      sessions.value = response.sessions
    } catch (error) {
      console.error('Failed to fetch sessions:', error)
    } finally {
      updateSessionsPromise = null
    }
  })()
  return updateSessionsPromise
}

const refreshSessions = () => {
  updateSessions().catch((error) => {
    console.error('Failed to fetch sessions:', error)
  })
}

/*
 * The task list used to be backed by a long-lived SSE connection. With many
 * users online, every open left panel kept polling /sessions server-side,
 * linearly increasing MongoDB reads and making the network request look
 * perpetually slow. The list is now refreshed by explicit UI events instead.
 */
const fetchSessions = async () => {
  try {
    await updateSessions()
  } catch (error) {
    console.error('Failed to fetch sessions:', error)
  }
}

const handleNewTaskClick = () => {
  router.push('/')
}

const handlePluginsClick = () => {
  router.push('/chat/plugins')
}

const handleDatasetChatClick = () => {
  router.push('/dataset/setup')
}

const handleAdminClick = () => {
  router.push('/chat/admin')
}

const handleDatasetAdminClick = () => {
  router.push('/chat/datasets')
}

const handleSessionDeleted = (sessionId: string) => {
  console.log('handleSessionDeleted', sessionId)
  sessions.value = sessions.value.filter(session => session.session_id !== sessionId);
}

const handleSessionRenamed = ({ sessionId, title }: { sessionId: string; title: string }) => {
  const session = sessions.value.find(item => item.session_id === sessionId)
  if (session) session.title = title
}

// Handle keyboard shortcuts
const handleKeydown = (event: KeyboardEvent) => {
  // Check for Command + K (Mac) or Ctrl + K (Windows/Linux)
  if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
    event.preventDefault()
    handleNewTaskClick()
  }
}

onMounted(() => {
  // Initial fetch of sessions
  fetchSessions()
  eventBus.on(EVENT_REFRESH_SESSION_LIST, refreshSessions)

  // Add keyboard event listener
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  eventBus.off(EVENT_REFRESH_SESSION_LIST, refreshSessions)

  // Remove keyboard event listener
  window.removeEventListener('keydown', handleKeydown)
})

watch(() => route.path, async () => {
  await updateSessions()
})
</script>
