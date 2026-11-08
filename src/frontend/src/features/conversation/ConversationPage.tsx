import type { ApiResponses } from "../../api/generated/request-contracts";
import { useInterfaceText } from "../settings/useInterfaceText";
// 对话工作台。消息、任务、事件与版本都只从服务端读；
// 本地仅保留未发送草稿、浮层开合与滚动位置。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, errorText } from "../../api/client";
import { consignmentApi } from "../../api/consignment";
import { subscribeSequenced, useSequencedStream } from "../../api/events";
import type { EvidenceTarget, MessageView, ReferenceSelection, RunView, TaskDraftView } from "../../api/types";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, Loading } from "../../components/Status";
import { ReviewHost } from "../review/ReviewHost";
import type { ReviewTargetSpec } from "../review/ReviewWorkspace";
import { useSession } from "../../stores/session";
import { Composer, EMPTY_DRAFT, type ComposerDraft } from "./Composer";
import { InputRequestDialog } from "./InputRequestDialog";
import { applyPreviewEvent, type AnswerPreview, type PreviewEvent } from "./preview";
import type { ProgressEvent } from "./progress";
import { MessageList } from "./MessageList";
import { ConversationHeader, ConversationSidebar, ExampleButtons } from "./Sidebar";
import { DocumentMainCard, RunCard, TaskDraftCard } from "./TaskCards";
import { UploadWizard, type UploadResult } from "./UploadWizard";
import { conversationApi, newKey, useCommand, useConversation, useInputRequests, useMessages, useRun, useTaskDraft, useTurn, workflowApi, type TurnBody } from "./hooks";

