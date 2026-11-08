import { useReviewPreference } from "../../stores/reviewPreference";
import { useSession } from "../../stores/session";
// 校验工作台主体：侧板与全屏共用同一套数据与状态。
// 布局按实测可用宽度决定，不靠设备名猜断点；确认与导出是两个独立命令。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EvidenceTarget, FieldView, IssueView, ReviewView, TranslationSegmentView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { reviewApi } from "../../api/review";
import { errorText } from "../../api/client";
import { englishError } from "../../i18n/runtime";
import { ErrorBar, IssueCounts, Loading, StatusPill, formatTime, runTone } from "../../components/Status";
import { Menu, useEscLayer } from "../../components/Overlay";
import { ComparisonPanel } from "./ComparisonPanel";
import { FieldsPanel, type FieldAction } from "./FieldsPanel";
import { FileTree } from "./FileTree";
import { ChecksList, IssuesPanel, type IssueDisposition } from "./IssuesPanel";
import { PreviewViewer } from "./PreviewViewer";
import { ChecklistDialog, ExportDialog, ExportProgress, SnapshotFiles } from "./ReviewExport";
import { ConfirmLanguageForm, FieldForm, InputRequestForm, IssueForm, ReparseForm, ReplaceDocumentForm, SectionAssignForm, SegmentForm, SwitchToWorkingDialog, UnsavedDialog } from "./ReviewForms";
import { SourceViewer, type AnchorBox } from "./SourceViewer";
import { TranslationViewer } from "./TranslationViewer";
import { useInputRequests, useFields, useIssues, useResolveEvidence, useReviewStream, useReviewView, useRun, useSnapshot, useTimeline, useTranslations } from "./hooks";

type Tab = "overview" | "bilingual" | "fields" | "issues" | "record" | "comparison";
type Layout = "bilingual" | "fields_evidence" | "three";
type IssueFilter = "open" | "all" | "blocking" | "warning" | "undetermined" | "done";
type IssueReturnPoint = { issueId: string; tab: Tab; drawer: boolean; filter: IssueFilter; documentFilter: string | null; documentId: string | null; sectionId: string | null; scrollTop: number };
type AnchorChoice = { kind: "field"; field: FieldView } | { kind: "segment"; segment: TranslationSegmentView };

export interface ReviewTargetSpec {
  caseId: string;
  runId?: string | null;
  snapshotId?: string | null;
  target?: EvidenceTarget | null;
  intent: "full" | "quick";
}

export function reviewTargetKey(spec: ReviewTargetSpec) {
  return JSON.stringify([spec.caseId, spec.runId ?? null, spec.snapshotId ?? null, spec.intent, spec.target ?? null]);
}

