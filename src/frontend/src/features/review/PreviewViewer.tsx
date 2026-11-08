import { languageName, languageCode } from "../../i18n/languages";
// 译后版面（R06）：默认受控 reading_html，仅 manifest 标可用时提供 draft_pdf。
// HTML 装入不开脚本、表单与顶层跳转的 iframe；React 不接收 HTML 字符串。
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchContent, downloadContent } from "../../api/content";
import { contentUrls } from "../../api/review";
import type { PreviewManifest, ReviewFileNode, ReviewView, TranslationScope, IssueView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { useSession } from "../../stores/session";
import { useReviewPreference } from "../../stores/reviewPreference";
import { ErrorBar, Loading, StatusPill } from "../../components/Status";
import { usePreviewManifest } from "./hooks";

const scopeText: Record<TranslationScope, string> = { all: "全文翻译", goods_description: "仅翻译货物描述", selected_sections: "仅翻译所选区段" };

export function PreviewViewer({
  view,
  file,
  onPickSegment,
  selectedSegmentId,
  navigationToken = 0,
  issues = [],
}: {
  view: ReviewView;
  issues?: IssueView[];
  file: ReviewFileNode;
  onPickSegment?: (segmentId: string) => void;
  selectedSegmentId?: string | null;
  navigationToken?: number;
}) {
  const manifest = usePreviewManifest(file.preview_id);
  const [rep, setRep] = useState<"reading_html" | "bilingual" | "draft_pdf">("reading_html");
  useEffect(() => { if (navigationToken > 0 && selectedSegmentId) setRep("reading_html"); }, [navigationToken, selectedSegmentId]);

  if (view.mode === "source_only" || !view.run_id) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-center">
        <p className="max-w-[320px] text-[13px] text-ink-2">{file.parse_status !== "parsed" ? "这份原件尚未解析，尚无译文。" : "这份材料尚无译文。"}发起翻译任务后才会生成译稿。</p>
      </div>
    );
  }
  if (!file.preview_id) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <p className="text-[13px] text-ink-2">该原件尚无译后版面，译稿生成后可在此查看。</p>
      </div>
    );
  }
  if (manifest.isPending) {
    return <div className="flex min-h-0 flex-1 items-center justify-center"><Loading text="正在读取译后版面" /></div>;
  }
  if (manifest.error || !manifest.data) {
    return (
      <div className="p-4">
        <ErrorBar text="译后版面读取失败，复核确认前必须能打开这份译稿。" action={{ label: "重试", onSelect: () => manifest.refetch() }} />
      </div>
    );
  }

  const m = manifest.data;
  // manifest 必须与当前所选目标同一份原件与配对，不按同名文件复用预览
  const mismatch = m.source_version.document_id !== file.source.document_id || m.source_version.document_version !== file.source.document_version;
  if (mismatch) {
    return (
      <div className="p-4">
        <ErrorBar text="该预览与当前所选原件不是同一版本，已停止显示以免张冠李戴。" />
      </div>
    );
  }
  const html = m.representations.find((r) => r.kind === "reading_html");
  const pdf = m.representations.find((r) => r.kind === "draft_pdf");
  const effective = rep === "bilingual" ? "bilingual" : rep === "draft_pdf" && pdf?.available ? "draft_pdf" : "reading_html";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PreviewToolbar view={view} manifest={m} rep={effective} onRep={setRep} htmlAvailable={!!html?.available} pdfAvailable={!!pdf?.available} pdfReason={pdf?.disabled_reason ?? undefined} />
      {m.translation_scope !== "all" && (
        <ScopeBar scope={m.translation_scope} view={view} manifest={m} />
      )}
      {m.status !== "valid" && (
        <div className="border-b border-line bg-hist-bg px-3 py-1.5 text-[12px] text-hist">
          <Icon name="history" size={14} className="mr-1 inline align-[-2px]" />
          {m.status === "stale" ? "该预览已过期：字段或译文已被修订，等待重算后才能导出。" : `历史只读预览 · 预览版 ${m.preview_version}`}
        </div>
      )}
      <div className="min-h-0 flex-1 bg-canvas">
        {effective !== "draft_pdf" ? (
          html?.available ? (
            <ReadingHtml mode={effective === "bilingual" ? "bilingual" : "translation"} key={`${m.preview_id}:${effective}`} previewId={m.preview_id} onPickSegment={onPickSegment} selectedSegmentId={selectedSegmentId} navigationToken={navigationToken} stale={m.status !== "valid"} highlights={issues.filter(issue => issue.status === "open" || issue.status === "unresolved").flatMap(issue => [issue.a,issue.b].filter(side => side?.target.document_id === file.source.document_id && side.target.translation_segment_id).map(side => ({id:side!.target.translation_segment_id!,tone:issue.severity === "blocking" ? "risk" : "warn",label:issue.conclusion})))} />
          ) : (
            <div className="p-4">
              <ErrorBar text={html?.disabled_reason ?? "这份原件的双语译后版面尚未生成，复核确认会因此被阻断。"} />
            </div>
          )
        ) : (
          <DraftPdf previewId={m.preview_id} />
        )}
      </div>
    </div>
  );
}

