/** Only explicit process facts may become an exit label; tool success is not an exit code. */
export function shellConsoleExitText(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const record = value as Record<string, unknown>;
  if (!Object.prototype.hasOwnProperty.call(record, 'returncode') || record.status === 'running') return '';
  if (typeof record.returncode === 'number' && Number.isSafeInteger(record.returncode)) {
    return `退出码：${record.returncode}`;
  }
  return record.returncode === null || record.status === 'completed' || record.status === 'failed'
    ? '未取得退出码' : '';
}
