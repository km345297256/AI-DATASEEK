import { apiResponseMessage, validationDetailMessage } from './apiError.ts';

type ErrorRecord = Record<string, unknown>;

const DATASET_SUBMISSION_ERROR_FALLBACK = '提交失败，请检查数据集信息和本机目录后重试';

function isErrorRecord(cause: unknown): cause is ErrorRecord {
  return typeof cause === 'object' && cause !== null;
}

function detailMessage(value: unknown): string | undefined {
  if (Array.isArray(value)) return validationDetailMessage(value);
  if (!isErrorRecord(value)) return undefined;

  const nestedData = isErrorRecord(value.data) ? value.data : undefined;
  return validationDetailMessage(value.detail)
    ?? validationDetailMessage(nestedData?.detail);
}

export function isAbsoluteDatasetDirectory(value: string): boolean {
  const normalized = value.trim();
  if (!normalized.startsWith('/')) return false;
  return !normalized.split('/').includes('..');
}

export function datasetSubmissionErrorMessage(cause: unknown): string {
  if (isErrorRecord(cause)) {
    const response = isErrorRecord(cause.response) ? cause.response : undefined;
    const responseData = response?.data;
    if (cause.code === 403 || cause.status === 403 || response?.status === 403) {
      return '仅管理员可提交本机目录进行分析';
    }

    // Only return human-readable messages. Never include validation locations,
    // rejected input values, or the absolute path supplied by the browser.
    const validationMessage = detailMessage(cause.details) ?? detailMessage(responseData);
    if (validationMessage) return validationMessage;

    const structuredMessage = apiResponseMessage(cause.details)
      ?? apiResponseMessage(responseData);
    if (structuredMessage) return structuredMessage;

    if (typeof cause.message === 'string' && cause.message.trim()) {
      return cause.message.trim();
    }
  }
  return DATASET_SUBMISSION_ERROR_FALLBACK;
}
