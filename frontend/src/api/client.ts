// Backend API client configuration
import axios from 'axios';
import type { AxiosError } from 'axios';
import { ApiError, apiResponseMessage, normalizeApiError } from '../utils/apiError.ts';
import { startSSEConnection } from '../utils/sseConnection.ts';
import type { SSECallbacks, SSEOptions } from '../utils/sseConnection.ts';

export { ApiError } from '../utils/apiError.ts';
export type { SSECallbacks, SSEOptions } from '../utils/sseConnection.ts';

// API configuration
export const API_CONFIG = {
  host: import.meta.env?.VITE_API_URL || '',
  version: 'v1',
  timeout: 30000, // Request timeout in milliseconds
};

// Complete API base URL
export const BASE_URL = API_CONFIG.host 
  ? `${API_CONFIG.host}/api/${API_CONFIG.version}` 
  : `/api/${API_CONFIG.version}`;

// Unified response format
export interface ApiResponse<T> {
  code: number;
  msg: string;
  data: T;
}

// Create axios instance
export const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: API_CONFIG.timeout,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.request.use((config) => {
  config.headers.delete('Authorization');
  config.headers.delete('X-API-Key');
  return config;
});

// Response interceptor, unified error handling.
apiClient.interceptors.response.use(
  (response) => {
    // Check backend response format
    if (response.data && typeof response.data.code === 'number') {
      // If it's a business logic error (code not 0), convert to error handling
      if (response.data.code !== 0) {
        const apiError = new ApiError(apiResponseMessage(response.data) ?? 'Unknown error', {
          status: response.status,
          code: response.data.code,
          details: response.data,
        });
        return Promise.reject(apiError);
      }
    }
    return response;
  },
  (error: AxiosError) => {
    const apiError = normalizeApiError(error);

    // Request/response objects may carry credential-form input. Log only the
    // status, never Axios config, API details, or provider error bodies.
    console.error('API request failed', apiError.status ?? 'network');
    return Promise.reject(apiError);
  }
); 

/**
 * Generic SSE connection function
 * @param endpoint - API endpoint (relative to BASE_URL)
 * @param options - Request options
 * @param callbacks - Event callbacks
 * @returns Function to cancel the SSE connection
 */
export const createSSEConnection = async <T = any>(
  endpoint: string,
  options: SSEOptions = {},
  callbacks: SSECallbacks<T> = {}
): Promise<() => void> => {
  const connection = startSSEConnection(`${BASE_URL}${endpoint}`, options, callbacks);
  // Caller failures must not leak provider bodies or credential-bearing errors.
  void connection.finished.catch(() => console.error('SSE callback failed'));
  return connection.cancel;
}; 
