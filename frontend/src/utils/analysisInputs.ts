import type { FileInfo } from '../api/file';

export const UPLOAD_ANALYSIS_PROMPT = '请先概览这些上传文件的内容、结构与可分析方向。';

/** A file-only submission is an explicit, visible overview request. */
export function uploadAnalysisPrompt(message: string, files: FileInfo[]): string {
  return message.trim() || (files.length ? UPLOAD_ANALYSIS_PROMPT : '');
}

export function toggleInputFile(selectedIds: string[], fileId: string, availableFiles: FileInfo[]): string[] {
  const allowed = new Set(availableFiles.map(file => file.file_id));
  const next = new Set(selectedIds.filter(id => allowed.has(id)));
  if (!allowed.has(fileId)) return [...next];
  if (next.has(fileId)) next.delete(fileId); else next.add(fileId);
  return [...next];
}