function ScopeBar({ scope, view, manifest }: { scope: TranslationScope; view: ReviewView; manifest: PreviewManifest }) {
  const [open, setOpen] = useState(false);
  const openEvidence = useSession(state => state.openEvidence);
  return (
    <div className="border-b border-line bg-hist-bg px-3 py-1.5 text-[12px] text-hist">
      <button type="button" className="inline-flex items-center gap-1" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Icon name="info" size={14} />
        {scopeText[scope]}
        <Icon name={open ? "chevron-down" : "chevron-right"} size={12} />
      </button>
      {open && <div className="mt-1 leading-relaxed">
        <p>范围来自此原件的冻结版本；未译区段仅在原文视图显示，正式导出仅包含译文。</p>
        {([{ label: "已选翻译区段", refs: manifest.selected_section_refs }, { label: "保留原文区段", refs: manifest.untranslated_section_refs }]).map(group => <details key={group.label} open>
          <summary>{group.label}（{group.refs.length}）</summary>
          {group.refs.map(target => {
            const file = view.files.find(item => item.source.document_id === target.document_id);
            const logical = file?.logical_documents.find(item => item.logical_document_id === target.logical_document_id);
            const section = logical?.sections.find(item => item.section_id === target.section_id);
            return <button key={target.section_id} className="btn btn-link block text-left whitespace-normal" onClick={() => openEvidence({ ...target, run_id: view.run_id ?? undefined, export_snapshot_id: view.snapshot_id ?? undefined, business_revision_id: manifest.business_revision_id }, "view_source")}>{file?.source.filename ?? "固定原件"} · v{target.document_version} / {logical?.label ?? "逻辑单据"} / {section?.title ?? "固定区段"}</button>;
          })}
          {!group.refs.length && <p>无</p>}
        </details>)}
      </div>}
    </div>
  );
}

function PreviewToolbar({
  view,
  manifest,
  rep,
  onRep,
  htmlAvailable,
  pdfAvailable,
  pdfReason,
}: {
  view: ReviewView;
  manifest: PreviewManifest;
  rep: "reading_html" | "bilingual" | "draft_pdf";
  onRep: (r: "reading_html" | "bilingual" | "draft_pdf") => void;
  htmlAvailable: boolean;
  pdfAvailable: boolean;
  pdfReason?: string;
}) {
  const [downloading, setDownloading] = useState(false);
  const stateLabel = view.mode === "historical" ? "历史" : manifest.status === "stale" ? "待重算" : view.run_status === "exported" ? "有效" : "待确认草稿";
  const tone = view.mode === "historical" ? "hist" : manifest.status === "stale" ? "warn" : view.run_status === "exported" ? "ok" : "warn";
  return (
    <div className="flex min-h-11 shrink-0 flex-wrap items-center gap-1.5 border-b border-line bg-card px-2">
      <span className="text-[13px] text-ink">{`${languageName(view.target_language)}译稿`}</span>
      <StatusPill tone={tone as "hist" | "warn" | "ok"} icon={tone === "ok" ? "check" : tone === "hist" ? "history" : "clock"}>
        {stateLabel}
      </StatusPill>

      <span className="flex-1" />
      <div className="flex items-center rounded-ctl border border-line p-0.5" role="group" aria-label="译文视图">
        <button type="button" className={`btn btn-sm h-7 ${rep === "reading_html" ? "btn-secondary" : "btn-ghost"}`} onClick={() => onRep("reading_html")} disabled={!htmlAvailable}>
          纯译文
        </button>
        <button type="button" className={`btn btn-sm h-7 ${rep === "bilingual" ? "btn-secondary" : "btn-ghost"}`} onClick={() => onRep("bilingual")} disabled={!htmlAvailable}>双语对照</button>
        <button type="button" className={`btn btn-sm h-7 ${rep === "draft_pdf" ? "btn-secondary" : "btn-ghost"}`} onClick={() => onRep("draft_pdf")} disabled={!pdfAvailable} title={pdfAvailable ? undefined : pdfReason ?? "草稿 PDF 尚未生成"}>
          导出预览
        </button>
      </div>
      {manifest.allowed_actions.includes("download_draft") && (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={!pdfAvailable || downloading}
          title={pdfAvailable ? "该文件带“待确认／草稿”标记，供讨论使用，不进入正式交付列表" : pdfReason ?? "草稿 PDF 尚未生成"}
          onClick={async () => {
            setDownloading(true);
            try {
              await downloadContent(contentUrls.preview(manifest.preview_id, "draft_pdf"), `${manifest.source_version.filename}_待确认草稿.pdf`);
            } finally {
              setDownloading(false);
            }
          }}
        >
          <Icon name="download" size={14} />
          下载草稿
        </button>
      )}
    </div>
  );
}

