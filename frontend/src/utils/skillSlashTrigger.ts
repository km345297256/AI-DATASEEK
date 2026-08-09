export interface SkillSlashTrigger {
  query: string;
  range: {
    start: number;
    end: number;
  };
}

export interface RemovedSkillSlashTrigger {
  value: string;
  cursor: number;
}

/** Resolve a slash-prefixed Skill query immediately before the cursor. */
export function findSkillSlashTrigger(
  value: string,
  cursor: number | null,
  isComposing = false,
): SkillSlashTrigger | null {
  if (isComposing || cursor === null || cursor <= 0) return null;

  const beforeCursor = value.slice(0, cursor);
  const match = beforeCursor.match(/(?:^|\s)\/([^\s/]*)$/);
  if (!match) return null;

  const query = match[1] ?? '';
  return {
    query,
    range: {
      start: cursor - query.length - 1,
      end: cursor,
    },
  };
}

/** Remove the active slash query after selecting a Skill. */
export function removeSkillSlashTrigger(
  value: string,
  range: SkillSlashTrigger['range'],
): RemovedSkillSlashTrigger {
  return {
    value: `${value.slice(0, range.start)}${value.slice(range.end)}`,
    cursor: range.start,
  };
}
