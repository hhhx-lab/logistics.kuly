import type { ApiResponses } from "./generated/request-contracts";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError, API_PREFIX } from "./client";

// 三条持久事件流：UploadSession（U04）、Run（W04）、ConversationTurn（C05）。
// 各流 seq 独立；先读快照 last_seq，再按 after_seq 订阅，缺号或 EVENT_GAP 时重取快照。

export const STREAM_EVENT_TYPES = {
  upload: ["file_status", "session_status", "EVENT_GAP"],
  run: ["run_status", "stage", "progress", "input_request", "export_step", "EVENT_GAP"],
  turn: [
    "answer_preview_started",
    "answer_preview_delta",
    "answer_preview_reset",
    "message_delta",
    "tool_started",
    "tool_finished",
    "evidence_card",
    "message_final",
    "message_error",
    "continuation_pending",
    "continuation_status",
    "continuation_final",
    "EVENT_GAP",
  ],
} as const;

export type StreamKind = keyof typeof STREAM_EVENT_TYPES;

export interface StreamEvent<T = Record<string, unknown>> {
  type: string;
  seq: number;
  data: T & { seq: number; occurred_at: string; status?: string; stage?: string; message?: string };
}

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamOptions {
  kind: StreamKind;
  path: string;
  afterSeq: number;
  onEvent: (e: StreamEvent) => void;
  onConnection?: (s: ConnectionState) => void;
  // 回合可在正式答复后继续异步续答，须按最新回合与续答状态一起判断。
  isTerminal?: () => Promise<boolean>;
}

export interface StreamHandle {
  close(): void;
}

type StreamFactory = (opts: StreamOptions) => StreamHandle;

const eventSourceFactory: StreamFactory = ({ kind, path, afterSeq, onEvent, onConnection, isTerminal }) => {
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let request: AbortController | null = null;
  let closed = false;
  let lastDelivered = afterSeq;
  const eventsPath = path.replace(/\/stream$/, "");
  const snapshotPath = eventsPath.replace(/\/events$/, "");
  const terminal = (status: unknown) => kind === "run"
    ? ["exported", "indexed", "failed_final", "cancelled"].includes(String(status))
    : kind === "upload" && ["completed", "partial", "expired"].includes(String(status));
  const stopPoll = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    request?.abort();
    request = null;
  };
  const close = () => {
    closed = true;
    es?.close();
    es = null;
    stopPoll();
    onConnection?.("closed");
  };
  const gap = (recoverable = true) => {
    close();
    onEvent({ type: "EVENT_GAP", seq: -1,
      data: { seq: -1, occurred_at: new Date().toISOString(), recoverable } });
  };
  const checkTurnTerminal = async () => {
    if (closed || kind !== "turn" || !isTerminal) return false;
    if (await isTerminal()) { close(); return true; }
    return closed;
  };
  const deliver = (event: StreamEvent) => {
    if (closed || event.seq <= lastDelivered && event.type !== "EVENT_GAP") return;
    if (event.type === "EVENT_GAP" || !Number.isSafeInteger(event.seq) || event.seq !== lastDelivered + 1) {
      gap();
      return;
    }
    lastDelivered = event.seq;
    onEvent(event);
    if (kind === "run" && terminal(event.data.status)
      || kind === "upload" && event.type === "session_status" && terminal(event.data.status)) close();
    if (kind === "turn" && ["message_final", "message_error", "continuation_final"].includes(event.type)) {
      void checkTurnTerminal().catch(() => { if (!closed) schedulePoll(); });
    }
  };
  const schedulePoll = () => {
    if (!closed && !timer && !request) timer = setTimeout(() => { timer = null; void poll(); }, 5000);
  };
  const poll = async () => {
    if (closed || request) return;
    const controller = new AbortController();
    request = controller;
    try {
      const options = { query: { after_seq: lastDelivered, ...(kind === "run" ? { limit: 200 } : {}) }, signal: controller.signal };
      const response = kind === "upload"
        ? await api<ApiResponses["GET /api/v1/upload-sessions/{session_id}/events"]["200"]>(eventsPath, options)
        : kind === "run"
          ? await api<ApiResponses["GET /api/v1/runs/{run_id}/events"]["200"]>(eventsPath, options)
          : await api<ApiResponses["GET /api/v1/conversations/{conversation_id}/turns/{turn_id}/events"]["200"]>(eventsPath, options);
      if (closed || controller.signal.aborted) return;
      const rows = Array.isArray(response) ? response : response.items;
      if (!Array.isArray(rows)) { gap(); return; }
      for (const row of rows.slice(0, 200)) {
        deliver({ type: String(row.event_type), seq: Number(row.seq), data: row as StreamEvent["data"] });
        if (closed) return;
      }
      if (kind === "turn" && rows.length < 200 && await checkTurnTerminal()) return;
      if (kind !== "turn" && rows.length < 200) {
        const snapshot = kind === "run"
          ? await api<ApiResponses["GET /api/v1/runs/{run_id}"]["200"]>(snapshotPath, { signal: controller.signal })
          : await api<ApiResponses["GET /api/v1/upload-sessions/{session_id}"]["200"]>(snapshotPath, { signal: controller.signal });
        if (closed || controller.signal.aborted) return;
        if (terminal(snapshot.status)) { close(); return; }
      }
      request = null;
      connect();
    } catch (error) {
      if (closed || controller.signal.aborted) return;
      if (error instanceof ApiError && [401, 403, 404, 410].includes(error.status)) {
        gap(false);
        return;
      }
    } finally {
      if (request === controller) request = null;
      if (!closed && es?.readyState !== EventSource.OPEN) schedulePoll();
    }
  };
  const connect = () => {
    if (closed || es) return;
    const source = new EventSource(`${API_PREFIX}${path}?after_seq=${lastDelivered}`, { withCredentials: true });
    es = source;
    onConnection?.(lastDelivered === afterSeq ? "connecting" : "reconnecting");
    source.onopen = () => {
      if (closed || es !== source) return;
      stopPoll();
      onConnection?.("open");
    };
    source.onerror = () => {
      if (closed || es !== source) return;
      source.close();
      es = null;
      onConnection?.("reconnecting");
      schedulePoll();
    };
    STREAM_EVENT_TYPES[kind].forEach((type) => source.addEventListener(type, (raw) => {
      if (closed || es !== source) return;
      const msg = raw as MessageEvent<string>;
      try {
        const data = JSON.parse(msg.data);
        deliver({ type, seq: Number(msg.lastEventId || data.seq), data });
      } catch { gap(); }
    }));
  };
  connect();
  return { close };
};

