// File API service
import { apiClient, ApiResponse, API_CONFIG, BASE_URL } from './client';
import { SignedUrlResponse } from '../types/response';

/**
 * File info type
 */
export interface FileInfo {
  file_id: string;
  filename: string;
  file_path?: string;
  content_type?: string;
  size?: number;
  upload_date: string;
  metadata?: Record<string, any>;
  file_url?: string;
}

export interface LargeUploadInitResponse {
  upload_id: string;
  file_id: string;
  filename: string;
  size: number;
  part_size: number;
  status: string;
  expires_at: string;
}

export interface LargeUploadPartUploadResponse {
  upload_id: string;
  part_number: number;
  etag: string;
  size: number;
}

export interface LargeUploadPart {
  part_number: number;
  etag: string;
  size?: number;
}

export async function initLargeUpload(file: File, metadata?: Record<string, any>): Promise<LargeUploadInitResponse> {
  const response = await apiClient.post<ApiResponse<LargeUploadInitResponse>>('/files/large-uploads/init', {
    filename: file.name,
    size: file.size,
    content_type: file.type || 'application/octet-stream',
    metadata: metadata || {},
  });
  return response.data.data;
}

export async function completeLargeUpload(uploadId: string, parts: LargeUploadPart[]): Promise<FileInfo> {
  const response = await apiClient.post<ApiResponse<FileInfo>>(`/files/large-uploads/${uploadId}/complete`, {
    parts,
  });
  return response.data.data;
}

export async function abortLargeUpload(uploadId: string): Promise<void> {
  await apiClient.post<ApiResponse<void>>(`/files/large-uploads/${uploadId}/abort`);
}

export async function uploadLargeFilePart(
  uploadId: string,
  partNumber: number,
  blob: Blob,
  onProgress?: (loaded: number) => void,
): Promise<LargeUploadPartUploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `${BASE_URL}/files/large-uploads/${encodeURIComponent(uploadId)}/parts/${partNumber}`, true);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const payload = JSON.parse(xhr.responseText || '{}') as ApiResponse<LargeUploadPartUploadResponse>;
        if (!payload.data?.etag) {
          reject(new Error('Missing ETag from large upload part response'));
          return;
        }
        resolve(payload.data);
        return;
      }
      reject(new Error(`Large upload part failed: ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('Large upload part network error'));
    xhr.send(blob);
  });
}



/**
 * Upload file
 * @param file File to upload
 * @param metadata Optional metadata
 * @returns Upload result
 */
export async function uploadFile(file: File, metadata?: Record<string, any>): Promise<FileInfo> {
  const formData = new FormData();
  formData.append('file', file);
  
  if (metadata) {
    formData.append('metadata', JSON.stringify(metadata));
  }

  const response = await apiClient.post<ApiResponse<FileInfo>>('/files', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data.data;
}

/**
 * Download file
 * @param fileId File ID
 * @returns File download result
 */
export async function downloadFile(fileId: string): Promise<Blob> {
  const response = await apiClient.get(`/files/${fileId}/download`, {
    responseType: 'blob',
  });
  
  return response.data;
}

/**
 * Delete file
 * @param fileId File ID
 * @returns Success status
 */
export async function deleteFile(fileId: string): Promise<boolean> {
  try {
    await apiClient.delete<ApiResponse<void>>(`/files/${fileId}`);
    return true;
  } catch (error) {
    console.error('Failed to delete file:', error);
    return false;
  }
}

/**
 * Get file information
 * @param fileId File ID
 * @returns File information or null if not found
 */
export async function getFileInfo(fileId: string): Promise<FileInfo | null> {
  try {
    const response = await apiClient.get<ApiResponse<FileInfo>>(`/files/${fileId}`);
    return response.data.data;
  } catch (error) {
    console.error('Failed to get file info:', error);
    return null;
  }
}

/**
 * Create file signed URL
 * @param fileId File ID to create signed URL for
 * @param expireMinutes URL expiration time in minutes (default: 15)
 * @returns Signed URL response for file download
 */
export async function createFileSignedUrl(fileId: string, expireMinutes: number = 15): Promise<SignedUrlResponse> {
  const response = await apiClient.post<ApiResponse<SignedUrlResponse>>(`/files/${fileId}/signed-url`, {
    expire_minutes: expireMinutes
  });
  return response.data.data;
}

/**
 * Get file download URL
 * @param file File info
 * @returns Promise resolving to file download URL string
 */
export async function getFileDownloadUrl(
  fileInfo: FileInfo,
): Promise<string> {
  if (fileInfo.file_url) {
    if (/^https?:\/\//i.test(fileInfo.file_url)) {
      return fileInfo.file_url;
    }
    return `${API_CONFIG.host}${fileInfo.file_url}`;
  }
  const signedUrlResponse = await createFileSignedUrl(fileInfo.file_id);
  if (/^https?:\/\//i.test(signedUrlResponse.signed_url)) {
    return signedUrlResponse.signed_url;
  }
  return `${API_CONFIG.host}${signedUrlResponse.signed_url}`;
}
