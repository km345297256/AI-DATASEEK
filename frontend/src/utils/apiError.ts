type ErrorRecord = Record<string, unknown>;

export interface ApiErrorOptions {
  status?: number;
  code?: number;
  details?: unknown;
  cause?: unknown;
}

function isErrorRecord(value: unknown): value is ErrorRecord {
  return typeof value === 'object' && value !== null;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

function numericValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * Extracts the human-readable portion of a FastAPI validation response without
 * exposing locations, rejected input values, or the rest of the response body.
 */
export function validationDetailMessage(detail: unknown): string | undefined {
  const directMessage = nonEmptyString(detail);
  if (directMessage) return directMessage;
  if (!Array.isArray(detail)) return undefined;

  const messages = detail
    .map((item) => {
      if (typeof item === 'string') return item.trim();
      return isErrorRecord(item) ? nonEmptyString(item.msg) ?? '' : '';
    })
    .filter(Boolean);

  return [...new Set(messages)].join('；') || undefined;
}

export function apiResponseMessage(details: unknown): string | undefined {
  if (!isErrorRecord(details)) return nonEmptyString(details);

  return validationDetailMessage(details.detail)
    ?? nonEmptyString(details.msg)
    ?? nonEmptyString(details.message);
}

/**
 * The only error shape rejected by the API client. It remains compatible with
 * `Error` consumers while retaining structured response metadata.
 */
export class ApiError extends Error {
  readonly status?: number;
  readonly code: number;
  readonly details?: unknown;
  readonly cause?: unknown;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status;
    this.code = options.code ?? options.status ?? 500;
    this.details = options.details;
    this.cause = options.cause;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Normalize Axios-like failures without coupling this utility to Axios. */
export function normalizeApiError(cause: unknown): ApiError {
  if (cause instanceof ApiError) return cause;

  const error = isErrorRecord(cause) ? cause : undefined;
  const response = error && isErrorRecord(error.response) ? error.response : undefined;
  const status = numericValue(response?.status);
  const details = response?.data;
  const detailRecord = isErrorRecord(details) ? details : undefined;
  const responseCode = numericValue(detailRecord?.code);
  const hasRequest = Boolean(error?.request);
  const isNetworkError = !response && hasRequest;

  const code = responseCode ?? status ?? (isNetworkError ? 503 : 500);
  const message = apiResponseMessage(details)
    ?? nonEmptyString(response?.statusText)
    ?? (isNetworkError ? 'Network error, please check your connection' : undefined)
    ?? nonEmptyString(error?.message)
    ?? 'Request failed';

  return new ApiError(message, {
    status,
    code,
    details,
    cause,
  });
}