export function ReviewWorkspace({
  spec,
  width,
  fullscreen,
  onClose,
  onExpand,
  onBack,
  onToggleFullscreen,
  onUploadMore,
  onSwitchToWorking,
  onOpenRun,
}: {
  spec: ReviewTargetSpec;
  width: number;
  fullscreen: boolean;
  onClose: () => void;
  onExpand?: () => void;
  onBack?: { label: string; onSelect: () => void };
  onToggleFullscreen?: () => void;
  /** 上传补充材料／替换原件：回到对话的上传向导并预置本 Case */
  onUploadMore?: (caseId: string, context?: { replaceDocumentId?: string; expectedVersion?: number; relatedIssueId?: string; expectedIssueVersion?: number }) => void;
  /** 历史只读视图切回当前工作版 */
  onSwitchToWorking?: () => void;
  onOpenRun?: (runId: string) => void;
}) {
  // R01 是打开门控：带显式目标时先校验归属与版本配对，通过后才取 R02。
  // 失败不打开工作台，也不从缓存显示任何原文。
  const resolved = useResolveEvidence(spec.target ?? null);
  const gateOpen = !spec.target || resolved.isSuccess;
  const scope = { caseId: spec.caseId, runId: spec.runId, snapshotId: spec.snapshotId, parseArtifactId: spec.target?.parse_artifact_id, businessRevisionId: spec.target?.business_revision_id };
  // 固定原件来源只读取该文件，避免历史版分页借用文件树首项的另一解析批次。
  const fixedDocumentId = spec.target?.parse_artifact_id ? resolved.data?.normalized_target.document_id ?? undefined : undefined;
  const viewQ = useReviewView(scope, fixedDocumentId, gateOpen);
  const view = viewQ.data;
  const run = useRun(gateOpen && !spec.snapshotId ? view?.run_id ?? spec.runId : null);
  const openedRun = useRef<string | null>(null);
  useReviewStream(view);

  const uiScope = `${spec.caseId}:${spec.snapshotId ?? spec.runId ?? "source"}:${spec.target?.parse_artifact_id ?? "current"}`;
  const [savedUi, setSavedUi] = useReviewPreference<{ documentId: string | null; sectionId: string | null; tab: Tab; layout: Layout; issue: string | null; field: string | null; anchor: string | null; segment: string | null; page: number | null; tree: boolean; fontSize: number }>(uiScope, { documentId: null, sectionId: null, tab: "overview", layout: "bilingual", issue: null, field: null, anchor: null, segment: null, page: null, tree: true, fontSize: 14 });
  const [documentId, setDocumentId] = useState<string | null>(spec.target ? null : savedUi.documentId);
  const [sectionId, setSectionId] = useState<string | null>(spec.target ? spec.target.section_id ?? null : savedUi.sectionId);
  const [tab, setTab] = useState<Tab>(spec.intent === "quick" ? "bilingual" : spec.target?.field_id ? "fields" : spec.target?.issue_id ? "bilingual" : savedUi.tab);
  const [layout, setLayout] = useState<Layout>(savedUi.layout);
  const [selectedAnchor, setSelectedAnchor] = useState<string | null>(spec.target ? spec.target.source_segment_id ?? spec.target.source_anchor_id ?? null : savedUi.anchor);
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(spec.target ? spec.target.field_id ?? null : savedUi.field);
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(spec.target ? spec.target.issue_id ?? null : savedUi.issue);
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(spec.target ? spec.target.translation_segment_id ?? null : savedUi.segment);
  const [targetPage, setTargetPage] = useState<number | null>(spec.target ? null : savedUi.page);
  const [navToken, setNavToken] = useState(0);
  const [issueFilter, setIssueFilter] = useState<IssueFilter>("open");
  const [docFilter, setDocFilter] = useState<string | null>(null);
  const [narrowCol, setNarrowCol] = useState<"source" | "translation" | "segments">("source");
  const [fieldEvidencePane, setFieldEvidencePane] = useState<"source" | "translation">("source");
  const [segmentsOpen, setSegmentsOpen] = useState(false);

  useEffect(() => {
    if (run.data?.workflow_type === "consignment_check" && openedRun.current !== run.data.run_id && !spec.target) { openedRun.current = run.data.run_id; setTab("comparison"); }
  }, [run.data, spec.target]);

  // 表单与弹层
  const [fieldAction, setFieldAction] = useState<FieldAction | null>(null);
  const [issueAction, setIssueAction] = useState<{ issue: IssueView; kind: IssueDisposition } | null>(null);
  const [segmentAction, setSegmentAction] = useState<TranslationSegmentView | null>(null);
  const [pendingSegmentEdit, setPendingSegmentEdit] = useState<string | null>(null);
  const [assign, setAssign] = useState<{ sectionId: string; suggestion: string } | null>(null);
  const [reparse, setReparse] = useState(false);
  const [confirmLang, setConfirmLang] = useState(false);
  const [replaceDoc, setReplaceDoc] = useState(false);
  const [switchWorking, setSwitchWorking] = useState(false);
  const [checklistOpen, setChecklistOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const [inputOpen, setInputOpen] = useState(false);
  const [unsaved, setUnsaved] = useState(false);
  const [treeOpen, setTreeOpen] = useState(savedUi.tree);
  const [fontSize, setFontSize] = useState(savedUi.fontSize);
  const [issuesDrawerOpen, setIssuesDrawerOpen] = useState(!!spec.target?.issue_id);
  const [issueReturnPoint, setIssueReturnPoint] = useState<IssueReturnPoint | null>(null);
  const [issueRestore, setIssueRestore] = useState<{ token: number; issueId: string; top: number } | undefined>();
  const issueReturnSequence = useRef(0);
  const workspaceRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setSavedUi({ documentId, sectionId, tab, layout, issue: selectedIssueId, field: selectedFieldId, anchor: selectedAnchor, segment: selectedSegmentId, page: targetPage, tree: treeOpen, fontSize }); }, [documentId, sectionId, tab, layout, selectedIssueId, selectedFieldId, selectedAnchor, selectedSegmentId, targetPage, treeOpen, fontSize, setSavedUi]);
  const moreRef = useRef<HTMLButtonElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);

  const hasOpenForm = !!(fieldAction || issueAction || segmentAction || assign || reparse || confirmLang || replaceDoc);
  /**
   * 操作栏「保存草稿」只作用于当前已打开的编辑器：把焦点移回该表单的保存按钮并触发它，
   * 不新建通用草稿接口，也不等同于「复核完成」。
   */
  const submitOpenForm = useCallback(() => {
    const dialog = document.querySelector('[role="dialog"]');
    const save = [...(dialog?.querySelectorAll("button") ?? [])].find((b) => /保存|确认此字段|接受并记录/.test(b.innerText) && !b.disabled);
    if (save) save.click();
    else (dialog?.querySelector("textarea, input") as HTMLElement | null)?.focus();
  }, []);

  const fieldsQ = useFields(view, null, undefined, documentId);
  const [anchorChoices, setAnchorChoices] = useState<AnchorChoice[]>([]);
  const [anchorLookup, setAnchorLookup] = useState<{ id: string; loading: boolean; error: string | null } | null>(null);
  const anchorLookupGeneration = useRef(0);
  const identityGeneration = useSession(s => s.generation);
  useEffect(() => { setIssueReturnPoint(null); setIssueRestore(undefined); }, [identityGeneration, view?.case_id, view?.run_id, view?.snapshot_id]);
  // 首次进入：选中目标文件，或第一个未处理红项
  const issuesQ = useIssues(view, undefined, undefined);
  useEffect(() => {
    if (!view?.files.length || documentId) return;
    const targetDoc = resolved.data?.normalized_target.document_id;
    setDocumentId(targetDoc && view.files.some((f) => f.source.document_id === targetDoc) ? targetDoc : view.files[0].source.document_id);
  }, [view, documentId, resolved.data]);


  const file = useMemo(() => view?.files.find((f) => f.source.document_id === documentId) ?? view?.files[0], [view, documentId]);
  const segmentsQ = useTranslations(view, documentId, null);
  const segments = segmentsQ.items;
  useEffect(() => {
    if (selectedSegmentId && !segments.some(segment=>segment.translation_segment_id===selectedSegmentId) && segmentsQ.hasNextPage && !segmentsQ.isFetching && !segmentsQ.error) void segmentsQ.fetchNextPage();
  }, [selectedSegmentId, segments, segmentsQ.hasNextPage, segmentsQ.isFetching, segmentsQ.error, segmentsQ.fetchNextPage]);
  useEffect(() => {
    if (!pendingSegmentEdit) return;
    const segment = segments.find(item => item.translation_segment_id === pendingSegmentEdit);
    if (segment) { setSegmentAction(segment); setPendingSegmentEdit(null); }
  }, [pendingSegmentEdit, segments]);
  const inputRequests = useInputRequests(run.data?.run_id, run.data?.status === "awaiting_input");
  const activeRequest = inputRequests.data?.items.find((r) => r.status === "active") ?? null;

  const editable = view?.mode === "working" && !!view.allowed_actions.some(action => ["revise_field", "confirm_field", "mark_field_unknown", "assign_section", "revise_segment"].includes(action));

  /** 从文件树的逻辑单据页范围推出所在页；无法确定时只作页级提示，不猜具体位置。 */
  const pageOfSection = useCallback((f: ReviewView["files"][number] | undefined, sectionId?: string) => {
    if (!f || !sectionId) return 1;
    const ld = f.logical_documents.find((l) => l.sections.some((s) => s.section_id === sectionId));
    const m = ld?.page_range.match(/\d+/);
    return m ? Number(m[0]) : 1;
  }, []);

  /**
   * 一次性定位：递增 navToken 取消上一次滚动任务。
   * 显式来源点击会重新开启自动跟随（用户手动滚动只停止当次跟随）。
   */
  const locate = useCallback(
    (target: EvidenceTarget, opts: { field?: string; issue?: string; segment?: string } = {}) => {
      if (view?.mode === "historical" && view.snapshot_id && target.export_snapshot_id && target.export_snapshot_id !== view.snapshot_id) return;
      anchorLookupGeneration.current++; setAnchorChoices([]); setAnchorLookup(null);
      const scopedTarget = view?.mode === "historical" && view.snapshot_id ? { ...target, export_snapshot_id: view.snapshot_id } : target;
      if (target.document_id && target.document_id !== documentId) setDocumentId(target.document_id);
      if (target.section_id) setSectionId(target.section_id);
      setSelectedAnchor(target.source_segment_id ?? target.source_anchor_id ?? target.section_id ?? null);
      setSelectedFieldId(opts.field ?? null);
      if (opts.issue) setSelectedIssueId(opts.issue);
      setSelectedSegmentId(opts.segment ?? target.translation_segment_id ?? segments.find(segment=>segment.document_id === target.document_id && (segment.source_segment_ids ?? [segment.source_segment_id]).includes(target.source_segment_id ?? ""))?.translation_segment_id ?? null);
      const f = view?.files.find((x) => x.source.document_id === target.document_id);
      setTargetPage(pageOfSection(f, target.section_id ?? undefined));
      const token=++locationGeneration.current;
      setLocatedResolution(null);
      setLocateError(null);
      void reviewApi.resolve(scopedTarget).then((value) => {
        if(token!==locationGeneration.current)return;
        if (value.page_no) setTargetPage(value.page_no);
        const normalized = value.normalized_target;
        setSelectedAnchor(normalized.source_segment_id ?? normalized.source_anchor_id ?? target.source_segment_id ?? target.source_anchor_id ?? null);
        if (normalized.translation_segment_id) setSelectedSegmentId(normalized.translation_segment_id);
        setLocatedResolution(value);
      }).catch(error => {if(token===locationGeneration.current){setLocatedResolution(null);setLocateError(errorText(error));}});
      setNavToken((t) => t + 1);
    },
    [documentId, view, pageOfSection, segments],
  );

  const [locatedResolution, setLocatedResolution] = useState<import("../../api/types").EvidenceResolution | null>(null);
  const [locateError, setLocateError] = useState<string | null>(null);
  const locationGeneration=useRef(0);
  const clearLocation=()=>{locationGeneration.current++;anchorLookupGeneration.current++;setAnchorChoices([]);setAnchorLookup(null);setLocatedResolution(null);setLocateError(null);setSelectedAnchor(null);setSelectedSegmentId(null);setTargetPage(null);};
  const locateIssueSource = (target: EvidenceTarget, issueId: string, drawer = false) => {
    const scroller = workspaceRef.current?.querySelector<HTMLElement>(`[data-review-issues-${drawer ? "drawer" : "main"}] [data-review-issues-scroll]`);
    setIssueReturnPoint({ issueId, tab, drawer, filter: issueFilter, documentFilter: docFilter, documentId, sectionId, scrollTop: scroller?.scrollTop ?? 0 });
    setIssueRestore(undefined);
    locate(target, { issue: issueId, segment: target.translation_segment_id ?? undefined });
    if (drawer || !(width >= 1296 && layout === "three")) {
      setTab("bilingual"); setNarrowCol(target.translation_segment_id ? "translation" : "source");
    }
    setIssuesDrawerOpen(false);
  };
  const returnToIssue = () => {
    if (!issueReturnPoint) return;
    clearLocation();
    setDocumentId(issueReturnPoint.documentId); setSectionId(issueReturnPoint.sectionId);
    setIssueFilter(issueReturnPoint.filter); setDocFilter(issueReturnPoint.documentFilter);
    setSelectedIssueId(issueReturnPoint.issueId); setTab(issueReturnPoint.tab); setIssuesDrawerOpen(issueReturnPoint.drawer);
    setIssueRestore({ token: ++issueReturnSequence.current, issueId: issueReturnPoint.issueId, top: issueReturnPoint.scrollTop });
    setIssueReturnPoint(null);
  };
  const locateTranslation=(id:string)=>{const segment=segments.find(value=>value.translation_segment_id===id);if(segment)locate(segment.target,{segment:id});else setSelectedSegmentId(id);};
  const editIssue = (issue: IssueView, kind: "field" | "translation") => {
    const sides = [issue.a, issue.b].filter(Boolean);
    const target = (kind === "translation" ? issue.guidance?.target?.translation_segment_id ? issue.guidance.target : sides.find(side => side?.target.translation_segment_id)?.target : sides.find(side => side?.target.field_id)?.target) ?? issue.a?.target ?? issue.b?.target;
    if (!target) return;
    setIssuesDrawerOpen(false);
    if (kind === "field") {
      locate(target, { issue: issue.issue_id, field: target.field_id ?? undefined });
      setTab("fields"); setLayout("fields_evidence");
    } else {
      locate(target, { issue: issue.issue_id, segment: target.translation_segment_id ?? undefined });
      setTab("bilingual"); setSegmentsOpen(true); setNarrowCol("segments");
      const segment = segments.find(item => item.translation_segment_id === target.translation_segment_id);
      if (segment) setSegmentAction(segment);
      else if (target.translation_segment_id) setPendingSegmentEdit(target.translation_segment_id);
    }
  };
  const selectAnchorChoice = (choice: AnchorChoice) => {
    if (choice.kind === "field") {
      locate(choice.field.target, { field: choice.field.field_id }); setTab("fields"); setLayout("fields_evidence");
    } else {
      locate(choice.segment.target, { segment: choice.segment.translation_segment_id, field: tab === "fields" ? selectedFieldId ?? undefined : undefined });
      if (tab === "fields") { setLayout("fields_evidence"); setFieldEvidencePane("translation"); }
      else { setTab("bilingual"); setNarrowCol("translation"); }
    }
  };
  useEffect(() => {
    anchorLookupGeneration.current++;
    setAnchorChoices([]); setAnchorLookup(null);
    return () => { anchorLookupGeneration.current++; };
  }, [identityGeneration, view?.case_id, view?.run_id, view?.snapshot_id, view?.business_revision_id, view?.translation_revision_id, view?.review_state_version, file?.parse_artifact_id, file?.source.document_id]);
  const pickAnchor = async (id: string) => {
    if (!view || !file) return;
    const generation = ++anchorLookupGeneration.current;
    const location = locationGeneration.current;
    const stillCurrent = () => generation === anchorLookupGeneration.current && location === locationGeneration.current && identityGeneration === useSession.getState().generation;
    setAnchorChoices([]); setSelectedAnchor(id); setAnchorLookup({ id, loading: true, error: null });
    const targets = [currentResolution?.normalized_target, ...fieldsQ.items.map(f => f.target), ...segments.map(s => s.target), ...issuesQ.items.flatMap(i => [i.a?.target, i.b?.target])];
    const sourceTarget = targets.find(t => t?.source_segment_id === id || t?.source_anchor_id === id);
    const sourceId = sourceTarget?.source_segment_id === id || !sourceTarget ? id : undefined;
    // 段落级提示没有源段坐标，不把 section_id 当作 source_segment_id 查询。
    const sectionOnly = !sourceTarget && targets.some(t => t?.section_id === id);
    try {
      const matches: FieldView[] = [];
      let cursor: string | undefined;
      if (!sectionOnly) do {
        const page = await reviewApi.fields(view.case_id, {
          run_id: view.run_id, snapshot_id: view.snapshot_id,
          business_revision_id: view.business_revision_id,
          review_state_version: view.mode === "working" ? view.review_state_version : undefined,
          document_id: file.source.document_id, parse_artifact_id: file.parse_artifact_id,
          source_segment_id: sourceId, source_anchor_id: sourceId ? undefined : id,
          cursor, limit: 200,
        });
        if (!stillCurrent()) return;
        matches.push(...page.items);
        cursor = page.next_cursor ?? undefined;
      } while (cursor);
      if (!stillCurrent()) return;
      const choices: AnchorChoice[] = matches.map(field => ({ kind: "field", field }));
      if (!sectionOnly && view.run_id && (view.snapshot_id || view.mode === "working" && view.business_revision_id && view.translation_revision_id)) {
        // 每次来源查询从第一页独立累加，不混入阅读区已加载的旧译段。
        cursor = undefined;
        do {
          const translations = await reviewApi.translations(view.run_id, {
            snapshot_id: view.snapshot_id, business_revision_id: view.business_revision_id,
            translation_revision_id: view.translation_revision_id, document_id: file.source.document_id,
            parse_artifact_id: file.parse_artifact_id, source_segment_id: sourceId,
            source_anchor_id: sourceId ? undefined : id, cursor, limit: 200,
          });
          if (!stillCurrent()) return;
          choices.push(...translations.items.map(segment => ({ kind: "segment" as const, segment })));
          cursor = translations.next_cursor ?? undefined;
        } while (cursor);
      }
      if (!stillCurrent()) return;
      setAnchorLookup(null);
      // 点击原文优先看对应译文；一个字段和一个译段不是译文歧义。
      const translations = choices.filter(choice => choice.kind === "segment");
      const relevant = translations.length ? translations : choices;
      if (relevant.length > 1) { setAnchorChoices(relevant); return; }
      if (relevant[0]) { selectAnchorChoice(relevant[0]); return; }
      const issue = issuesQ.items.find(i => [i.a, i.b].some(side => side?.target.source_segment_id === id || side?.target.source_anchor_id === id || side?.target.section_id === id));
      if (issue) { const side = [issue.a, issue.b].find(v => v?.target.source_segment_id === id || v?.target.source_anchor_id === id || v?.target.section_id === id); if (side) locate(side.target, { issue: issue.issue_id }); }
    } catch (error) {
      if (stillCurrent()) setAnchorLookup({ id, loading: false, error: errorText(error) });
    }
  };
  useEffect(() => {
    if (selectedFieldId && !fieldsQ.items.some(f => f.field_id === selectedFieldId) && fieldsQ.hasNextPage && !fieldsQ.isFetching && !fieldsQ.error) void fieldsQ.fetchNextPage();
  }, [selectedFieldId, fieldsQ.items, fieldsQ.hasNextPage, fieldsQ.isFetching, fieldsQ.error, fieldsQ.fetchNextPage]);
  useEffect(() => {
    if (spec.intent !== "full" || spec.target || selectedIssueId) return;
    if (!run.data || run.data.workflow_type === "consignment_check") return;
    const first = issuesQ.items.find(i => i.severity === "blocking" && ["open", "unresolved"].includes(i.status));
    if (first?.a) { locate(first.a.target, { issue: first.issue_id }); setTab("bilingual"); }
    else if (issuesQ.hasNextPage && !issuesQ.isFetching && !issuesQ.error) void issuesQ.fetchNextPage();
  }, [spec.intent, spec.target, selectedIssueId, run.data?.workflow_type, issuesQ.items, issuesQ.hasNextPage, issuesQ.isFetching, issuesQ.error, issuesQ.fetchNextPage, locate]);
  const candidateResolution = navToken > 0 ? locatedResolution : resolved.data;
  const currentResolution = candidateResolution?.normalized_target.document_id === file?.source.document_id ? candidateResolution : null;
  const focusedSourceSpec = useRef<ReviewTargetSpec | null>(null);
  useEffect(() => {
    const target = resolved.data?.normalized_target;
    if (spec.intent !== "quick" || !spec.target || !target?.document_id || target.document_id !== file?.source.document_id
      || focusedSourceSpec.current === spec) return;
    focusedSourceSpec.current = spec;
    // 每次外部来源点击重新打开阅读区并发起定位；字段页内部定位不改变spec，仍保留字段页。
    setTab("bilingual"); setNarrowCol("source");
    locate(spec.target, { field: target.field_id ?? undefined, segment: target.translation_segment_id ?? undefined });
  }, [spec, resolved.data, file?.source.document_id, locate]);
  useEffect(() => {
    const value = resolved.data;
    if (!value || navToken > 0) return;
    setSelectedAnchor(value.normalized_target.source_segment_id ?? value.normalized_target.source_anchor_id ?? null);
    setTargetPage(value.page_no ?? null);
  }, [resolved.data, navToken]);
  const anchors: AnchorBox[] = useMemo(() => {
    if (!file) return [];
    const boxes: AnchorBox[] = [];
    issuesQ.items.forEach((i) => {
      [i.a, i.b].forEach((side) => {
        if (!side || side.target.document_id !== file.source.document_id || side.target.document_version !== file.source.document_version
            || side.target.parse_artifact_id !== file.parse_artifact_id) return;
        const id = side.target.source_segment_id ?? side.target.source_anchor_id ?? side.target.section_id;
        if (!id) return;
        const open = i.status === "open" || i.status === "unresolved";
        // 同一锚点可能同时命中多条规则：只保留最高风险的一个框，避免重复绘制
        const existing = boxes.find((b) => b.id === id);
        const rank = (t: AnchorBox["tone"]) => (t === "risk" ? 3 : t === "warn" ? 2 : t === "brand" ? 1 : 0);
        const tone: AnchorBox["tone"] = !open ? "neutral" : i.severity === "blocking" ? "risk" : i.severity === "warning" ? "warn" : "brand";
        const label = !open ? i.status === "accepted" ? "已接受提示" : i.status === "adjudicated" ? "已裁定" : i.status === "not_applicable" ? "已标记不适用" : i.status === "ignored" ? "已人工忽略" : i.status === "revised" ? "已修订留档" : "仍无法判断" : i.severity === "blocking" ? "高风险" : "待确认";
        if (existing) {
          if (rank(tone) > rank(existing.tone)) {
            existing.tone = tone;
            existing.label = label;
          }
          return;
        }
        // 疑点两侧各自使用固定来源的真实坐标，未选疑点也保留红黄框。
        const f2 = view?.files.find((x) => x.source.document_id === side.target.document_id);
        const ld = f2?.logical_documents.find((l) => l.logical_document_id === side.target.logical_document_id);
        const sec = ld?.sections.find((x) => x.section_id === side.target.section_id);
        const location = side.location;
        const rect = location?.precision === "rect" && location.coord_system === "page_normalized_top_left" && location.page_no
          ? location.bbox ?? undefined : undefined;
        boxes.push({ id, page_no: location?.page_no ?? pageOfSection(f2, side.target.section_id ?? undefined), rect, tone, label, where: [ld?.label, sec?.title].filter(Boolean).join(" · ") || undefined });
      });
    });
    if (currentResolution?.anchor_precision === "rect" && currentResolution.bbox && currentResolution.coord_system === "page_normalized_top_left"
        && currentResolution.normalized_target.document_id === file.source.document_id) {
      const id = currentResolution.normalized_target.source_segment_id ?? currentResolution.normalized_target.source_anchor_id ?? currentResolution.normalized_target.section_id ?? "selected-evidence";
      const existing = boxes.find(box => box.id === id);
      if (existing) {existing.rect = currentResolution.bbox;existing.page_no = currentResolution.page_no ?? 1;}
      else boxes.push({id,page_no:currentResolution.page_no ?? 1,rect:currentResolution.bbox,tone:"brand",label:"来源位置"});
    }
    return boxes;
  }, [file, issuesQ.items, view, currentResolution]);

  const requestClose = useCallback(() => {
    if (hasOpenForm) {
      setUnsaved(true);
      return;
    }
    onClose();
  }, [hasOpenForm, onClose]);

  // Esc 只在没有更上层表单／确认层时关闭工作台；有未提交输入先确认
  useEscLayer(!hasOpenForm && !checklistOpen && !exportOpen && !attemptId && !inputOpen && !unsaved, requestClose);

  // R01 未通过：停止加载敏感内容，只给不含原文的受限说明
  if (resolved.error) {
    return (
      <div className="flex h-full flex-col bg-card">
        <div className="flex h-bar items-center justify-between border-b border-line px-4">
          <span className="text-[14px] font-medium text-ink">校验区</span>
          <button type="button" className="btn btn-icon btn-ghost" onClick={onClose} aria-label="关闭">
            <Icon name="close" size={16} />
          </button>
        </div>
        <div className="p-4">
          <ErrorBar text={`${errorText(resolved.error)}。这个位置无法定位到有权查看的原件，已停止加载内容。`} />
          {onBack && (
            <button type="button" className="btn btn-secondary mt-3" onClick={onBack.onSelect}>
              {onBack.label}
            </button>
          )}
        </div>
      </div>
    );
  }
  if (!gateOpen || viewQ.isPending) {
    return (
      <div className="flex h-full items-center justify-center bg-card">
        <Loading text={gateOpen ? "正在打开校验区" : "正在校验这条来源的归属与版本"} />
      </div>
    );
  }
  if (viewQ.error || !view || !file) {
    return (
      <div className="flex h-full flex-col bg-card">
        <div className="flex h-bar items-center justify-between border-b border-line px-4">
          <span className="text-[14px] font-medium text-ink">校验区</span>
          <button type="button" className="btn btn-icon btn-ghost" onClick={onClose} aria-label="关闭">
            <Icon name="close" size={16} />
          </button>
        </div>
        <div className="p-4">
          <ErrorBar text="无法打开这份材料：可能没有权限，或对象已被删除。已停止加载内容。" action={{ label: "重试", onSelect: () => viewQ.refetch() }} />
        </div>
      </div>
    );
  }

  const canBilingual = width >= 760;
  const canThree = width - (treeOpen && fullscreen ? 200 : 0) >= 1296;
  const effectiveLayout: Layout = layout === "three" && !canThree ? "bilingual" : layout;

  return (
    <div ref={workspaceRef} className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-card">
      <Header view={view} runStatus={run.data?.status} fullscreen={fullscreen} onClose={requestClose} onExpand={onExpand} onBack={onBack} moreRef={moreRef} onMore={() => setMoreOpen(true)} onRecord={() => setTab("record")} savedAt={run.data?.updated_at} onToggleFullscreen={onToggleFullscreen} />
      <ActionBar
        view={view}
        run={run.data}
        editable={!!editable}
        hasOpenForm={hasOpenForm}
        onSaveDraft={submitOpenForm}
        onChecklist={() => { void run.refetch(); setChecklistOpen(true); }}
        onExport={() => { void run.refetch(); setExportOpen(true); }}
        onProgress={(id) => setAttemptId(id)}
      />
      <Menu
        open={moreOpen}
        onClose={() => setMoreOpen(false)}
        anchor={moreRef}
        label="校验区操作"
        items={[
          { label: "查看处理记录", icon: "history", onSelect: () => setTab("record") },
          { label: "确认源语言", icon: "source", onSelect: () => setConfirmLang(true), disabled: !editable || !!activeRequest, disabledReason: activeRequest ? "有待处理的必要确认，请先在任务卡中回答" : view.mode === "historical" ? "历史交付版本只读" : undefined },
          { label: "申请重识别", icon: "retry", onSelect: () => setReparse(true), disabled: !editable, disabledReason: view.mode === "historical" ? "历史交付版本只读" : undefined },
          { label: "上传补充材料", icon: "plus", onSelect: () => onUploadMore?.(view.case_id), disabled: !onUploadMore, disabledReason: onUploadMore ? undefined : "请在对话中上传，新文件会加入同一票" },
          { label: "替换此原件", icon: "docs", onSelect: () => setReplaceDoc(true), disabled: !editable, disabledReason: view.mode === "historical" ? "历史交付版本只读" : undefined, separatorBefore: true },
          ...(view.mode === "historical" ? [{ label: "切换当前工作版", icon: "history" as const, onSelect: () => setSwitchWorking(true), separatorBefore: true }] : []),
          { label: treeOpen ? "收起文件树" : "展开文件树", icon: "panel", onSelect: () => setTreeOpen((v) => !v) },
        ]}
      />

      {anchorLookup?.loading && <div className="border-b border-line px-4 py-2 text-sm text-ink-2" role="status">正在查找此来源关联的字段和译段…</div>}
      {anchorLookup?.error && <ErrorBar text={anchorLookup.error} action={{ label: "重试", onSelect: () => { void pickAnchor(anchorLookup.id); } }} />}
      {anchorChoices.length > 0 && <div className="flex max-h-48 shrink-0 flex-wrap gap-2 overflow-auto border-b border-line bg-card p-3" role="group" aria-label="选择此处字段或译段">
        <span className="text-ink-2">{anchorChoices.some(choice => choice.kind === "segment") ? "请选择要查看的译文：" : "请选择要查看的字段："}</span>{anchorChoices.map(choice => choice.kind === "field"
          ? <button key={`field:${choice.field.field_id}`} className="btn btn-secondary btn-sm" onClick={() => selectAnchorChoice(choice)}>字段 · {choice.field.name} · {choice.field.role}</button>
          : <button key={`segment:${choice.segment.translation_segment_id}`} className="btn btn-secondary btn-sm max-w-full whitespace-normal text-left" title={choice.segment.translated_text} onClick={() => selectAnchorChoice(choice)}>译段 · {choice.segment.section_title} · {choice.segment.translated_text.slice(0, 80)}{choice.segment.translated_text.length > 80 ? "…" : ""}</button>)}
        <button className="btn btn-ghost btn-sm" onClick={() => setAnchorChoices([])}>关闭</button>
      </div>}

      {view.mode === "historical" && (
        <div className="flex items-center gap-1.5 border-b border-line bg-hist-bg px-4 py-1.5 text-[12px] text-hist">
          <Icon name="history" size={14} />
          {view.snapshot_id ? "历史交付版本，只读。字段、疑点、译文与识别稿使用该次冻结的配对。" : "固定原件与识别版本，只读核验；需要编辑时请打开当前工作版。"}
        </div>
      )}
      {run.data?.status === "paused_reconcile" && (
        <div className="border-b border-line bg-warn-bg px-4 py-1.5 text-[12px] text-warn">
          <div className="flex items-center gap-1.5">
          <Icon name="shield" size={14} />
          外部请求待核查（{formatTime(run.data.updated_at)}）。{run.data.pause_explanation ?? ""}核查完成前这份处理保持只读。
          </div>
          {run.data.failure_reason && <p className="mt-1 break-words text-ink-2" lang="en">{englishError(run.data.failure_reason)}</p>}
        </div>
      )}
      {activeRequest && (
        <div className="flex flex-wrap items-center gap-2 border-b border-line bg-brand-soft px-4 py-2 text-[12px] text-brand-ink">
          <Icon name="question" size={14} />
          处理需要你确认一项信息后才能继续。
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setInputOpen(true)}>
            处理必要确认
          </button>
        </div>
      )}

      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line px-3 text-[12px] text-ink-2">
        {issueReturnPoint && !issuesDrawerOpen && <button className="btn btn-ghost btn-sm" onClick={returnToIssue}><Icon name="chevron-left" size={14} />返回疑点</button>}
        <button className="btn btn-ghost btn-sm" onClick={() => setIssuesDrawerOpen(v => !v)}>疑点清单</button>
        {selectedIssueId && (view.mode === "historical" || issuesQ.items.some(i => i.issue_id === selectedIssueId && ["open", "unresolved"].includes(i.status))) && <span className="min-w-0 flex-1 truncate">{issuesQ.items.find(i => i.issue_id === selectedIssueId)?.conclusion ?? "正在定位所选问题"}</span>}
        <label className="ml-auto">阅读字号 <select aria-label="阅读字号" value={fontSize} onChange={e => setFontSize(Number(e.target.value))}>{[14,15,16,17,18].map(n => <option key={n} value={n}>{n}px</option>)}</select></label>
      </div>
      {locateError && <div className="shrink-0 border-b border-line px-3 py-2"><ErrorBar text={`对应位置定位失败：${locateError}`} /></div>}
      {view.mode === "working" && view.files.some(item => item.preview_id) && ["queued","processing","paused_reconcile","failed"].includes(run.data?.status ?? "") && <div className="shrink-0 border-b border-line bg-warn-bg px-3 py-2 text-[12px] text-warn">{run.data?.current_stage ?? "正在更新译段与预览"} · 旧预览仅供参考，请等待本轮完成后核对新版本</div>}
      <TabsBar tab={tab} onTab={setTab} canBilingual={canBilingual} hasTranslation={!!view.translation_revision_id} segmentsOpen={segmentsOpen} onToggleSegments={tab === "bilingual" && view.translation_revision_id ? () => setSegmentsOpen((v) => !v) : undefined} />

      <div style={{ "--review-font-size": `${fontSize}px` } as React.CSSProperties} className="relative flex min-h-0 flex-1 overflow-hidden">
        {issuesDrawerOpen && <div data-review-issues-drawer className="absolute inset-y-0 left-0 z-pop flex w-[320px] max-w-full flex-col border-r border-line bg-card shadow-panel"><button className="btn btn-ghost btn-sm" onClick={() => setIssuesDrawerOpen(false)}>收起疑点</button><IssuesPanel view={view} onEdit={editIssue} editable={!!editable} selectedIssueId={selectedIssueId} filter={issueFilter} onFilter={setIssueFilter} restoreScroll={issueRestore} onLocate={(target,id) => locateIssueSource(target,id,true)} onDispose={(issue,kind) => setIssueAction({issue,kind})} onSupplement={issue => onUploadMore?.(view.case_id,{relatedIssueId:issue.issue_id,expectedIssueVersion:issue.issue_version})} canSupplement={!!onUploadMore} /></div>}
        {treeOpen && fullscreen && tab !== "comparison" && (
          <div className="flex min-h-0 w-[200px] shrink-0 flex-col border-r border-line">
            <FileTree
              view={view}
              selectedDocumentId={documentId}
              selectedSectionId={sectionId}
              onSelectFile={(id) => {
                clearLocation();
                setDocumentId(id);
                setSectionId(null);
              }}
              onSelectSection={(doc, _ld, sec) => {
                clearLocation();
                setDocumentId(doc);
                setSectionId(sec);
                setSelectedAnchor(sec);
                setNavToken((t) => t + 1);
                setTargetPage(pageOfSection(view.files.find(value=>value.source.document_id===doc),sec));
              }}
              onAssignSection={(sectionIdValue, suggestion) => setAssign({ sectionId: sectionIdValue, suggestion })}
              editable={!!editable}
            />
          </div>
        )}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {/* 侧板没有左树：文件与区段改为顶部下拉，仍可切换与确认归属 */}
          {!fullscreen && tab !== "comparison" && (
            <FileSelect
              view={view}
              documentId={documentId}
              sectionId={sectionId}
              editable={!!editable}
              onSelectFile={(id) => {
                clearLocation();
                setDocumentId(id);
                setSectionId(null);
              }}
              onSelectSection={(sec) => {
                clearLocation();
                setSectionId(sec);
                setSelectedAnchor(sec);
                setNavToken((t) => t + 1);
                setTargetPage(pageOfSection(file,sec));
              }}
              onAssignSection={(sectionIdValue, suggestion) => setAssign({ sectionId: sectionIdValue, suggestion })}
            />
          )}
          {tab === "comparison" && <ComparisonPanel view={view} run={run.data} width={width} onOpenRun={onOpenRun} onOpenIssue={id => { setSelectedIssueId(id); setTab("issues"); setIssueFilter("all"); }} onUploadMore={onUploadMore && view.mode !== "historical" ? () => onUploadMore(view.case_id) : undefined} />}
          {tab === "overview" && <Overview view={view} rulesStatus={run.data?.rules_status} onOpenIssues={() => setTab("issues")} onOpenBilingual={() => setTab("bilingual")} />}

          {tab === "bilingual" && (
            <div className="flex min-h-0 flex-1 flex-col">
              {/* 宽度不足时改为列切换，不隐藏译文 */}
              {!canBilingual && (
                <div className="flex shrink-0 items-center gap-1 border-b border-line px-3 py-1.5" role="group" aria-label="阅读列">
                  {(["source", "translation", "segments"] as const).map((c) => (
                    <button key={c} type="button" className={`btn btn-sm ${narrowCol === c ? "btn-secondary" : "btn-ghost"}`} onClick={() => setNarrowCol(c)} aria-pressed={narrowCol === c}>
                      {c === "source" ? "原件" : c === "translation" ? "译后版面" : "源译片段"}
                    </button>
                  ))}
                  <span className="flex-1" />
                  {onExpand && (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={onExpand}>
                      展开对照
                    </button>
                  )}
                </div>
              )}
              <div className={`flex min-h-0 flex-1 ${canBilingual ? "flex-row" : "flex-col"}`}>
                {(canBilingual || narrowCol === "source") && (
                  <div className={`flex min-h-0 min-w-0 flex-1 flex-col ${canBilingual ? "border-r border-line" : ""}`}>
                    <SourceViewer file={file} resolution={currentResolution} targetPage={spec.target || navToken > 0 ? targetPage : null} navigationToken={navToken} anchors={anchors} selectedAnchorId={selectedAnchor} onPickAnchor={pickAnchor} onLocate={locate} compact={!fullscreen} />
                  </div>
                )}
                {(canBilingual || narrowCol === "translation") && (
                  <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                    <PreviewViewer issues={issuesQ.items} view={view} file={file} selectedSegmentId={selectedSegmentId} navigationToken={navToken} onPickSegment={locateTranslation} />
                  </div>
                )}
                {!canBilingual && narrowCol === "segments" && (
                  <TranslationViewer
                    view={view}
                    documentId={documentId}
                    sectionId={sectionId}
                    editable={!!editable}
                    selectedSegmentId={selectedSegmentId}
                    navigationToken={navToken}
                    onSelect={(s) => {
                      setSelectedSegmentId(s.translation_segment_id);
                      setSelectedAnchor(s.source_segment_id ?? null);
                    }}
                    onLocateSource={(s) => locate(s.target, { segment: s.translation_segment_id })}
                    onRevise={setSegmentAction}
                  />
                )}
              </div>
            </div>
          )}

          {tab === "fields" && (
            <div className={`flex min-h-0 flex-1 ${canBilingual ? "flex-row" : "flex-col"}`}>
              <div className={`flex min-h-0 min-w-0 flex-1 flex-col ${!canBilingual && effectiveLayout === "fields_evidence" ? "basis-[45%]" : ""}`}>
                <FieldsPanel
                  view={view}
                  file={file}
                  editable={!!editable}
                  segments={segments}
                  selectedFieldId={selectedFieldId}
                  onLocateSource={(f) => { locate(f.target, { field: f.field_id }); setLayout("fields_evidence"); setFieldEvidencePane("source"); }}
                  onLocateTranslation={(f) => {
                    const sourceIds = new Set(f.source_segment_ids?.length ? f.source_segment_ids : [f.target.source_segment_id]);
                    const seg = segments.find(s => s.document_id === f.document_id && (s.source_segment_ids ?? [s.source_segment_id]).some(id => sourceIds.has(id)));
                    if (seg) {
                      locate(seg.target, { field: f.field_id, segment: seg.translation_segment_id });
                      setLayout("fields_evidence"); setFieldEvidencePane("translation");
                    }
                  }}
                  onAction={setFieldAction}
                />
              </div>
              {effectiveLayout === "fields_evidence" && (
                <div className={`flex min-h-0 min-w-0 flex-1 flex-col ${canBilingual ? "border-l" : "basis-[55%] border-t"} border-line`}>
                  <div className="flex shrink-0 items-center gap-1 border-b border-line px-2 py-1" role="group" aria-label="字段对应内容">
                    {(["source", "translation"] as const).map(pane => <button key={pane} type="button" className={`btn btn-sm ${fieldEvidencePane === pane ? "btn-secondary" : "btn-ghost"}`} aria-pressed={fieldEvidencePane === pane} onClick={() => setFieldEvidencePane(pane)}>{pane === "source" ? "原文" : "译文"}</button>)}
                  </div>
                  {fieldEvidencePane === "source" ? <SourceViewer file={file} resolution={currentResolution} targetPage={spec.target || navToken > 0 ? targetPage : null} navigationToken={navToken} anchors={anchors} selectedAnchorId={selectedAnchor} onPickAnchor={pickAnchor} onLocate={locate} compact /> : <PreviewViewer issues={issuesQ.items} view={view} file={file} selectedSegmentId={selectedSegmentId} navigationToken={navToken} onPickSegment={locateTranslation} />}
                </div>
              )}
            </div>
          )}

          {tab === "issues" && (
            <div className={`flex min-h-0 flex-1 ${canThree && effectiveLayout === "three" ? "flex-row" : "flex-col"}`}>
              <div data-review-issues-main className={`flex min-h-0 flex-col ${canThree && effectiveLayout === "three" ? "w-[320px] shrink-0 border-r border-line" : "flex-1"}`}>
                <IssuesPanel
                  view={view}
                  onEdit={editIssue}
                  editable={!!editable}
                  selectedIssueId={selectedIssueId}
                  filter={issueFilter}
                  onFilter={setIssueFilter}
                  documentFilter={docFilter}
                  onDocumentFilter={setDocFilter}
                  restoreScroll={issueRestore}
                  onLocate={(t, id) => locateIssueSource(t, id)}
                  onDispose={(issue, kind) => setIssueAction({ issue, kind })}
                  onSupplement={issue => onUploadMore?.(view.case_id, { relatedIssueId: issue.issue_id, expectedIssueVersion: issue.issue_version })}
                  canSupplement={!!onUploadMore}
                />
              </div>
              {canThree && effectiveLayout === "three" && (
                <>
                  <div className="flex min-h-0 min-w-0 flex-1 flex-col border-r border-line">
                    <SourceViewer file={file} resolution={currentResolution} targetPage={spec.target || navToken > 0 ? targetPage : null} navigationToken={navToken} anchors={anchors} selectedAnchorId={selectedAnchor} onPickAnchor={pickAnchor} onLocate={locate} />
                  </div>
                  <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                    <PreviewViewer issues={issuesQ.items} view={view} file={file} selectedSegmentId={selectedSegmentId} navigationToken={navToken} onPickSegment={locateTranslation} />
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "record" && <RecordView view={view} runId={run.data?.run_id ?? view.run_id} />}
        </div>

        {tab === "bilingual" && canBilingual && (segmentsOpen || (fullscreen && canThree && effectiveLayout === "three")) && (
          <div className="flex min-h-0 w-[320px] shrink-0 flex-col border-l border-line">
            <div className="flex h-11 shrink-0 items-center gap-1.5 border-b border-line px-2">
              <span className="flex-1 text-[13px] text-ink">源译片段</span>
              <button type="button" className="btn btn-icon btn-ghost h-8 w-8" onClick={() => setSegmentsOpen(false)} aria-label="收起源译片段">
                <Icon name="close" size={14} />
              </button>
            </div>
            <TranslationViewer
              view={view}
              documentId={documentId}
              sectionId={sectionId}
              editable={!!editable}
              selectedSegmentId={selectedSegmentId}
              navigationToken={navToken}
              onSelect={(s) => {
                setSelectedSegmentId(s.translation_segment_id);
                setSelectedAnchor(s.source_segment_id ?? null);
              }}
              onLocateSource={(s) => locate(s.target, { segment: s.translation_segment_id })}
              onRevise={setSegmentAction}
            />
          </div>
        )}
      </div>

      {fieldAction && <FieldForm key={`${fieldAction.field.field_id}:${fieldAction.kind}`} action={fieldAction} view={view} onClose={() => setFieldAction(null)} />}
      {issueAction && <IssueForm key={`${issueAction.issue.issue_id}:${issueAction.kind}`} issue={issueAction.issue} kind={issueAction.kind} view={view} onClose={() => setIssueAction(null)} onSaved={() => { setIssueAction(null); setSelectedIssueId(null); setIssueReturnPoint(null); setIssueRestore(undefined); setIssueFilter("open"); setTab("issues"); }} />}
      {segmentAction && <SegmentForm key={segmentAction.translation_segment_id} segment={segmentAction} view={view} onClose={() => setSegmentAction(null)} />}
      {assign && <SectionAssignForm view={view} sectionId={assign.sectionId} suggestion={assign.suggestion} onClose={() => setAssign(null)} />}
      {confirmLang && <ConfirmLanguageForm view={view} file={file} onClose={() => setConfirmLang(false)} />}
      {replaceDoc && (
        <ReplaceDocumentForm
          file={file}
          onClose={() => setReplaceDoc(false)}
          onGoUpload={(docId, ver) => {
            setReplaceDoc(false);
            onUploadMore?.(view.case_id, { replaceDocumentId: docId, expectedVersion: ver });
          }}
        />
      )}
      {switchWorking && (
        <SwitchToWorkingDialog
          onClose={() => setSwitchWorking(false)}
          onSwitch={() => {
            setSwitchWorking(false);
            onSwitchToWorking?.();
          }}
        />
      )}
      {reparse && <ReparseForm view={view} documentId={file.source.document_id} documentVersion={file.source.document_version} filename={file.source.filename} onClose={() => setReparse(false)} />}
      {checklistOpen && run.data && <ChecklistDialog view={view} run={run.data} onClose={() => setChecklistOpen(false)} onGoToTarget={(t) => locate(t)} />}
      {(checklistOpen || exportOpen) && !run.data && run.error && <div className="px-4 py-2"><ErrorBar text={errorText(run.error)} action={{ label: "重新读取任务", onSelect: () => void run.refetch() }} /></div>}
      {exportOpen && run.data && <ExportDialog view={view} run={run.data} onClose={() => setExportOpen(false)} onStarted={setAttemptId} />}
      {attemptId && run.data && <ExportProgress run={run.data} attemptId={attemptId} onClose={() => setAttemptId(null)} />}
      {inputOpen && activeRequest && <InputRequestForm key={`${activeRequest.input_request_id}:${activeRequest.request_version}`} request={activeRequest} view={view} onClose={() => setInputOpen(false)} onLocate={(t) => locate(t)} />}
      <UnsavedDialog
        open={unsaved}
        onKeep={() => setUnsaved(false)}
        onDiscard={() => {
          setUnsaved(false);
          setFieldAction(null);
          setIssueAction(null);
          setSegmentAction(null);
          setAssign(null);
          setReparse(false);
          setConfirmLang(false);
          setReplaceDoc(false);
          onClose();
        }}
      />
    </div>
  );
}

function Header({
  view,
  runStatus,
  fullscreen,
  onClose,
  onExpand,
  onBack,
  moreRef,
  onMore,
  onRecord,
  savedAt,
  onToggleFullscreen,
}: {
  view: ReviewView;
  runStatus?: string;
  fullscreen: boolean;
  savedAt?: string;
  onToggleFullscreen?: () => void;
  onClose: () => void;
  onExpand?: () => void;
  onBack?: { label: string; onSelect: () => void };
  moreRef: React.RefObject<HTMLButtonElement | null>;
  onMore: () => void;
  onRecord: () => void;
}) {
  const modeLabel = view.mode === "historical" ? "历史只读" : "仅原件";
  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="truncate text-[14px] font-medium text-ink">{view.case_name}</h2>
          {view.mode !== "working" && <StatusPill tone={view.mode === "historical" ? "hist" : "dis"} icon={view.mode === "historical" ? "history" : "info"}>
            {modeLabel}
          </StatusPill>}
          {runStatus && <StatusPill tone={runTone(runStatus).tone} icon={runTone(runStatus).icon} busy={runTone(runStatus).busy}>{runTone(runStatus).text}</StatusPill>}
        </div>
        <p className="mt-0.5 truncate text-[11px] text-ink-3">
          {savedAt ? `最近保存 ${formatTime(savedAt)}` : view.snapshot_exported_at ? `最近保存 ${formatTime(view.snapshot_exported_at)}` : ""}
        </p>
      </div>
      {onBack && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack.onSelect}>
          <Icon name="chevron-left" size={14} />
          {onBack.label}
        </button>
      )}
      <button type="button" className="btn btn-ghost btn-sm" onClick={onRecord}>
        查看记录
      </button>
      {!fullscreen && onExpand && (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onExpand}>
          展开双语对照
        </button>
      )}
      {onToggleFullscreen && (
        <button type="button" className="btn btn-icon btn-ghost" onClick={onToggleFullscreen} aria-label={fullscreen ? "收起为侧边面板" : "全屏显示"}>
          <Icon name={fullscreen ? "collapse" : "expand"} size={16} />
        </button>
      )}
      <button ref={moreRef} type="button" className="btn btn-icon btn-ghost" onClick={onMore} aria-label="更多操作">
        <Icon name="more" size={18} />
      </button>
      <button type="button" className="btn btn-icon btn-ghost" onClick={onClose} aria-label="关闭校验区">
        <Icon name="close" size={18} />
      </button>
    </header>
  );
}

