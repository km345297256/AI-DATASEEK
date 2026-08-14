const HIDDEN_DATASET_RESULT_NOTICES = new Set([
  '文件组织直接来自数据中心登记清单，无需调用模型判断。',
  '方法与限制：仅展示登记清单中的相对路径，不读取文件内容，也不暴露宿主机真实路径。',
  'The file organization comes directly from the data-center inventory; no model decision was required.',
  'Method and limits: only registered relative paths are shown; file contents are not read and real host paths remain private.',
]);

export function stripHiddenDatasetResultNotices(text: string): string {
  const lines = text.split(/\r?\n/);
  let removed = false;
  const visibleLines = lines.filter((line) => {
    const hidden = HIDDEN_DATASET_RESULT_NOTICES.has(line.trim());
    removed ||= hidden;
    return !hidden;
  });

  if (!removed) return text;
  return visibleLines.join('\n').replace(/^\n+|\n+$/g, '').replace(/\n{3,}/g, '\n\n');
}

export function isPlaceholderAssistantMessage(text: string): boolean {
  return /^(?:placeholder|tbd|todo|n\/?a|待补充|占位(?:符|文本)?|暂无(?:内容|结果)?)\.?$/i.test(text.trim());
}
