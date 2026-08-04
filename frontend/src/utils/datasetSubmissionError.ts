type ErrorRecord = Record<string, unknown>;

const DATASET_SUBMISSION_ERROR_FALLBACK = '提交失败，请检查数据集信息和服务器目录后重试';


function isErrorRecord(cause: unknown): cause is ErrorRecord {
  return typeof cause === 'object' && cause !== null;
}


function validationDetailMessage(detail: unknown): string | undefined {
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  if (!Array.isArray(detail)) return undefined;

  const messages = detail
    .map((item) => isErrorRecord(item) && typeof item.msg === 'string' ? item.msg.trim() : '')
    .filter(Boolean);
  return [...new Set(messages)].join('；') || undefined;
}


export function datasetSubmissionErrorMessage(cause: unknown): string {
  if (isErrorRecord(cause)) {
    const response = isErrorRecord(cause.response) ? cause.response : undefined;
    const responseData = response && isErrorRecord(response.data) ? response.data : undefined;
    if (cause.code === 403 || response?.status === 403) {
      return '仅管理员可提交服务器目录进行分析';
    }
    const responseMessage = responseData?.msg ?? responseData?.message;
    if (typeof responseMessage === 'string' && responseMessage.trim()) {
      return responseMessage.trim();
    }
    const detailMessage = validationDetailMessage(responseData?.detail);
    if (detailMessage) return detailMessage;
    if (typeof cause.message === 'string' && cause.message.trim()) {
      return cause.message.trim();
    }
  }
  return DATASET_SUBMISSION_ERROR_FALLBACK;
}
