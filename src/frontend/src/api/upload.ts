import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";

export interface UploadFileSpec {
  filename: string;
  mime: string;
  size_bytes: number;
  sha256?: string;
  replace_document_id?: string;
  expected_document_version?: number;
}

export interface PutResult {
  etag: string;
}

type Uploader = (url: string, file: Blob, onProgress: (loaded: number) => void, signal: AbortSignal) => Promise<PutResult>;

/** 浏览器直传签名地址；进度只代表本连接已发送字节，不写成服务端事件。 */
const xhrUploader: Uploader = (url, file, onProgress, signal) =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve({ etag: (xhr.getResponseHeader("ETag") || "").replace(/"/g, "") });
      else reject(new Error(`PUT_${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("PUT_NETWORK"));
    xhr.onabort = () => reject(new DOMException("aborted", "AbortError"));
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(file);
  });

export function putBytes(url: string, file: Blob, onProgress: (loaded: number) => void, signal: AbortSignal) {
  return xhrUploader(url, file, onProgress, signal);
}

export const uploadApi = {
  createCase: (name: string | null, key: string) => api<ApiResponses["POST /api/v1/cases"]["201"]>("/cases", { method: "POST", body: { name }, idempotencyKey: key }),
  getCase: (id: string) => api<ApiResponses["GET /api/v1/cases/{case_id}"]["200"]>(`/cases/${id}`),
  createSession: (caseId: string, files: UploadFileSpec[], disclosure_version_ack: string, key: string, issueSupplement?: { related_issue_id: string; expected_issue_version: number }) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/upload-sessions"]["201"]>(`/cases/${caseId}/upload-sessions`, { method: "POST", body: { files, disclosure_version_ack, ...issueSupplement }, idempotencyKey: key }),
  sessions: (params: { status?: string; cursor?: string }) => api<ApiResponses["GET /api/v1/upload-sessions"]["200"]>("/upload-sessions", { query: params }),
  session: (id: string) => api<ApiResponses["GET /api/v1/upload-sessions/{session_id}"]["200"]>(`/upload-sessions/${id}`),
  complete: (fileId: string, body: { etag: string; sha256?: string }, key: string) =>
    api<ApiResponses["POST /api/v1/upload-files/{file_id}/complete"]["200"]>(`/upload-files/${fileId}/complete`, { method: "POST", body, idempotencyKey: key }),
  retry: (fileId: string, key: string) => api<ApiResponses["POST /api/v1/upload-files/{file_id}/retry"]["200"]>(`/upload-files/${fileId}/retry`, { method: "POST", idempotencyKey: key }),
  cancel: (fileId: string, key: string) => api<ApiResponses["POST /api/v1/upload-files/{file_id}/cancel"]["200"]>(`/upload-files/${fileId}/cancel`, { method: "POST", idempotencyKey: key }),
};
