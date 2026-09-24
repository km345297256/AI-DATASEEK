import type { AnalysisOutcome, AnalysisContinuationAttempt, ArtifactIssue, ArtifactIssueKind, ArtifactIssueReasonCode } from '../types/analysisOutcome';
import type { Message, MessageContent } from '../types/message';

const CHECKPOINT_ID = /^[a-f0-9]{32}$/;
const ISSUE_KINDS: Record<ArtifactIssueKind, string> = { image: '图表', table: '数据表', report: '报告', code: '代码', any: '结果文件' };

export const ARTIFACT_ISSUE_REASON_LABELS: Record<ArtifactIssueReasonCode, string> = {
  missing_artifact: '成果文件尚未生成。',
  invalid_content: '文件内容未通过校验。',
  unavailable_or_unsafe_path: '文件不可读取或不在允许的交付范围内。',
  unsupported_kind: '暂不支持校验此类文件。',
  unsupported_format: '暂不支持校验此文件格式。',
  kind_mismatch: '文件内容与声明的成果类型不一致。',
  format_mismatch: '文件内容与声明的格式不一致。',
  not_regular_file: '该文件不是可交付的普通文件。',
  file_size_limit: '文件大小超出校验范围。',
  batch_size_limit: '本批文件总大小超出校验范围。',
  empty_file: '文件为空。',
  changed_during_read: '文件在校验过程中发生变化，未能确认最终内容。',
  empty_or_binary_text: '文本文件为空或包含无法按文本读取的内容。',
  image_size_limit: '图像尺寸超出安全读取范围。',
  table_size_limit: '数据表规模超出安全读取范围。',
  inconsistent_table_width: '数据表各行的列数不一致。',
  empty_table: '数据表中没有可读取的数据。',
  archive_size_limit: '文件包的展开大小超出安全读取范围。',
  encrypted_workbook: '工作簿已加密，无法校验内容。',
  unsafe_workbook_xml: '工作簿内部结构未通过安全检查。',
  worksheet_count_limit: '工作表数量超出安全读取范围。',
  invalid_json_constant: 'JSON 包含不受支持的常量。',
  duplicate_json_key: 'JSON 中存在重复字段。',
  invalid_json_table: 'JSON 内容不是可识别的数据表结构。',
  invalid_notebook: 'Notebook 文件结构未通过校验。',
  validation_deadline: '文件校验超时，尚未确认内容是否有效。',
  validator_unavailable: '此类文件的校验组件暂时不可用。',
  validation_unavailable: '暂时无法校验此文件。',
  validation_receipt_invalid: '文件校验凭据无效，无法确认交付内容。',
  delivery_failed: '文件尚未成功交付。',
  invalid_csv_syntax: 'CSV 文件结构不合法。',
  invalid_text_encoding: '文本编码无法正确读取。',
  invalid_json_syntax: 'JSON 语法不合法。',
  invalid_code_syntax: '代码文件存在语法错误。',
};

/** Discard malformed diagnostics, never turn paths or provider text into UI labels. */
function readArtifactIssues(value: unknown): ArtifactIssue[] {
  if (!Array.isArray(value)) return [];
  const issues: ArtifactIssue[] = [];
  const seen = new Set<string>();
  for (const entry of value.slice(0, 64)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    if (!['artifact_name', 'kind', 'reason_code', 'blocking'].every((key) => Object.prototype.hasOwnProperty.call(entry, key))) continue;
    const { artifact_name: name, kind, reason_code: reason, blocking } = entry;
    if (typeof name !== 'string' || !name.trim() || name.length > 320 || [...name].length > 160 || ['.', '..'].includes(name.trim())
      || /[/\\\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(name)
      || typeof kind !== 'string' || !Object.prototype.hasOwnProperty.call(ISSUE_KINDS, kind)
      || typeof reason !== 'string' || !Object.prototype.hasOwnProperty.call(ARTIFACT_ISSUE_REASON_LABELS, reason)
      || typeof blocking !== 'boolean') continue;
    const issue: ArtifactIssue = { artifact_name: name, kind: kind as ArtifactIssueKind, reason_code: reason as ArtifactIssueReasonCode, blocking };
    const key = JSON.stringify(issue);
    if (!seen.has(key)) {
      issues.push(issue);
      seen.add(key);
    }
  }
  return issues;
}

export function analysisOutcomeIssues(outcome: AnalysisOutcome) {
  return readArtifactIssues(outcome.issues).map((issue) => ({
    ...issue, kind_label: ISSUE_KINDS[issue.kind], reason: ARTIFACT_ISSUE_REASON_LABELS[issue.reason_code],
  }));
}

export function analysisOutcomeIsComplete(outcome: AnalysisOutcome): boolean {
  return outcome.status === 'succeeded' && !outcome.missing.length
    && !readArtifactIssues(outcome.issues).some((issue) => issue.blocking);
}

/** Metadata is a server contract, never inferred from the model's prose. */
export function readAnalysisOutcome(value: unknown): AnalysisOutcome | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const outcome = value as Record<string, unknown>;
  if (typeof outcome.status !== 'string' || !['succeeded', 'partial', 'failed'].includes(outcome.status)
    || typeof outcome.reason_code !== 'string' || typeof outcome.can_resume !== 'boolean'
    || !Array.isArray(outcome.missing) || outcome.missing.length > 32) return undefined;
  if (outcome.missing.some((item) => !item || typeof item !== 'object'
    || typeof item.kind !== 'string' || typeof item.label !== 'string'
    || !Number.isSafeInteger(item.min_count) || item.min_count < 1 || item.min_count > 16)) return undefined;
  const resumeFrom = typeof outcome.resume_from === 'string' && CHECKPOINT_ID.test(outcome.resume_from)
    ? outcome.resume_from : undefined;
  return {
    status: outcome.status as AnalysisOutcome['status'],
    reason_code: outcome.reason_code,
    missing: outcome.missing as AnalysisOutcome['missing'],
    can_resume: outcome.can_resume && outcome.status !== 'succeeded' && Boolean(resumeFrom),
    resume_from: resumeFrom,
    issues: readArtifactIssues(outcome.issues),
  };
}