export function ConversationPage() {
  const uiText = useInterfaceText();
  const { conversationId } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const identity = useSession((s) => s.identity);
  const generation = useSession((s) => s.generation);

  const [convId, setConvId] = useState<string | null>(conversationId ?? null);
  const [draft, setDraft] = useState<ComposerDraft>(EMPTY_DRAFT);
  const setDraftAndRenew = (next: ComposerDraft | ((d: ComposerDraft) => ComposerDraft)) => {
    setDraft(next);
    // 草稿内容变化即属于新意图，换键，避免第二次发送被幂等缓存当成重放
    sendTurn.renewKey();
  };
  const [uploadOpen, setUploadOpen] = useState(false);
  const [replaceTarget, setReplaceTarget] = useState<{ documentId: string; expectedVersion: number } | null>(null);
  const [issueSupplement, setIssueSupplement] = useState<{ related_issue_id: string; expected_issue_version: number } | null>(null);
  const [uploadCaseId, setUploadCaseId] = useState<string | null>(null);
  // 已验收但尚未决定工作流的上传结果
  const [pendingUpload, setPendingUpload] = useState<UploadResult | null>(null);
  const uploadHandoff = useRef<{ scope: string; result: UploadResult; draft: ComposerDraft } | null>(null);
  // 从会话行菜单触发重命名时，通知顶栏进入编辑态
  const [renameSignal, setRenameSignal] = useState(0);
  const [reviewSpec, setReviewSpec] = useState<ReviewTargetSpec | null>(null);
  const evidenceRequest = useSession(s => s.evidenceRequest);
  useEffect(() => { if (!evidenceRequest) return; const target=evidenceRequest.target; setReviewSpec({caseId:target.case_id,runId:target.run_id??null,snapshotId:target.export_snapshot_id??null,target,intent:evidenceRequest.intent==="review"?"full":"quick"}); useSession.getState().clearEvidence(); }, [evidenceRequest]);
  const [inputOpen, setInputOpen] = useState(false);
  const [askEnd, setAskEnd] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [events, setEvents] = useState<(ProgressEvent & { runId: string })[]>([]);
  const [hasNewProgress, setHasNewProgress] = useState(false);
  const [previews, setPreviews] = useState<Record<string, AnswerPreview>>({});
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const draftScope = `${identity?.user_id ?? "anonymous"}:${convId ?? "new"}`;
  const [restoredDraftScope, setRestoredDraftScope] = useState<string | null>(null);
  useEffect(() => {
    const key = `conversation-draft:${draftScope}`;
    const handoff = uploadHandoff.current;
    uploadHandoff.current = null;
    if (handoff?.scope === draftScope) {
      setDraft(handoff.draft);
      setPendingUpload(handoff.result);
      setRestoredDraftScope(draftScope);
      setActiveTurnId(null);
      return;
    }
    try {
      const saved = sessionStorage.getItem(key);
      const parsed = saved ? JSON.parse(saved) as Partial<ComposerDraft> : null;
      setDraft(parsed && typeof parsed.text === "string" && Array.isArray(parsed.references) && Array.isArray(parsed.capabilities) && Array.isArray(parsed.attachments) ? parsed as ComposerDraft : EMPTY_DRAFT);
    } catch { setDraft(EMPTY_DRAFT); }
    setRestoredDraftScope(draftScope);
    setActiveTurnId(null);
    setPreviews({});
    setPendingUpload(null);
  }, [draftScope]);
  useEffect(() => {
    if (restoredDraftScope !== draftScope) return;
    try { sessionStorage.setItem(`conversation-draft:${draftScope}`, JSON.stringify(draft)); } catch { /* 存储不可用时仍允许编辑和发送。 */ }
  }, [draft, draftScope, restoredDraftScope]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const olderScroll = useRef<{ scope: string; height: number; top: number; firstId: string; pages: number } | null>(null);
  const lastVisibleSeq = useRef(0);

  const conv = useConversation(convId);
  const msgs = useMessages(convId);
  const run = useRun(conv.data?.active_run_id);
  const taskDraft = useTaskDraft(conv.data?.active_task_draft_id);
  const inputs = useInputRequests(conv.data?.active_run_id, run.data?.status === "awaiting_input");

  const messages = useMemo(() => (msgs.data?.items ?? []) as MessageView[], [msgs.data]);
  const latestTurn = useTurn(convId, activeTurnId ?? messages.at(-1)?.turn_id ?? null);
  const turnBusy = ["pending", "running"].includes(latestTurn.data?.status ?? "");
  useEffect(() => {
    if (!latestTurn.data) return;
    if (["pending", "running"].includes(latestTurn.data.status)) setActiveTurnId(latestTurn.data.turn_id);
    else {
      setActiveTurnId(null);
      void qc.invalidateQueries({ queryKey: ["c-msgs"] });
    }
  }, [latestTurn.data?.turn_id, latestTurn.data?.status, qc]);
  const linkedMessage = params.get("message_id");
  const focusedMessage = useRef<string | null>(null);
  useEffect(() => {
    if (!linkedMessage || !/^[a-f0-9-]{36}$/i.test(linkedMessage)) return;
    const key = `${generation}:${convId}:${linkedMessage}`;
    if (focusedMessage.current === key) return;
    if (messages.some(message => message.message_id === linkedMessage)) {
      atBottom.current = false;
      requestAnimationFrame(() => {
        const target = document.getElementById(`message-${linkedMessage}`);
        if (!target) return;
        target.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        target.focus({ preventScroll: true });
        focusedMessage.current = key;
      });
    } else if (msgs.hasNextPage && !msgs.isFetchingNextPage && !msgs.isFetchNextPageError) {
      void msgs.fetchNextPage();
    }
  }, [linkedMessage, convId, generation, messages, msgs.hasNextPage, msgs.isFetchingNextPage, msgs.isFetchNextPageError]);
  const ended = conv.data?.status === "ended" || !!conv.data?.archived_at;
  const messageScope = `${generation}:${convId ?? "new"}`;
  useLayoutEffect(() => {
    olderScroll.current = null;
    lastVisibleSeq.current = 0;
    atBottom.current = true;
    setHasNewProgress(false);
  }, [messageScope]);
  useLayoutEffect(() => {
    const anchor = olderScroll.current;
    if (anchor && anchor.scope === messageScope && (msgs.data?.pages.length ?? 0) > anchor.pages) {
      const el = scrollRef.current;
      if (el && messages[0]?.message_id !== anchor.firstId) el.scrollTop = anchor.top + el.scrollHeight - anchor.height;
      olderScroll.current = null;
    }
    const latest = messages.at(-1)?.seq ?? 0;
    if (lastVisibleSeq.current && latest > lastVisibleSeq.current && !atBottom.current) setHasNewProgress(true);
    lastVisibleSeq.current = latest;
  }, [messages, messageScope]);

  useLayoutEffect(() => {
    if (!Object.keys(previews).length) return;
    if (atBottom.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    else setHasNewProgress(true);
  }, [previews]);

  const loadOlderMessages = async () => {
    const el = scrollRef.current;
    if (!el || !messages.length || msgs.isFetching) return;
    olderScroll.current = { scope: messageScope, height: el.scrollHeight, top: el.scrollTop, firstId: messages[0].message_id, pages: msgs.data?.pages.length ?? 0 };
    atBottom.current = false;
    const result = await msgs.fetchNextPage({ cancelRefetch: false });
    if (result.isError && olderScroll.current?.scope === messageScope) olderScroll.current = null;
  };

  const sendTurn = useCommand<{ id: string; body: TurnBody }, { turn_id: string }>((a, key) => conversationApi.sendTurn(a.id, a.body, key), ["c-msgs", "c-conv", "c-list"]);
  const endConv = useCommand<void, unknown>((_a, key) => conversationApi.end(convId!, key), ["c-conv", "c-list", "c-msgs"]);
  const translationDraftBusy = useRef(false);
  const prepareTranslation = useCommand<{ conversationId: string; run: RunView }, TaskDraftView>(async (a, key) => {
    const sources = a.run.source_versions.map(({ document_id, document_version }) => ({ document_id, document_version }));
    const scope = await consignmentApi.scope(a.run.case_id);
    return workflowApi.createDraft({
      case_id: a.run.case_id,
      conversation_id: a.conversationId,
      workflow_type: "translate_review",
      workflow_version: 2,
      source_versions: sources,
      draft_parameters: {
        comparison_source_versions: sources,
        ...(scope.scope_revision_id ? { scope_revision_id: scope.scope_revision_id } : {}),
      },
    }, key);
  }, ["c-conv", "w-draft"]);
  useEffect(() => prepareTranslation.renewKey(), [convId, run.data?.run_id, prepareTranslation.renewKey]);

  useEffect(() => {
    if (conversationId && conversationId !== convId) setConvId(conversationId);
  }, [conversationId, convId]);

  // ---- 深链：?upload=1 | ?case_id=&reprocess=1 | ?replace_document_id=… ----
  const handledDeepLink = useRef<string | null>(null);
  useEffect(() => {
    const upload = params.get("upload") === "1";
    const reprocess = params.get("reprocess") === "1";
    const replaceDoc = params.get("replace_document_id");
    const caseId = params.get("case_id");
    if (!upload && !reprocess && !replaceDoc && !caseId) { handledDeepLink.current = null; return; }
    const key = params.toString();
    if (handledDeepLink.current === key) return;
    handledDeepLink.current = key;
    if (replaceDoc) setReplaceTarget({ documentId: replaceDoc, expectedVersion: Number(params.get("expected_document_version") ?? 1) });
    setUploadCaseId(caseId);
    const issueId = params.get("related_issue_id");
    setIssueSupplement(issueId ? { related_issue_id: issueId, expected_issue_version: Number(params.get("expected_issue_version")) } : null);
    // 从管理台／校验区跳来时新建会话，并按需预置该票引用；不携带旧会话消息或摘要
    void (async () => {
      try {
        const refs: ReferenceSelection[] = caseId ? [{ kind: "case", id: caseId, label: "这一票", selection_mode: "current", case_id: caseId }] : [];
        const c = await conversationApi.create({ initial_reference_selections: refs, entry_source: reprocess ? "management_reprocess" : "upload" }, newKey());
        setConvId(c.conversation_id);
        navigate(`/conversation/${c.conversation_id}`, { replace: true });
        if (upload || reprocess || replaceDoc) setUploadOpen(true);
      } catch (e) {
        toast({ tone: "risk", text: errorText(e) });
      }
    })();
    const next = new URLSearchParams(params);
    ["upload", "reprocess", "replace_document_id", "expected_document_version", "case_id", "related_issue_id", "expected_issue_version"].forEach((k) => next.delete(k));
    setParams(next, { replace: true });
  }, [params, navigate, setParams, toast]);

  // 每个待续答的原回合独立订阅，刷新先从持久消息和回合游标恢复。
  const pendingTurnIds = useMemo(() => Array.from(new Set([
    ...(activeTurnId ? [activeTurnId] : []),
    ...messages.filter((m) => m.status === "generating" || m.status === "interrupted" || m.status === "failed" || ["pending", "running", "awaiting_retry", "awaiting_reconcile"].includes(m.continuation?.status ?? "")).map((m) => m.parent_turn_id ?? m.turn_id),
    ...(messages.at(-1)?.turn_id ? [messages.at(-1)!.turn_id] : []),
  ])).sort(), [messages, activeTurnId]);
  const pendingTurnKey = pendingTurnIds.join(":");
  const [turnStreamGeneration, setTurnStreamGeneration] = useState(0);
  useEffect(() => {
    if (!convId) return;
    let cancelled = false;
    const handles: Array<{ close(): void }> = [];
    void Promise.all(pendingTurnIds.map(async (turnId) => {
      const snapshot = await conversationApi.turn(convId, turnId);
      if (cancelled) return;
      const turnKey = ["c-turn", generation, convId, turnId];
      qc.setQueryData(turnKey, snapshot);
      let replaySeq = 0;
      const replayEvents: PreviewEvent[] = [];
      do {
        const replay = await api<ApiResponses["GET /api/v1/conversations/{conversation_id}/turns/{turn_id}/events"]["200"]>(`/conversations/${convId}/turns/${turnId}/events`, { query: { after_seq: replaySeq } });
        if (cancelled) return;
        if (!replay.items.length) break;
        replayEvents.push(...replay.items.map(data => ({ type:data.event_type, data })));
        replaySeq = replay.items.at(-1)!.seq;
      } while (replaySeq < snapshot.last_seq);
      let streamPreviews = replayEvents.reduce((value, event) => applyPreviewEvent(value, turnId, event), {} as Record<string, AnswerPreview>);
      const publish = () => {
        if (cancelled) return;
        setPreviews(current => ({ ...Object.fromEntries(Object.entries(current).filter(([, value]) => value.turnId !== turnId)), ...streamPreviews }));
      };
      publish();
      if (!["pending", "running"].includes(snapshot.status) && !messages.some(m => (m.parent_turn_id ?? m.turn_id) === turnId && ["pending", "running", "awaiting_retry", "awaiting_reconcile"].includes(m.continuation?.status ?? ""))) {
        setActiveTurnId(current => current === turnId ? null : current);
        void msgs.refetch();
        return;
      }
      let batch: PreviewEvent[] = [], frame = 0;
      const renderBatch = () => {
        const events = batch; batch = []; frame = 0;
        if (cancelled) return;
        try {
          streamPreviews = events.reduce((value, event) => applyPreviewEvent(value, turnId, event), streamPreviews);
          publish();
        } catch {
          handle.close();
          streamPreviews = {}; publish();
          void msgs.refetch().then(() => { if (!cancelled) setTurnStreamGeneration(n => n + 1); });
        }
      };
      const handle = subscribeSequenced({ kind: "turn", path: `/conversations/${convId}/turns/${turnId}/events/stream`, afterSeq: replaySeq,
        isTerminal: async () => {
          const [turn, refreshed] = await Promise.all([conversationApi.turn(convId, turnId), msgs.refetch({ cancelRefetch: false })]);
          if (cancelled) return true;
          qc.setQueryData(turnKey, turn);
          if (refreshed.isError || !refreshed.data) return false;
          return !["pending", "running"].includes(turn.status) && !refreshed.data.items.some(message =>
            (message.parent_turn_id ?? message.turn_id) === turnId &&
            ["pending", "waiting", "running", "awaiting_retry", "awaiting_reconcile"].includes(message.continuation?.status ?? ""));
        },
        onEvent: event => {
          if (event.type !== "message_final") batch.push({type:event.type, data:event.data});
          if (!frame) frame = requestAnimationFrame(renderBatch);
          if (event.type.startsWith("answer_preview_") || event.type === "evidence_card") return;
          void qc.invalidateQueries({ queryKey: ["c-turn"] });
          void qc.invalidateQueries({ queryKey: ["c-msgs"] });
          void qc.invalidateQueries({ queryKey: ["c-conv"] });
          void qc.invalidateQueries({ queryKey: ["w-draft"] });
          if (["message_final", "message_error"].includes(event.type)) {
            // 等正式消息读取后才撤去预览，避免短暂消失。
            if (event.type === "message_final") void msgs.refetch().then(() => {
              streamPreviews = applyPreviewEvent(streamPreviews, turnId, {type:event.type, data:event.data});
              publish();
            });
            else void msgs.refetch();
            useSession.getState().refreshCapabilities();
            for (const key of ["ext-list", "ext-mcp", "ext-calls"]) void qc.invalidateQueries({ queryKey: [key] });
          }
          if (["message_final", "message_error", "continuation_final"].includes(event.type)) setActiveTurnId(current => current === turnId ? null : current);
        }, onGap: (recoverable = true) => {
          setPreviews({});
          void Promise.all([msgs.refetch(), qc.invalidateQueries({ queryKey: ["c-conv"] }), qc.invalidateQueries({ queryKey: ["me"] })])
            .then(() => { if (recoverable && !cancelled) setTurnStreamGeneration(n => n + 1); }).catch(() => {});
        },
      });
      handles.push({close: () => { handle.close(); if (frame) cancelAnimationFrame(frame); }});
      if (cancelled) handle.close();
    })).catch(() => { if (!cancelled) void msgs.refetch(); });
    return () => { cancelled = true; handles.forEach((handle) => handle.close()); };
  }, [convId, generation, pendingTurnKey, turnStreamGeneration, qc]);

  // ---- Run 事件流（W04）：只更新任务卡与「有新进度」，不抢消息滚动 ----
  const runStream = useSequencedStream({
    kind: "run",
    path: run.data && ["queued", "processing", "exporting"].includes(run.data.status) ? `/runs/${run.data.run_id}/events/stream` : null,
    snapshotSeq: run.data?.last_seq,
    onEvent: (e) => {
      const text = String(e.data.message ?? e.data.stage ?? e.data.status ?? e.type);
      setEvents((p) => [...p.filter((item) => item.runId === run.data?.run_id && item.seq !== e.seq).slice(-40), { runId: run.data!.run_id, seq: e.seq, at: e.data.occurred_at, text, stage: e.data.stage, type: e.type }]);
      if (!atBottom.current) setHasNewProgress(true);
      void qc.invalidateQueries({ queryKey: ["w-run"] });
      void qc.invalidateQueries({ queryKey: ["c-conv"] });
    },
    refetch: () => run.refetch(),
  });

  useEffect(() => {
    if (!atBottom.current) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages.length]);

  const openEvidence = (target: EvidenceTarget, intent: "quick" | "full") => {
    setReviewSpec({
      caseId: target.case_id,
      runId: target.run_id ?? conv.data?.active_run_id ?? null,
      snapshotId: target.export_snapshot_id ?? null,
      target,
      intent,
    });
  };

  /** 到管理台使用受控 ID 定位，不按名称搜索。 */
  const gotoManagement = () => {
    const q = new URLSearchParams();
    if (conv.data?.case_id) q.set("case_id", conv.data.case_id);
    if (run.data?.run_id) q.set("run_id", run.data.run_id);
    if (run.data?.latest_success_snapshot_id) q.set("snapshot_id", run.data.latest_success_snapshot_id);
    navigate(q.toString() ? `/documents?${q.toString()}` : "/documents");
  };

  const openReview = (intent: "quick" | "full") => {
    const caseId = conv.data?.case_id;
    if (!caseId) return;
    setReviewSpec({ caseId, runId: conv.data?.active_run_id ?? null, snapshotId: null, target: null, intent });
  };

  const ensureConversation = async () => {
    if (convId) return convId;
    const c = await conversationApi.create({}, newKey());
    try { sessionStorage.setItem(`conversation-draft:${identity?.user_id ?? "anonymous"}:${c.conversation_id}`, JSON.stringify(draft)); } catch { /* 保留内存草稿。 */ }
    setConvId(c.conversation_id);
    navigate(`/conversation/${c.conversation_id}`, { replace: true });
    return c.conversation_id;
  };

  const doSend = async () => {
    setSendError(null);
    try {
      const id = await ensureConversation();
      const r = await sendTurn.mutateAsync({
        id,
        body: {
          text: draft.text.trim(),
          references: draft.references,
          capabilities: draft.capabilities,
          // C04 只接受已验收文件对应的 upload_file_id
          upload_file_ids: draft.attachments.map((a) => a.uploadFileId),
          task_draft_id: conv.data?.active_task_draft_id ?? null,
        },
      });
      setActiveTurnId(r.turn_id);
      setDraft(EMPTY_DRAFT);
      sessionStorage.removeItem(`conversation-draft:${draftScope}`);
      await qc.invalidateQueries({ queryKey: ["c-msgs"] });
      await qc.invalidateQueries({ queryKey: ["c-conv"] });
    } catch (e) {
      const err = e as { code?: string };
      // TURN_BUSY：保留全部草稿，不隐式排第二轮
      setSendError(err.code === "TURN_BUSY" ? "上一条回复仍在生成，请稍后重试。你的输入已保留。" : errorText(e));
    }
  };

  /**
   * 上传完成只记录已验收原件并询问要做什么。
   * 不预设翻译工作流：只问文件内容应走只读识别，不该追问目标语言。
   */
  const onUploadDone = async (r: UploadResult) => {
    setUploadOpen(false);
    setReplaceTarget(null);
    setIssueSupplement(null);
    setUploadCaseId(null);
    try {
      const wasNew = !convId;
      const id = await ensureConversation();
      const nextDraft = { ...draft, attachments: [...draft.attachments, ...r.verified.map((v, i) => ({ uploadFileId: r.uploadFileIds[i], filename: v.filename, sizeBytes: v.sizeBytes }))] };
      if (wasNew) uploadHandoff.current = { scope: `${identity?.user_id ?? "anonymous"}:${id}`, result: r, draft: nextDraft };
      setPendingUpload(r);
      setDraft(nextDraft);
      await qc.invalidateQueries({ queryKey: ["c-conv"] });
      toast({ tone: "ok", text: `已验收 ${r.verified.length} 份原件，请选择要做什么` });
    } catch (e) {
      toast({ tone: "risk", text: errorText(e) });
    }
  };

  /** 用户明确选择后才建对应工作流的草稿。 */
  const chooseWorkflow = async (workflow: "read_index" | "translate_review" | "consignment_check") => {
    if (!pendingUpload) return;
    try {
      const cid = await ensureConversation();
      await workflowApi.createDraft(
        {
          case_id: pendingUpload.caseId,
          workflow_type: workflow,
          workflow_version: workflow === "translate_review" ? 2 : 1,
          source_versions: pendingUpload.verified.map((v) => ({ document_id: v.documentId, document_version: v.documentVersion })),
          conversation_id: cid,
        },
        newKey(),
      );
      setPendingUpload(null);
      await qc.invalidateQueries({ queryKey: ["c-conv"] });
      await qc.invalidateQueries({ queryKey: ["w-draft"] });
    } catch (e) {
      toast({ tone: "risk", text: errorText(e) });
    }
  };

  const startTranslationFromRun = async () => {
    const sourceRun = run.data;
    if (!convId || ended || !sourceRun || !["indexed", "checked"].includes(sourceRun.status) || translationDraftBusy.current) return;
    translationDraftBusy.current = true;
    try {
      await prepareTranslation.mutateAsync({ conversationId: convId, run: sourceRun });
    } catch (e) {
      toast({ tone: "risk", text: errorText(e) });
    } finally {
      translationDraftBusy.current = false;
    }
  };

  const startNewConversation = () => {
    setConvId(null);
    setDraft(EMPTY_DRAFT);
    setEvents([]);
    navigate("/conversation");
  };

  const continueInNew = async (caseId?: string | null) => {
    const cid = caseId ?? conv.data?.case_id;
    try {
      const refs: ReferenceSelection[] = cid ? [{ kind: "case", id: cid, label: conv.data?.case_name ?? "这一票", selection_mode: "current", case_id: cid }] : [];
      const c = await conversationApi.create({ initial_reference_selections: refs, entry_source: "continue" }, newKey());
      setConvId(c.conversation_id);
      setDraft(EMPTY_DRAFT);
      navigate(`/conversation/${c.conversation_id}`);
      toast({ tone: "ok", text: "已新建会话，只预置了这一票，不带旧会话的消息与摘要" });
    } catch (e) {
      toast({ tone: "risk", text: errorText(e) });
    }
  };

  const openCrossCaseReference = async (selection: ReferenceSelection) => {
    const created = await conversationApi.create({ initial_reference_selections: [selection], entry_source: "cross_case_reference" }, newKey());
    setConvId(created.conversation_id);
    navigate(`/conversation/${created.conversation_id}`);
  };

  const hasDraft = draft.text.trim().length > 0 || draft.attachments.length > 0 || draft.references.length > 0 || draft.capabilities.length > 0;
  const reviewEnabled = !!conv.data?.case_id;
  const activeInput = inputs.data?.items.find((i) => i.status === "active") ?? null;

  return (
    <div className="flex h-full min-h-0">
      <ConversationSidebar
        currentId={convId}
        hasDraft={hasDraft}
        onNewConversation={startNewConversation}
        onOpen={(id) => {
          setConvId(id);
          setDraft(EMPTY_DRAFT);
          navigate(`/conversation/${id}`);
        }}
        onRename={(id) => {
          setConvId(id);
          navigate(`/conversation/${id}`);
          setRenameSignal((n) => n + 1);
        }}
        onContinueInNew={(cid) => void continueInNew(cid)}
      />

      <div style={{ paddingRight: "var(--review-reserved-width, 0px)" }} className="flex min-w-0 flex-1 flex-col">
        <ConversationHeader
          conv={conv.data ?? null}
          run={run.data ?? null}
          reviewEnabled={reviewEnabled}
          reviewDisabledReason="先上传或关联单据后才能打开校验区"
          reviewOpen={!!reviewSpec}
          onToggleReview={() => (reviewSpec ? setReviewSpec(null) : openReview("quick"))}
          onEndConversation={() => setAskEnd(true)}
          onContinueInNew={() => void continueInNew()}
          renameSignal={renameSignal}
        />

        <div
          ref={scrollRef}
          className="min-h-0 flex-1 overflow-auto scroll-thin px-6 pb-6"
          onScroll={(e) => {
            const el = e.currentTarget;
            atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
            if (atBottom.current) setHasNewProgress(false);
          }}
        >
          <div className="mx-auto max-w-[760px]">
            {conv.isError && <ErrorBar text={errorText(conv.error)} action={{ label: "重试", onSelect: () => void conv.refetch() }} />}

            {!convId && (
              <div className="pt-[72px]">
                <h2 className="text-[20px] font-semibold text-ink">你好{identity ? `，${identity.display_name}` : ""}</h2>
                <p className="mt-1 text-[13px] text-ink-2">{uiText("上传同票单据做识别与翻译，或先问一个物流单证的问题。", "Upload documents for one consignment, or ask a logistics document question.")}</p>
                <div className="mt-6">
                  <DocumentMainCard onUpload={() => setUploadOpen(true)} />
                </div>
                <ExampleButtons hasCase={false} onPick={(t) => setDraftAndRenew((d) => ({ ...d, text: t }))} />
              </div>
            )}

            {convId && msgs.isPending && (
              <div className="flex justify-center py-12">
                <Loading text="正在读取消息" />
              </div>
            )}

            {msgs.isError && <ErrorBar text={errorText(msgs.error)} action={{ label: "重试读取消息", onSelect: () => void msgs.refetch() }} />}
            {msgs.hasNextPage && <div className="flex justify-center py-3"><button type="button" className="btn btn-ghost btn-sm" disabled={msgs.isFetching} onClick={() => void loadOlderMessages()}>
              {msgs.isFetchingNextPage ? "正在读取更早消息" : "加载更早消息"}
            </button></div>}

            {convId && !conv.data?.archived_at && !run.data && !taskDraft.data && !pendingUpload && !msgs.isPending && messages.length === 0 && (
              <div className="pt-[72px]">
                <h2 className="text-[20px] font-semibold text-ink">这个会话还没有消息</h2>
                <p className="mt-1 text-[13px] text-ink-2">{uiText("可以上传单据，或直接提问。", "Upload documents or ask a question.")}</p>
                <div className="mt-6">
                  <DocumentMainCard onUpload={() => setUploadOpen(true)} />
                </div>
                <ExampleButtons hasCase={!!conv.data?.case_id} onPick={(t) => setDraftAndRenew((d) => ({ ...d, text: t }))} />
              </div>
            )}

            {messages.length > 0 && (
              <MessageList
                key={convId}
                onReveal={() => { if (atBottom.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }}
                previews={Object.values(previews)}
                messages={messages}
                onOpenEvidence={openEvidence}
                onOpenRun={gotoManagement}
                onEndService={() => setAskEnd(true)}
                onContinueInNew={(cid) => void continueInNew(cid)}
              />
            )}
          </div>
        </div>

        <div className="shrink-0 px-6 pb-6">
          <div className="mx-auto max-w-[760px] space-y-2">
            {hasNewProgress && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
                  setHasNewProgress(false);
                }}
              >
                有新进度，回到最新
              </button>
            )}

            {!conv.data?.archived_at && run.data && !(taskDraft.data && !taskDraft.data.started_run_id) && (
              <RunCard
                run={run.data}
                events={events.filter((event) => event.runId === run.data?.run_id)}
                connection={runStream.connection}
                onOpenReview={() => openReview("full")}
                onOpenRecord={gotoManagement}
                onEndService={() => setAskEnd(true)}
                onResolveInput={() => { setInputOpen(true); if (!activeInput) void inputs.refetch(); }}
                onStartTranslate={!ended && ["indexed", "checked"].includes(run.data.status) ? () => void startTranslationFromRun() : undefined}
              />
            )}

            {prepareTranslation.isPending && <p role="status" className="text-[12px] text-ink-2">正在准备翻译要求…</p>}

            {!conv.data?.archived_at && pendingUpload && !taskDraft.data && (
              <section className="card p-4">
                <p className="text-[13px] font-medium text-ink">已验收 {pendingUpload.verified.length} 份原件，要做什么？</p>
                <p className="mt-1 text-[12px] text-ink-2">只问文件内容时先识别，不需要指定目标语言；需要译本时选择翻译与校验。</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void chooseWorkflow("read_index")}>
                    先识别以回答文件内容
                  </button>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void chooseWorkflow("consignment_check")}>
                    同票多单证核对
                  </button>
                  <button type="button" className="btn btn-primary btn-sm" onClick={() => void chooseWorkflow("translate_review")}>
                    翻译并校验
                  </button>
                </div>
              </section>
            )}

            {!conv.data?.archived_at && taskDraft.data && !taskDraft.data.started_run_id && (
              <TaskDraftCard
                draft={taskDraft.data}
                onStarted={() => void qc.invalidateQueries({ queryKey: ["c-conv"] })}
                onDiscarded={() => void qc.invalidateQueries({ queryKey: ["c-conv"] })}
                onUpload={() => setUploadOpen(true)}
              />
            )}

            {!conv.data?.archived_at && !run.data && !taskDraft.data && messages.length > 0 && <DocumentMainCard compact onUpload={() => setUploadOpen(true)} />}

            <Composer
              draft={draft}
              onDraftChange={setDraftAndRenew}
              onSend={() => void doSend()}
              onPickFiles={() => setUploadOpen(true)}
              onCrossCaseReference={openCrossCaseReference}
              sending={sendTurn.isPending || turnBusy}
              sendError={sendError}
              onRetry={() => void doSend()}
              caseId={conv.data?.case_id ?? null}
              readOnly={ended}
            />
            {ended && (
              <button type="button" className="btn btn-primary" onClick={() => void continueInNew()}>
                在新对话继续
              </button>
            )}
          </div>
        </div>
      </div>

      {reviewSpec && <ReviewHost spec={reviewSpec} onClose={() => setReviewSpec(null)} />}

      <UploadWizard
        open={uploadOpen}
        caseId={uploadCaseId ?? conv.data?.case_id ?? null}
        caseName={uploadCaseId === conv.data?.case_id ? conv.data?.case_name ?? null : uploadCaseId ? null : conv.data?.case_name ?? null}
        replaceTarget={replaceTarget}
        issueSupplement={issueSupplement}
        onClose={() => {
          setUploadOpen(false);
          setReplaceTarget(null);
          setIssueSupplement(null);
          setUploadCaseId(null);
        }}
        onDone={(r) => void onUploadDone(r)}
      />

      {inputOpen && !activeInput && <Dialog open title="读取待确认事项" onClose={() => setInputOpen(false)} footer={<button type="button" className="btn btn-secondary" onClick={() => setInputOpen(false)}>关闭</button>}>
        {inputs.isFetching ? <Loading text="正在读取最新确认内容" /> : <ErrorBar text={inputs.isError ? errorText(inputs.error) : "当前没有待确认事项，正在重新核对任务状态。"} action={{label:"重新读取",onSelect:()=>{void inputs.refetch();void run.refetch();}}} />}
      </Dialog>}
      {inputOpen && activeInput && <InputRequestDialog key={`${activeInput.input_request_id}:${activeInput.request_version}`} request={activeInput} sources={run.data?.source_versions ?? []} onClose={() => setInputOpen(false)} onLocate={(t) => openEvidence(t, "quick")} />}

      <Dialog
        open={askEnd}
        onClose={() => setAskEnd(false)}
        title="结束本次服务"
        description="结束后这个会话变为只读。后台正在执行的处理任务不会被取消，原件与历史快照仍可在单据管理台查看。"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setAskEnd(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={endConv.isPending}
              onClick={() =>
                endConv.mutate(undefined, {
                  onSuccess: () => {
                    setAskEnd(false);
                    toast({ tone: "ok", text: "已结束本次服务，会话转为只读" });
                  },
                })
              }
            >
              确认结束
            </button>
          </>
        }
      >
        {endConv.error && <ErrorBar text={errorText(endConv.error)} />}
      </Dialog>
    </div>
  );
}