function TabsBar({
  tab,
  onTab,
  canBilingual,
  hasTranslation,
  segmentsOpen,
  onToggleSegments,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  canBilingual: boolean;
  hasTranslation: boolean;
  segmentsOpen?: boolean;
  onToggleSegments?: () => void;
}) {
  const tabs: { key: Tab; label: string }[] = [
    { key: "overview", label: "总览" },
    { key: "bilingual", label: hasTranslation ? "原文与译文" : "原件阅读" },
    { key: "fields", label: "字段" },
    { key: "comparison", label: "同票对比" },
    { key: "issues", label: "疑点" },
    { key: "record", label: "处理记录" },
  ];
  return (
    <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-3">
      <div className="flex min-w-0 flex-1 gap-0.5 overflow-x-auto scroll-thin" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`relative shrink-0 px-2.5 py-2 text-[13px] ${tab === t.key ? "font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
            onClick={() => onTab(t.key)}
          >
            {t.label}
            {tab === t.key && <span className="absolute inset-x-1.5 -bottom-px h-0.5 rounded bg-brand" />}
          </button>
        ))}
      </div>
      {onToggleSegments && (
        <button type="button" className={`btn btn-sm shrink-0 ${segmentsOpen ? "btn-secondary" : "btn-ghost"}`} onClick={onToggleSegments} aria-pressed={segmentsOpen} disabled={!canBilingual} title={canBilingual ? "查看源译片段并修订译文" : "需要至少 760 px 宽度"}>
          源译片段
        </button>
      )}
    </div>
  );
}

/** 侧板文件／区段切换：与全屏文件树共用当前逻辑单据的类型／货类入口。 */
function FileSelect({
  view,
  documentId,
  sectionId,
  editable,
  onSelectFile,
  onSelectSection,
  onAssignSection,
}: {
  view: ReviewView;
  documentId: string | null;
  sectionId: string | null;
  editable: boolean;
  onSelectFile: (id: string) => void;
  onSelectSection: (sectionId: string) => void;
  onAssignSection: (sectionId: string, suggestion: string) => void;
}) {
  const file = view.files.find((f) => f.source.document_id === documentId) ?? view.files[0];
  const logical = file?.logical_documents.find(item => item.sections.some(section => section.section_id === sectionId)) ?? file?.logical_documents.find(item => item.sections.length > 0);
  const assignSection = logical?.sections.find(section => section.section_id === sectionId)?.section_id ?? logical?.sections[0]?.section_id;
  return (
    <div className="shrink-0 border-b border-line bg-page px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex min-w-0 flex-1 items-center gap-1.5">
          <span className="shrink-0 text-[12px] text-ink-3">原件</span>
          <select className="input h-8 min-w-0 flex-1 text-[13px]" value={file?.source.document_id ?? ""} onChange={(e) => onSelectFile(e.target.value)}>
            {view.files.map((f) => (
              <option key={f.source.document_id} value={f.source.document_id}>
                {f.source.filename}
                {f.issue_counts.blocking ? `（${f.issue_counts.blocking} 红）` : f.issue_counts.warning ? `（${f.issue_counts.warning} 黄）` : ""}
              </option>
            ))}
          </select>
        </label>
        {file && file.logical_documents.length > 0 && (
          <label className="flex min-w-0 flex-1 items-center gap-1.5">
            <span className="shrink-0 text-[12px] text-ink-3">区段</span>
            <select className="input h-8 min-w-0 flex-1 text-[13px]" value={sectionId ?? ""} onChange={(e) => onSelectSection(e.target.value)}>
              <option value="">全部区段</option>
              {file.logical_documents.flatMap((l) =>
                l.sections.map((s) => (
                  <option key={`${l.logical_document_id}-${s.section_id}`} value={s.section_id}>
                    {l.label} · {s.title}
                  </option>
                )),
              )}
            </select>
          </label>
        )}
      </div>
      {logical && (logical.needs_confirmation || (editable && assignSection)) && (
        <div className={`mt-1.5 flex flex-wrap items-center gap-2 text-[12px] ${logical.needs_confirmation ? "text-warn" : "text-ink-2"}`}>
          {logical.needs_confirmation && <><Icon name="warn" size={14} />「{logical.label}」的单据类型待确认</>}
          {editable && assignSection && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onAssignSection(assignSection, logical.label)}>
            {logical.needs_confirmation ? "确认类型或区段" : "调整类型与货物领域"}
          </button>}
        </div>
      )}
    </div>
  );
}

function Overview({ view, rulesStatus, onOpenIssues, onOpenBilingual }: { view: ReviewView; rulesStatus?: "run" | "not_run" | null; onOpenIssues: () => void; onOpenBilingual: () => void }) {
  const snap = useSnapshot(view.snapshot_id);
  return (
    <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
      <div className="card p-4">
        <h3 className="text-[14px] font-medium text-ink">这一票的当前状态</h3>
        <p className="mt-1.5 text-[13px] leading-relaxed text-ink-2">{view.gate_summary}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenIssues}>
            查看疑点
          </button>
          {view.run_id && view.mode !== "source_only" && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenBilingual}>
              查看原文与译文
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {view.files.map((f) => (
          <div key={f.source.document_id} className="card p-3">
            <p className="truncate text-[13px] font-medium text-ink" title={f.source.filename}>
              {f.source.filename}
            </p>
            <p className="mt-0.5 text-[11px] text-ink-3">
              原件 v{f.source.document_version} · {f.page_count} 页 · {f.logical_documents.map((l) => l.label).join("、")}
            </p>
            <div className="mt-2">
              <IssueCounts rulesStatus={view.snapshot_id ? undefined : !view.business_revision_id ? "not_run" : rulesStatus} blocking={f.issue_counts.blocking} warning={f.issue_counts.warning} undetermined={f.issue_counts.undetermined} />
            </div>
          </div>
        ))}
      </div>

      {snap.data && (
        <div className="card mt-3 p-4">
          <h3 className="mb-2 text-[14px] font-medium text-ink">这次交付的文件</h3>
          <SnapshotFiles snapshot={snap.data} />
        </div>
      )}
    </div>
  );
}

function RecordView({ view, runId }: { view: ReviewView; runId?: string | null }) {
  const q = useTimeline(runId, true);
  return (
    <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
      <h3 className="mb-2 text-[14px] font-medium text-ink">处理记录</h3>
      {q.isPending && <Loading />}
      {!q.isPending && !q.data?.items.length && <p className="text-[13px] text-ink-2">这一票还没有处理记录。</p>}
      <ol className="space-y-2">
        {(q.data?.items ?? []).map((e) => (
          <li key={e.seq} className="flex gap-2.5">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${e.kind === "failure" ? "bg-risk" : e.kind === "human" ? "bg-brand" : e.kind === "export" ? "bg-ok" : e.kind === "ops" ? "bg-warn" : "bg-line"}`} aria-hidden="true" />
            <div className="min-w-0 flex-1 border-b border-line pb-2">
              <p className="text-[13px] text-ink">{e.message}</p>
              <p className="mt-0.5 text-[11px] text-ink-3">
                {formatTime(e.occurred_at)} · {e.actor}
              </p>
            </div>
          </li>
        ))}
      </ol>
      <div className="mt-4">
        <ChecksList view={view} />
      </div>
    </div>
  );
}