const REASON_LABELS: Record<string, string> = {
  completed: '本次分析已完成。',
  artifacts_missing: '部分要求的成果还未完成。',
  artifact_validation_failed: '部分文件尚未通过内容检查，暂不能计为已完成成果。',
  answer_validation_unavailable: '本次说明暂无法完成证据核验；核验未完成不等于结论被判错误。',
  answer_validation_rejected: '本次说明中的部分引用或证据未通过检查，相关结论暂未发布。',
  report_validation_rejected: '报告已保存，但部分正文未通过证据核验。',
  report_validation_unavailable: '报告已保存，但正文尚未完成证据核验。',
  scientific_validation_rejected: '本次结果的计算方法或数值一致性未通过核验。',
  scientific_validation_unavailable: '本次结果的计算方法与数值一致性尚未完整核验。',
  answer_objectives_missing: '结果说明遗漏了您明确要求的部分内容，本次分析尚未完成。',
  input_preparation_failed: '输入准备失败，本次分析尚未开始。',
  dataset_unreadable: '当前数据无法读取，本次分析尚未开始。请检查数据来源的可用性和读取权限。',
  dataset_unsafe: '当前数据未通过安全读取检查，本次分析尚未开始。',
  dataset_changed: '数据读取前后的状态校验不一致，本次分析尚未开始。',
  dataset_limit: '当前数据超出准备阶段的处理范围，本次分析尚未开始。',
  dataset_preparation_failed: '数据准备失败，本次分析尚未开始。',
  model_audit_unavailable: '暂时无法记录准备过程，本次分析尚未开始。',
  transport_timeout: '请求准备超时，本次分析尚未开始。',
  transport_error: '请求准备时连接失败，本次分析尚未开始。',
  invalid_response: '请求准备未得到有效结果，本次分析尚未开始。',
  invalid_safety: '请求安全检查未完成，本次分析尚未开始。',
  invalid_routing: '未能确定本次请求的处理方式，分析尚未开始。',
  invalid_decision: '请求准备未得到有效处理决定，本次分析尚未开始。',
  front_controller_unavailable: '请求准备服务暂时不可用，本次分析尚未开始。',
  runtime_admission_unavailable: '暂时无法确认执行条件，本次请求未进入分析队列。',
  analytical_requirements_missing: '部分要求的分析内容尚未完成。',
  validation_unavailable: '暂时无法核验成果内容，完成情况尚未确认。',
  delivery_failed: '部分成果文件尚未成功交付。',
  tool_budget_exhausted: '本轮工具执行额度已用尽。',
  analysis_budget_deadline_exceeded: '本次分析已达到最长执行时间，尚未完成的部分已停止。',
  analysis_budget_store_unavailable: '暂时无法可靠记录执行额度，系统已安全停止后续操作。',
  budget_no_progress_loop: '连续操作未产生新的有效进展，系统已停止重复尝试。',
  tool_arguments_invalid: '工具参数校验未通过。',
  tool_execution_failed: '工具执行失败。',
  tool_execution_unknown: '部分操作的执行状态尚未确认，未将其计为已完成。',
  finalization_timeout: '结果整理超时。',
  finalization_failed: '结果整理失败。',
  invalid_final_result: '结果格式未通过验证。',
  invalid_execution_result: '模型未返回可验证的执行结果，本次分析尚未完成。',
  tool_protocol_error: '模型未能发起有效的工具调用，相关操作未执行。',
  execution_failed: '分析尚未完成。',
  requirements_satisfied: '本次分析已完成。',
  missing_artifacts: '部分要求的成果尚未生成。',
  missing_deliverables: '部分要求的成果尚未生成。',
  incomplete_analysis: '本次分析还有未完成的部分。',
  execution_interrupted: '本次分析执行已中断。',
  request_cancelled: '本次请求已取消。',
  tool_authorization_stopped: '工具操作未获授权，本次执行已停止。',
  model_runtime_stopped: '模型运行已停止，本次分析尚未完成。',
  model_unavailable: '模型服务暂时不可用。',
  model_budget_exceeded: '本次分析已达到处理预算。',
  model_protocol_error: '模型返回的结果未通过完整性校验。',
  user_cancelled: '本次分析已停止。',
};