/** 受控 HTML：不开脚本/表单/顶层跳转；只允许 manifest 列出的锚点触发来源定位。 */
export function ReadingHtml({ previewId, onPickSegment, selectedSegmentId, navigationToken = 0, stale, mode = "translation", highlights = [], highlightDuration }: { highlightDuration?: number; highlights?: {id:string;tone:string;label:string}[]; mode?: "translation" | "source" | "bilingual"; previewId: string; onPickSegment?: (id: string) => void; selectedSegmentId?: string | null; navigationToken?: number; stale: boolean }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const [frameVersion, setFrameVersion] = useState(0);
  const [saved, setSaved] = useReviewPreference(`preview:${previewId}:${mode}`, { scrollTop: 0, following: true });
  const restore = useRef(saved);
  const pickSegment = useRef(onPickSegment);
  pickSegment.current = onPickSegment;
  const [following, setFollowing] = useState(saved.following);
  const lastSelection = useRef(selectedSegmentId);
  useEffect(() => { if (lastSelection.current === selectedSegmentId) return; lastSelection.current = selectedSegmentId; setFollowing(true); }, [selectedSegmentId]);
  useEffect(() => { if (navigationToken > 0) setFollowing(true); }, [navigationToken]);

  const styled = useMemo(
    () => `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">`,
    [],
  );

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    setError(false);
    setBlobUrl(null);
    fetchContent(contentUrls.preview(previewId, "reading_html", mode))
      .then(async (b) => {
        if (cancelled) return;
        const text = await b.text();
        const doc = `${styled}<style>
          body{margin:0;padding:20px 24px;background:#fff;color:#111418;font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}
          section.document-page{margin:0 0 24px;min-height:800px;border-bottom:1px solid #e4e7eb;padding-bottom:24px;break-after:page}
          table{width:100%;border-collapse:collapse;table-layout:fixed;margin:12px 0}
          td,th{border:1px solid #4a525c;padding:4px 8px;vertical-align:top;overflow-wrap:anywhere}
          hr{border:0;border-top:1px solid #4a525c;margin:12px 0}
          p{white-space:pre-wrap;margin:6px 0}
          .risk{outline:1px solid #b42318;background:#fff7f7}.warn{outline:1px solid #b7791f;background:#fffaf0}
          [id^="source-"]{cursor:pointer}
          h3{margin:0 0 8px;font-size:13px;color:#4a525c;font-weight:600;border-bottom:1px solid #e4e7eb;padding-bottom:4px}
          .seg{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:6px 8px;border-radius:6px;margin:0 -8px}
          p[id^="translation-"]{cursor:pointer;padding:8px;border-radius:6px}
          p[id^="translation-"].sel{background:#edf3fe;outline:2px solid #1f63d6}
          .seg:hover{background:#f3f4f6}
          .seg.sel{background:#edf3fe;outline:2px solid #1f63d6}
          .src{margin:0;color:#4a525c}
          .tgt{margin:0;color:#111418}
          [id].sel{outline:2px solid #1f63d6 !important;outline-offset:2px;background:#edf3fe !important;box-shadow:0 0 0 3px rgba(31,99,214,.12)}
          ${stale ? ".seg{opacity:.75}" : ""}
        </style>${text}`;
        url = URL.createObjectURL(new Blob([doc], { type: "text/html" }));
        setBlobUrl(url);
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [previewId, styled, stale, mode]);

  useEffect(() => {
    const el = frame.current;
    if (!el || !blobUrl) return;
    let detach: (() => void) | undefined;
    const onLoad = () => {
      detach?.();
      const doc = el.contentDocument;
      if (!doc) return;
      doc.body.style.fontSize = getComputedStyle(el).getPropertyValue("--review-font-size") || "14px";
      const scroller = doc.scrollingElement;
      if (scroller) scroller.scrollTop = restore.current.scrollTop;
      const click = (e: Event) => {
        const target = e.target as HTMLElement | null;
        if (target?.closest("a")) e.preventDefault();
        const seg = target?.closest(`[id^="${mode === "source" ? "source" : "translation"}-"]`);
        if (seg) pickSegment.current?.(seg.id.slice(mode === "source" ? "source-".length : "translation-".length));
      };
      const stopFollowing = () => setFollowing(false);
      const saveScroll = () => setSaved(previous => ({ ...previous, scrollTop: doc.scrollingElement?.scrollTop ?? 0 }));
      doc.addEventListener("click", click);
      doc.addEventListener("wheel", stopFollowing, { passive: true });
      doc.addEventListener("touchmove", stopFollowing, { passive: true });
      doc.addEventListener("scroll", saveScroll, { passive: true });
      detach = () => {
        doc.removeEventListener("click", click);
        doc.removeEventListener("wheel", stopFollowing);
        doc.removeEventListener("touchmove", stopFollowing);
        doc.removeEventListener("scroll", saveScroll);
      };
      setFrameVersion(old => old + 1);
    };
    el.addEventListener("load", onLoad);
    return () => { el.removeEventListener("load", onLoad); detach?.(); };
  }, [blobUrl, setSaved, mode]);
  useEffect(() => { setSaved(previous => ({ ...previous, following })); }, [following, setSaved]);

  useEffect(() => {
    const doc = frame.current?.contentDocument;
    if (!doc) return;
    doc.querySelectorAll(".sel").forEach((n) => n.classList.remove("sel"));
    if (!selectedSegmentId) return;
    const prefix = mode === "source" ? "source" : "translation";
    const el = doc.getElementById(`${prefix}-${selectedSegmentId}`) ?? [...doc.querySelectorAll<HTMLElement>(`[data-${prefix}-segment-ids]`)].find(element => element.getAttribute(`data-${prefix}-segment-ids`)?.split(/\s+/).includes(selectedSegmentId));
    el?.classList.add("sel");
    if (following) el?.scrollIntoView({ block: "center", behavior: document.documentElement.dataset.motion==="reduce"?"auto":"smooth" });
    if (highlightDuration && el) { const timer = window.setTimeout(() => el.classList.remove("sel"), highlightDuration); return () => window.clearTimeout(timer); }
  }, [selectedSegmentId, navigationToken, blobUrl, frameVersion, following, mode, highlightDuration]);

  const highlightKey = JSON.stringify(highlights);
  useEffect(() => {
    const doc = frame.current?.contentDocument;
    if (!doc) return;
    doc.querySelectorAll(".risk,.warn").forEach(node => {node.classList.remove("risk","warn");node.removeAttribute("title");});
    for (const marker of highlights) {
      const prefix = mode === "source" ? "source" : "translation";
      const element=doc.getElementById(`${prefix}-${marker.id}`) ?? [...doc.querySelectorAll<HTMLElement>(`[data-${prefix}-segment-ids]`)].find(node => node.getAttribute(`data-${prefix}-segment-ids`)?.split(/\s+/).includes(marker.id));
      if (element) { element.classList.add(marker.tone === "risk" ? "risk" : "warn");element.title=marker.label; }
    }
  }, [highlightKey, blobUrl, frameVersion, mode]);

  useEffect(() => { const parent = frame.current?.closest("[style]"); if (!parent) return; const update = () => { const element = frame.current; if (element?.contentDocument?.body) element.contentDocument.body.style.fontSize = getComputedStyle(element).getPropertyValue("--review-font-size") || "14px"; }; const observer = new MutationObserver(update); observer.observe(parent, {attributes: true, attributeFilter: ["style"]}); update(); return () => observer.disconnect(); }, [blobUrl, frameVersion]);

  if (error) return <div className="p-4"><ErrorBar text="译后版面读取失败，复核确认会因此被阻断。" /></div>;
  if (!blobUrl) return <div className="flex h-full items-center justify-center"><Loading text="正在加载译后版面" /></div>;
  return <iframe ref={frame} src={blobUrl} title={mode === "source" ? "识别原文版面" : mode === "bilingual" ? "双语对照版面" : "纯译文版面"} className="h-full w-full border-0 bg-white" sandbox="allow-same-origin" referrerPolicy="no-referrer" />;
}

function DraftPdf({ previewId }: { previewId: string }) {
  const [href, setHref] = useState<string | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    setHref(null);
    setError(false);
    fetchContent(contentUrls.preview(previewId, "draft_pdf"))
      .then((b) => {
        if (cancelled) return;
        url = URL.createObjectURL(b);
        setHref(url);
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [previewId]);
  if (error) return <div className="p-4"><ErrorBar text="草稿 PDF 读取失败，可改用译后版面继续校验。" /></div>;
  if (!href) return <div className="flex h-full items-center justify-center"><Loading text="正在加载草稿 PDF" /></div>;
  return <iframe src={href} title="待确认草稿 PDF" className="h-full w-full border-0" />;
}
