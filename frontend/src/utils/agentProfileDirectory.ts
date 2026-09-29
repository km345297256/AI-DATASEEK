import { ref } from 'vue';
import type { AgentProfile } from '../api/agentProfile';

export const AGENT_PROFILE_STORAGE_KEY = 'selected_agent_profile_id';

/** One owner for the catalog and selection shared by every selector in this tab. */
export function createAgentProfileDirectory(
  load: () => Promise<AgentProfile[]>,
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
) {
  const selectedProfileId = ref<string | null>(storage.getItem(AGENT_PROFILE_STORAGE_KEY));
  const selectedProfile = ref<AgentProfile | null>(null);
  const profiles = ref<AgentProfile[]>([]);
  let generation = 0;
  let inFlight: { generation: number; promise: Promise<void> } | undefined;

  function setSelectedProfile(profile: AgentProfile | null): void {
    // A read started before this user choice has no authority to revoke it.
    generation += 1;
    selectedProfile.value = profile;
    selectedProfileId.value = profile?.id ?? null;
    if (profile) storage.setItem(AGENT_PROFILE_STORAGE_KEY, profile.id);
    else storage.removeItem(AGENT_PROFILE_STORAGE_KEY);
  }

  function refreshProfiles({ force = false }: { force?: boolean } = {}): Promise<void> {
    if (!force && inFlight?.generation === generation) return inFlight.promise;
    const revision = ++generation;
    const promise = Promise.resolve().then(load).then(result => {
      if (revision !== generation) return;
      profiles.value = result;
      const id = selectedProfileId.value;
      selectedProfile.value = id ? result.find(profile => profile.id === id) ?? null : null;
      // Only a successful read owned by the current selection can prove deletion.
      if (id && !selectedProfile.value) {
        selectedProfileId.value = null;
        storage.removeItem(AGENT_PROFILE_STORAGE_KEY);
      }
    }).finally(() => {
      // A superseded read must not release a newer single-flight owner.
      if (inFlight?.generation === revision) inFlight = undefined;
    });
    inFlight = { generation: revision, promise };
    return promise;
  }

  return { selectedProfileId, selectedProfile, profiles, refreshProfiles, setSelectedProfile };
}
