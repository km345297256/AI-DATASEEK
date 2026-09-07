import type { AgentToolRuntimeConfig } from '../api/agentProfile.ts';

export const LEGACY_PROFILE_TOOL_RUNTIME: Readonly<AgentToolRuntimeConfig> = Object.freeze({
  preset_id: 'general',
  selection_mode: 'all',
  code_mode_enabled: false,
  domain_subagents_enabled: false,
});

export function copyAgentToolRuntime(
  value?: AgentToolRuntimeConfig | null,
): AgentToolRuntimeConfig {
  const source = value ?? LEGACY_PROFILE_TOOL_RUNTIME;
  return {
    preset_id: source.preset_id,
    selection_mode: source.selection_mode,
    code_mode_enabled: source.code_mode_enabled,
    domain_subagents_enabled: source.domain_subagents_enabled,
  };
}

export function sameAgentToolRuntime(
  first?: AgentToolRuntimeConfig | null,
  second?: AgentToolRuntimeConfig | null,
): boolean {
  const left = copyAgentToolRuntime(first);
  const right = copyAgentToolRuntime(second);
  return left.preset_id === right.preset_id
    && left.selection_mode === right.selection_mode
    && left.code_mode_enabled === right.code_mode_enabled
    && left.domain_subagents_enabled === right.domain_subagents_enabled;
}
