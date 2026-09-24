import type { ToolContent } from '../types/message';
import type { ProgramAttemptView } from '../types/programAttempt';

function confirmedAttempt(tool: ToolContent): ProgramAttemptView | undefined {
  const attempt = tool.program_attempt;
  if (tool.name !== 'shell' || tool.function !== 'program_run' || tool.status !== 'called'
    || !attempt || attempt.version !== 1 || typeof attempt.identity !== 'string'
    || !/^[0-9a-f]{64}$/.test(attempt.identity) || !Number.isSafeInteger(attempt.returncode)
    || !['succeeded', 'failed'].includes(attempt.state)
    || (attempt.returncode === 0) !== (attempt.state === 'succeeded')
    || attempt.state !== tool.execution_status) return undefined;
  return attempt;
}

/** Display-only matching for the merged operations of ONE step, in launch order.
 * Keep this scoped to the step: another step/turn/session may reuse a script for
 * a different purpose. Absence of trusted proof always leaves failures visible.
 * A later successful program does not certify the whole analysis or its files.
 */
export function recoveredProgramAttempts(stepTools: ToolContent[]): Map<string, string> {
  const pending = new Map<string, ToolContent[]>();
  const recovered = new Map<string, string>();
  for (const tool of stepTools) {
    const attempt = confirmedAttempt(tool);
    if (!attempt) continue;
    if (attempt.state === 'failed') {
      const failures = pending.get(attempt.identity) || [];
      failures.push(tool);
      pending.set(attempt.identity, failures);
    } else {
      for (const failure of pending.get(attempt.identity) || []) {
        if (failure.tool_call_id !== tool.tool_call_id
          && Number.isFinite(failure.timestamp) && Number.isFinite(tool.timestamp)
          && failure.timestamp <= tool.timestamp) {
          recovered.set(failure.tool_call_id, tool.tool_call_id);
        }
      }
      pending.delete(attempt.identity);
    }
  }
  return recovered;
}
