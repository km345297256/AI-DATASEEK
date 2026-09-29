import { listAgentProfiles } from '../api/agentProfile'
import { createAgentProfileDirectory } from '../utils/agentProfileDirectory'

const directory = createAgentProfileDirectory(listAgentProfiles, localStorage)

export function useAgentProfile() {
  return directory
}