function ActionBar({
  view,
  run,
  editable,
  hasOpenForm,
  onSaveDraft,
  onChecklist,
  onExport,
  onProgress,
}: {
  view: ReviewView;
  run?: import("../../api/types").RunView;
  editable: boolean;
  hasOpenForm: boolean;
  onSaveDraft: () => void;
  onChecklist: () => void;
  onExport: () => void;
  onProgress: (id: string) => void;
}) {
  const confirmReason =
    view.disabled_reasons.confirm_review ?? (!editable ? (view.mode === "historical" ? "历史交付版本只读" : "这份材料还没有可复核的译本") : undefined);
  const exportReason = view.disabled_reasons.export ?? (view.mode === "historical" ? "历史交付版本只读" : undefined);
  const canConfirm = view.mode === "working" && view.allowed_actions.includes("confirm_review");
  const canExport = view.mode === "working" && view.allowed_actions.includes("export");
  const failedAttempt = run?.latest_export_attempt_status === "failed_retryable";
  // 只读识别结果与仅原件视图没有译本，按设计不出现确认／导出按钮（而非以禁用态出现）
  const translationFlow = (view.mode === "historical" && !!view.snapshot_id) || (!!view.run_id && view.mode === "working" && run?.workflow_type === "translate_review");

  return (
    <div role="group" aria-label="复核操作" className="shrink-0 border-b border-line bg-card px-4 py-1">
      <div className="flex min-h-8 flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1 text-[12px] text-ink-2">
          {translationFlow && !canExport && exportReason && <span className="text-ink-3">{exportReason}</span>}
          {view.issue_counts.blocking > 0 && <span className="ml-2 text-risk">{view.issue_counts.blocking} 个红项阻断交付</span>}
          {run?.confirmation_status === "valid" && (
            <span className="ml-2 text-ok">已完成复核</span>
          )}
          {run?.confirmation_status === "stale" && <span className="ml-2 text-warn">确认已失效，需重新复核</span>}
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onSaveDraft} disabled={!hasOpenForm || view.mode === "historical"} title={view.mode === "historical" ? "历史交付版本只读" : hasOpenForm ? undefined : "先选择字段、疑点或译段进行修订"}>
          保存草稿
        </button>
        {translationFlow && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onChecklist} disabled={!canConfirm} title={confirmReason}>
            复核完成
          </button>
        )}
        {!translationFlow && <span className="text-[12px] text-ink-3">{run?.workflow_type === "consignment_check" ? "本次只做同票核对，不产生译本" : view.mode === "source_only" ? "这份材料还没有可复核的译本" : "本次只做识别与索引，没有译本需要确认或导出"}</span>}
        {translationFlow && failedAttempt && run?.latest_export_attempt_id ? (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => onProgress(run.latest_export_attempt_id!)}>
            查看导出失败原因
          </button>
        ) : translationFlow ? (
          <button type="button" className="btn btn-primary btn-sm" onClick={onExport} disabled={!canExport} title={exportReason}>
            导出
          </button>
        ) : null}
      </div>

    </div>
  );
}

export type { FieldView, IssueView };
