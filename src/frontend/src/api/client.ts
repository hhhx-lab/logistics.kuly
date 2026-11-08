import type { ApiErrorBody } from "./types";
import { englishError } from "../i18n/runtime";

export const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  current: Record<string, unknown> | null;
  traceId: string | null;

  constructor(status: number, body: Partial<ApiErrorBody["error"]> | undefined) {
    const message = body?.message;
    const translated = message ? englishError(message) : undefined;
    super(translated && !translated.startsWith("The operation could not") ? translated : defaultMessage(status));
    this.status = status;
    this.code = body?.code || (status === 0 ? "NETWORK_ERROR" : `HTTP_${status}`);
    this.details = body?.details ?? {};
    this.current = body?.current ?? null;
    this.traceId = body?.trace_id ?? null;
  }
}

function defaultMessage(status: number) {
  if (status === 0) return "Network connection failed. Check your connection and retry.";
  if (status === 401) return "Your session has expired. Please sign in again.";
  if (status === 403 || status === 404) return "Access denied or the requested item does not exist.";
  if (status === 409) return "The data has changed. Reload the latest version and retry.";
  if (status === 413 || status === 415) return "The file does not meet the upload requirements.";
  if (status === 422) return "Invalid request parameters. Check your input and retry.";
  if (status === 429) return "Too many requests. Please retry later.";
  if (status === 503) return "An external service is temporarily unavailable.";
  return "The service could not complete the request. Please retry later.";
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;
const inflight = new Set<AbortController>();

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function onUnauthorized(handler: () => void) {
  unauthorizedHandler = handler;
}

/** 身份切换时取消全部未完成请求，防止慢响应写回新身份界面。 */
export function abortAllRequests() {
  inflight.forEach((c) => c.abort());
  inflight.clear();
}

export function newIdempotencyKey() {
  return crypto.randomUUID();
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null | string[]>;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export function buildQuery(query?: RequestOptions["query"]) {
  if (!query) return "";
  const params = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    if (Array.isArray(v)) v.forEach((x) => params.append(k, x));
    else params.set(k, String(v));
  });
  const s = params.toString();
  return s ? `?${s}` : "";
}

type Transport = (method: string, url: string, headers: Record<string, string>, body: string | undefined, signal: AbortSignal) => Promise<{ status: number; json: unknown }>;

const fetchTransport: Transport = async (method, url, headers, body, signal) => {
  const res = await fetch(url, { method, headers, body, credentials: "include", cache: method === "GET" ? "no-store" : undefined, signal });
  const text = res.status === 204 ? "" : await res.text();
  let json: unknown = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  return { status: res.status, json };
};

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (method !== "GET") {
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  }
  const controller = new AbortController();
  inflight.add(controller);
  if (opts.signal) opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
  try {
    const { status, json } = await fetchTransport(method, `${API_PREFIX}${path}${buildQuery(opts.query)}`, headers, body, controller.signal);
    if (status >= 200 && status < 300) return json as T;
    if (status === 401 && !path.startsWith("/auth/")) unauthorizedHandler?.();
    throw new ApiError(status, (json as ApiErrorBody | null)?.error);
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if ((e as Error)?.name === "AbortError") throw e;
    throw new ApiError(0, undefined);
  } finally {
    inflight.delete(controller);
  }
}

export function isApiError(e: unknown, code?: string): e is ApiError {
  return e instanceof ApiError && (code === undefined || e.code === code);
}

export function errorText(e: unknown) {
  if (e instanceof ApiError) return e.message;
  return "The operation could not be completed. Please retry later.";
}