let factory: StreamFactory = eventSourceFactory;
const openHandles = new Set<StreamHandle>();



/** 身份切换／退出时关闭全部事件流。 */
export function closeAllStreams() {
  openHandles.forEach((h) => h.close());
  openHandles.clear();
}

/**
 * 订阅一条持久事件流并按 seq 去重。
 * onGap：发现缺号或 EVENT_GAP 时调用，由调用方重新读取快照（随后以新 last_seq 重订）。
 */
export function subscribeSequenced(
  opts: Omit<StreamOptions, "onEvent"> & { onEvent: (e: StreamEvent) => void; onGap: (recoverable?: boolean) => void },
): StreamHandle {
  let lastApplied = opts.afterSeq;
  let stopped = false;
  let handle: StreamHandle | undefined;
  const wrapped = {
    close() {
      stopped = true;
      handle?.close();
      openHandles.delete(wrapped);
    },
  };
  const gap = (recoverable = true) => {
    wrapped.close();
    opts.onGap(recoverable);
  };
  handle = factory({
    ...opts,
    onEvent: (e) => {
      if (stopped) return;
      if (e.type === "EVENT_GAP" || e.seq < 0) {
        gap(e.data.recoverable !== false);
        return;
      }
      if (e.seq <= lastApplied) return;
      if (e.seq > lastApplied + 1) {
        gap();
        return;
      }
      lastApplied = e.seq;
      opts.onEvent(e);
    },
  });
  if (stopped) handle.close(); else openHandles.add(wrapped);
  return wrapped;
}

/**
 * React 绑定：snapshotSeq 来自快照查询；事件到来时调用 onEvent，缺号时调用 refetch。
 * 返回连接状态与最后事件时间，供“正在恢复连接”与离线展示。
 */
export function useSequencedStream(args: {
  kind: StreamKind;
  path: string | null;
  snapshotSeq: number | undefined;
  onEvent: (e: StreamEvent) => void;
  refetch: () => unknown | Promise<unknown>;
}) {
  const { kind, path, snapshotSeq, refetch } = args;
  const onEventRef = useRef(args.onEvent);
  onEventRef.current = args.onEvent;
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;
  const [connection, setConnection] = useState<ConnectionState>("closed");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [recovery, setRecovery] = useState(0);
  const qc = useQueryClient();

  useEffect(() => {
    if (!path || snapshotSeq === undefined) return;
    let disposed = false;
    const h = subscribeSequenced({
      kind,
      path,
      afterSeq: snapshotSeq,
      onConnection: setConnection,
      onEvent: (e) => {
        setLastEventAt(e.data.occurred_at);
        onEventRef.current(e);
      },
      onGap: (recoverable = true) => {
        void Promise.all([Promise.resolve(refetchRef.current()), qc.invalidateQueries({ queryKey: ["me"] })])
          .then(() => { if (recoverable && !disposed) setRecovery((n) => n + 1); }).catch(() => {});
      },
    });
    return () => {
      disposed = true;
      h.close();
      setConnection("closed");
    };
  }, [kind, path, snapshotSeq, recovery, qc]);

  return { connection, lastEventAt };
}

/** 只有可靠分母时才返回百分比。 */
export function reliablePercent(d: { completed?: number; total?: number; unit?: string } | undefined | null) {
  if (!d || d.completed === undefined || !d.total || !d.unit) return null;
  return Math.max(0, Math.min(100, Math.round((d.completed / d.total) * 100)));
}