/** Do not render arbitrary diagnostic text, paths or provider output. */
export function analysisOutcomeReason(outcome: AnalysisOutcome, hasDeliveredFiles = false): string {
  if (analysisOutcomeIsComplete(outcome)) return '本次分析已完成。';
  if (['completed', 'requirements_satisfied'].includes(outcome.reason_code)) return '本次分析仍有待完成项。';
  const reason = (Object.prototype.hasOwnProperty.call(REASON_LABELS, outcome.reason_code) ? REASON_LABELS[outcome.reason_code] : undefined)
    ?? '本次分析尚未完成，具体原因暂未确认。';
  return hasDeliveredFiles && ['answer_validation_unavailable', 'execution_interrupted',
    'scientific_validation_rejected', 'scientific_validation_unavailable'].includes(outcome.reason_code)
    ? `${reason}本次已交付的文件仍可查看，内容需结合核验结论使用。` : reason;
}

/** Final assistant message attachments are published deliveries for this response.
 * Never infer delivery from prose, source files, or another turn's attachments.
 */
export function hasDeliveredAnalysisFiles(message: Message): boolean {
  if (message.type !== 'assistant') return false;
  const files = (message.content as MessageContent).attachments;
  return Array.isArray(files) && files.some(file => file && typeof file.file_id === 'string'
    && file.file_id.trim().length > 0 && typeof file.filename === 'string' && file.filename.trim().length > 0);
}

export function analysisOutcomeTitle(outcome: AnalysisOutcome): string {
  return analysisOutcomeIsComplete(outcome) ? '已完成'
    : outcome.status === 'failed' ? '未完成' : '部分完成';
}

// Keep unknown metadata out of display labels, including history predating formats.
const MISSING_FORMAT_LABELS = new Set([
  'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tif', 'tiff', 'svg', 'avif',
  'csv', 'tsv', 'xlsx', 'xls', 'parquet', 'md', 'markdown',
  'txt', 'json', 'html', 'htm', 'pdf', 'docx',
  'py', 'r', 'js', 'ts', 'sh', 'sql', 'ipynb',
]);

function missingFormatLabel(formats: unknown): string {
  if (!Array.isArray(formats) || formats.length < 1 || formats.length > 8) return '';
  const normalized = new Set<string>();
  for (const value of formats) {
    if (typeof value !== 'string') return '';
    const suffix = (value.startsWith('.') ? value.slice(1) : value).toLowerCase();
    if (!MISSING_FORMAT_LABELS.has(suffix)) return '';
    normalized.add(suffix);
  }
  return `（${[...normalized].map(suffix => suffix.toUpperCase()).join(' / ')}）`;
}

export function analysisOutcomeMissing(outcome: AnalysisOutcome): string[] {
  const labels: Record<string, string> = { image: '图表', table: '数据表', report: '报告', code: '代码', any: '结果文件' };
  return outcome.missing.map((item) => {
    const label = Object.prototype.hasOwnProperty.call(labels, item.kind) ? labels[item.kind] : '成果';
    const formats = 'formats' in item ? missingFormatLabel(item.formats) : '';
    return `${label}${formats} × ${item.min_count}`;
  });
}

export function resumableAnalysisOutcome(messages: Message[], index: number): AnalysisOutcome | undefined {
  if (messages[index]?.type !== 'assistant'
    || messages.slice(index + 1).some((message) => message.type === 'user' || message.type === 'assistant')) return undefined;
  const content = messages[index].content as MessageContent;
  if (content.metadata?.safety_review?.decision === 'reject') return undefined;
  const outcome = readAnalysisOutcome(content.metadata?.analysis_outcome);
  return outcome?.can_resume ? outcome : undefined;
}

/** Re-clicking after an ambiguous transport failure retains the same logical input. */
export function continuationAttempt(
  previous: AnalysisContinuationAttempt | null,
  sessionId: string,
  resumeFrom: string,
  createId: () => string,
): AnalysisContinuationAttempt {
  if (!sessionId || !CHECKPOINT_ID.test(resumeFrom)) throw new Error('无法识别可继续的分析，请刷新任务状态。');
  if (previous?.sessionId === sessionId && previous.resumeFrom === resumeFrom) return previous;
  return { sessionId, resumeFrom, clientMessageId: createId() };
}
