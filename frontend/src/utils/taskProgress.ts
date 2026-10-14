import type { DataRun } from '@/types/dataRun';

export const runStatusLabels: Record<DataRun['status'], string> = {
  running: '运行中（最后记录）', waiting: '等待输入', succeeded: '任务已完成',
  partial: '部分完成', failed: '任务失败', unknown: '结果待确认',
};
export function taskRunLabel(run: DataRun | undefined, active: boolean, error: string): string {
  if (active) return '任务进行中';
  if (error) return '任务状态暂不可用';
  if (!run) return '任务记录';
  if (!run.history_complete) return '任务记录不完整';
  // A rejected follow-up may create no run. Label this as the latest execution,
  // rather than claiming the latest chat message succeeded.
  return `最近执行：${runStatusLabels[run.status]}`;
}
