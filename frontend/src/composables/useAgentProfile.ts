import { ref } from 'vue'
import type { AgentProfile } from '../api/agentProfile'
import { listAgentProfiles } from '../api/agentProfile'

const STORAGE_KEY = 'selected_agent_profile_id'

const selectedProfileId = ref<string | null>(localStorage.getItem(STORAGE_KEY))
const selectedProfile = ref<AgentProfile | null>(null)
const profiles = ref<AgentProfile[]>([])

async function refreshProfiles(): Promise<void> {
  const result = await listAgentProfiles()
  profiles.value = result
  if (selectedProfileId.value) {
    const found = result.find(p => p.id === selectedProfileId.value)
    selectedProfile.value = found ?? null
    if (!found) {
      selectedProfileId.value = null
      localStorage.removeItem(STORAGE_KEY)
    }
  }
}

export function useAgentProfile() {
  function setSelectedProfile(profile: AgentProfile | null) {
    selectedProfile.value = profile
    selectedProfileId.value = profile?.id ?? null
    if (profile?.id) {
      localStorage.setItem(STORAGE_KEY, profile.id)
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  }

  return {
    selectedProfileId,
    selectedProfile,
    profiles,
    refreshProfiles,
    setSelectedProfile,
  }
}
