export type ArtifactIssueKind = 'image' | 'table' | 'report' | 'code' | 'any';

export type ArtifactIssueReasonCode =
  | 'invalid_content' | 'unavailable_or_unsafe_path' | 'unsupported_kind' | 'unsupported_format'
  | 'kind_mismatch' | 'format_mismatch' | 'not_regular_file' | 'file_size_limit' | 'batch_size_limit'
  | 'empty_file' | 'changed_during_read' | 'empty_or_binary_text' | 'image_size_limit' | 'table_size_limit'
  | 'inconsistent_table_width' | 'empty_table' | 'archive_size_limit' | 'encrypted_workbook'
  | 'unsafe_workbook_xml' | 'worksheet_count_limit' | 'invalid_json_constant' | 'duplicate_json_key'
  | 'invalid_json_table' | 'invalid_notebook' | 'validation_deadline' | 'validator_unavailable'
  | 'validation_unavailable' | 'validation_receipt_invalid' | 'delivery_failed' | 'invalid_csv_syntax'
  | 'invalid_text_encoding' | 'invalid_json_syntax' | 'invalid_code_syntax';

export interface ArtifactIssue {
  artifact_name: string;
  kind: ArtifactIssueKind;
  reason_code: ArtifactIssueReasonCode;
  blocking: boolean;
}

export interface AnalysisOutcome {
  status: 'succeeded' | 'partial' | 'failed';
  reason_code: string;
  missing: Array<{ kind: string; min_count: number; label: string }>;
  can_resume: boolean;
  resume_from?: string;
  /** Absent on older history; these are file diagnostics, not replacement answers. */
  issues?: ArtifactIssue[];
}

export interface AnalysisContinuationAttempt {
  sessionId: string;
  resumeFrom: string;
  clientMessageId: string;
}
