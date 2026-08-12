<template>
  <div class="relative min-w-0" ref="containerRef">
    <button
      ref="buttonRef"
      @click.stop="toggleOpen"
      class="flex max-w-full items-center gap-1.5 h-9 sm:h-7 px-2 rounded-lg hover:bg-[var(--fill-tsp-white-light)] transition-colors">
      <BotMessageSquare class="size-3.5 text-[var(--icon-secondary)] flex-shrink-0" />
      <span class="min-w-0 text-sm font-medium text-[var(--text-primary)] max-w-[160px] truncate">
        {{ selectedProfile?.name ?? defaultAgentName }}
      </span>
      <ChevronDown
        class="size-3.5 text-[var(--icon-tertiary)] flex-shrink-0 transition-transform duration-150"
        :class="isOpen ? 'rotate-180' : ''" />
    </button>

    <div v-if="isOpen" class="fixed inset-0 z-40" @click="isOpen = false" />
    <div
      v-if="isOpen"
      class="fixed z-50 min-w-[220px] rounded-xl bg-[var(--background-menu-white)] shadow-[0px_8px_32px_0px_var(--shadow-S),0px_0px_0px_1px_var(--border-light)] py-1 overflow-hidden"
      :style="{ left: `${menuPosition.left}px`, top: `${menuPosition.top}px` }">

      <div class="px-2 py-1">
        <div class="text-[11px] font-medium text-[var(--text-tertiary)] px-1 pb-1">{{ t('DataSeek Profile') }}</div>
        <button
          @click="selectProfile(null)"
          class="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-[var(--fill-tsp-white-main)] text-sm transition-colors">
          <div class="size-5 rounded-md bg-[var(--fill-tsp-white-dark)] flex items-center justify-center flex-shrink-0">
            <BotMessageSquare class="size-3 text-[var(--icon-secondary)]" />
          </div>
          <div class="flex-1 min-w-0 text-left">
            <div class="text-[var(--text-primary)] truncate">{{ defaultAgentName }}</div>
            <div class="text-[10px] text-[var(--text-tertiary)] truncate">{{ defaultModelLabel }}</div>
          </div>
          <Check v-if="!selectedProfile" class="size-3.5 text-[var(--icon-primary)]" />
        </button>

        <button
          v-for="profile in profiles"
          :key="profile.id"
          @click="selectProfile(profile)"
          class="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-[var(--fill-tsp-white-main)] text-sm transition-colors">
          <div
            class="size-5 rounded-md bg-[var(--fill-tsp-white-dark)] flex items-center justify-center flex-shrink-0 text-[10px] font-bold text-[var(--icon-secondary)]">
            {{ profile.name.charAt(0).toUpperCase() }}
          </div>
          <div class="flex-1 min-w-0 text-left">
            <div class="text-[var(--text-primary)] truncate">{{ profile.name }}</div>
            <div class="text-[10px] text-[var(--text-tertiary)] truncate">{{ profile.model_name }}</div>
          </div>
          <Check v-if="selectedProfile?.id === profile.id" class="size-3.5 text-[var(--icon-primary)] flex-shrink-0" />
        </button>
      </div>

    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { BotMessageSquare, ChevronDown, Check } from 'lucide-vue-next'
import { type AgentProfile } from '@/api/agentProfile'
import { useAgentProfile } from '@/composables/useAgentProfile'
import { getCachedClientConfig, type ClientConfigResponse } from '@/api/config'

const { t } = useI18n()
const { selectedProfile, setSelectedProfile, profiles, refreshProfiles } = useAgentProfile()

const isOpen = ref(false)
const buttonRef = ref<HTMLElement | null>(null)
const menuPosition = ref({ left: 0, top: 0 })
const clientConfig = ref<ClientConfigResponse | null>(null)

const defaultAgentName = computed(() => clientConfig.value?.default_agent_name || 'DataSeek')
const defaultModelLabel = computed(() => {
  const provider = clientConfig.value?.default_model_provider
  const model = clientConfig.value?.default_model_name
  return [provider, model].filter(Boolean).join(' / ') || t('Default')
})

function updateMenuPosition() {
  const rect = buttonRef.value?.getBoundingClientRect()
  if (!rect) return
  menuPosition.value = {
    left: rect.left,
    top: rect.bottom + 4,
  }
}

async function toggleOpen() {
  isOpen.value = !isOpen.value
  if (isOpen.value) {
    await nextTick()
    updateMenuPosition()
  }
}

watch(isOpen, (open) => {
  if (open) {
    updateMenuPosition()
    refreshProfiles()
  }
})

function selectProfile(profile: AgentProfile | null) {
  setSelectedProfile(profile)
  isOpen.value = false
}

onMounted(async () => {
  clientConfig.value = await getCachedClientConfig()
  await refreshProfiles()
})
</script>
