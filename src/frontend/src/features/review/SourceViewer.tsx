import type { ApiResponses } from "../../api/generated/request-contracts";
// 原件阅读区：PDF.js 按可见页渲染、图片直读、DOCX 走受控段表。
// 锚点矩形只在 anchor_precision=rect 时绘制，其余只给页/段说明，不画猜测框。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api } from "../../api/client";
import { contentUrls } from "../../api/review";
import { downloadContent, fetchContent } from "../../api/content";
import type { EvidenceResolution, EvidenceTarget, ReviewFileNode, SourceVersion } from "../../api/types";
import { ReadingHtml } from "./PreviewViewer";
import { useReviewPreference } from "../../stores/reviewPreference";
import { Icon } from "../../components/Icon";
import { ErrorBar, Loading } from "../../components/Status";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export interface AnchorBox {
  page_no: number;
  /**
   * 归一化矩形，仅使用服务端核验的坐标。
   */
  rect?: [number, number, number, number];
  tone: "risk" | "warn" | "ok" | "brand" | "neutral";
  label: string;
  /** 该风险涉及的区段标题，供无坐标时说明位置 */
  where?: string;
  id: string;
}

const ZOOMS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3];

interface Props {
  file: ReviewFileNode;
  resolution?: EvidenceResolution | null;
  /** 需要定位的页；由来源点击驱动，用户手动滚动后不再跟随。 */
  targetPage?: number | null;
  navigationToken?: number;
  anchors?: AnchorBox[];
  selectedAnchorId?: string | null;
  onPickAnchor?: (id: string) => void;
  compact?: boolean;
  highlightDuration?: number;
  onLocate?: (target: EvidenceTarget) => void;
}

export function SourceViewer(props: Props) {
  const [original,setOriginal]=useState(false);
  const file=props.file;
  if (!file.preview_id) return <OriginalSourceViewer {...props}/>;
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-card p-2">
      <button type="button" className={`btn btn-sm ${!original?"btn-secondary":"btn-ghost"}`} onClick={()=>setOriginal(false)}>识别原文</button>
      <button type="button" className={`btn btn-sm ${original?"btn-secondary":"btn-ghost"}`} onClick={()=>setOriginal(true)}>原始文件</button>
      {!original&&<span className="text-[11px] text-ink-3">保留表格、横线和源分页；扫描细节请切换原始文件核对</span>}
    </div>
    {original?<OriginalSourceViewer {...props}/>:<div className="min-h-0 flex-1"><ReadingHtml highlightDuration={props.highlightDuration} previewId={file.preview_id} mode="source" selectedSegmentId={props.resolution?.normalized_target.source_segment_id ?? props.selectedAnchorId} navigationToken={props.navigationToken} onPickSegment={props.onPickAnchor} highlights={(props.anchors ?? []).map(a=>({id:a.id,tone:a.tone,label:a.label}))} stale={false}/></div>}
  </div>;
}

function OriginalSourceViewer({ file, resolution, targetPage, navigationToken = 0, anchors = [], selectedAnchorId, onPickAnchor, compact = false, onLocate }: Props) {
  const src = file.source;
  const isPdf = src.mime.includes("pdf");
  const isImage = src.mime.startsWith("image/");
  const isDocx = src.mime.includes("word");
  const sourceRect = resolution?.anchor_precision === "rect" && resolution.coord_system === "page_normalized_top_left" ? resolution.bbox : null;
  const uiScope = `${src.case_id}:${src.document_id}:${file.parse_artifact_id ?? src.document_version}:source`;
  const [zoom, setZoom] = useReviewPreference(`${uiScope}:zoom`, compact ? 0.75 : 1);
  const [fitWidth, setFitWidth] = useReviewPreference(`${uiScope}:fit`, true);
  const [page, setPage] = useReviewPreference(`${uiScope}:page`, 1);
  const [manualPage, setManualPage] = useState<number | null>(null);
  const [pageToken, setPageToken] = useState(0);
  const [following, setFollowing] = useState(true);
  const [query, setQuery] = useState("");
  const [showFullPage, setShowFullPage] = useReviewPreference(`${uiScope}:full`, !compact);
  const [savedScroll, setSavedScroll] = useReviewPreference(`${uiScope}:scroll`, { top: 0, left: 0 });
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setFollowing(true); setManualPage(null); }, [navigationToken]);
  const restoreScroll = useRef(savedScroll);
  const restored = useRef(false);
  const restoreScope = useRef(uiScope);
  useEffect(() => { if (restoreScope.current === uiScope) return; restoreScope.current = uiScope; restoreScroll.current = savedScroll; restored.current = false; setManualPage(null); setQuery(""); setFollowing(true); }, [uiScope, savedScroll]);
  useEffect(() => {
    const element = scrollRef.current;
    if (!element || resolution || targetPage) { restored.current = true; return; }
    const restore = () => {
      if (restored.current) return;
      const target = restoreScroll.current;
      if (element.scrollHeight < target.top + element.clientHeight || element.scrollWidth < target.left + element.clientWidth) return;
      element.scrollTo({top: target.top, left: target.left}); restored.current = true;
    };
    const observer = new MutationObserver(restore);
    observer.observe(element, {childList: true, subtree: true, attributes: true}); restore();
    return () => observer.disconnect();
  }, [uiScope, resolution, targetPage]);

  if (file.parse_status !== "parsed" && file.parse_status !== "parsing") {
    // 未解析原件仍可只读预览，但不伪造字段与版本
    return <UnparsedNotice file={file} />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar
        src={src}
        pageCount={file.page_count}
        page={page}
        onPage={(value) => { setPage(value); setManualPage(value); setPageToken((old) => old + 1); setFollowing(true); }}
        zoom={zoom}
        onZoom={(z) => {
          setFitWidth(false);
          setZoom(z);
        }}
        fitWidth={fitWidth}
        onFitWidth={() => setFitWidth(true)}
        query={query}
        onQuery={setQuery}
        isPdf={isPdf}
        compact={compact}
        showFullPage={showFullPage}
        onToggleFullPage={() => setShowFullPage((v) => !v)}
      />
      {resolution && resolution.anchor_precision !== "rect" && (
        <div className="border-b border-line bg-warn-bg px-3 py-1.5 text-[12px] text-warn">
          <Icon name="warn" size={14} className="mr-1 inline align-[-2px]" />
          {resolution.anchor_precision === "none" ? "该来源没有可用坐标，无法精确框选" : `只能定位到${resolution.anchor_precision === "page_only" ? "页" : resolution.anchor_precision === "cell" ? "单元格" : "段落"}级别，无法精确框选`}
        </div>
      )}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto scroll-thin bg-canvas p-4" onScroll={event => { if(restored.current) setSavedScroll({top: event.currentTarget.scrollTop, left: event.currentTarget.scrollLeft}); }} onWheel={() => { restored.current = true; setFollowing(false); }} onTouchMove={() => { restored.current = true; setFollowing(false); }} onKeyDown={(e) => { if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(e.key)) setFollowing(false); }}>
        {query && file.parse_artifact_id && !isDocx && <SourceSearch file={file} query={query} onLocate={onLocate} />}
        <AnchorNotice anchors={anchors} selectedAnchorId={selectedAnchorId} onPickAnchor={onPickAnchor} />
        {isPdf && (
          <PdfPages
            url={contentUrls.document(src.document_id, src.document_version)}
            pageCount={file.page_count}
            zoom={zoom}
            fitWidth={fitWidth}
            containerRef={scrollRef}
            targetPage={following ? manualPage ?? targetPage : null}
            navigationToken={navigationToken + pageToken}
            targetRect={manualPage === null || manualPage === resolution?.page_no ? sourceRect : null}
            anchors={anchors}
            selectedAnchorId={selectedAnchorId}
            onPickAnchor={onPickAnchor}
            onVisiblePage={setPage}
            singlePage={compact && !showFullPage ? (manualPage ?? targetPage ?? page) : null}
            crop={compact && !showFullPage && (manualPage === null || manualPage === resolution?.page_no) ? sourceRect : null}
          />
        )}
        {isImage && <ImagePage url={contentUrls.document(src.document_id, src.document_version)} zoom={zoom} fitWidth={fitWidth} anchors={anchors} selectedAnchorId={selectedAnchorId} navigationToken={navigationToken} following={following} containerRef={scrollRef} onPickAnchor={onPickAnchor} filename={src.filename} crop={compact && !showFullPage ? sourceRect : null} />}
        {!isDocx && compact && resolution && resolution.anchor_precision !== "rect" && <DocxReader file={file} query="" resolution={resolution} navigationToken={navigationToken} following={following} onLocate={onLocate} />}
        {isDocx && <DocxReader file={file} query={query} resolution={resolution} navigationToken={navigationToken} following={following} onLocate={onLocate} />}
      </div>
    </div>
  );
}

function UnparsedNotice({ file }: { file: ReviewFileNode }) {
  const src = file.source;
  const isImage = src.mime.startsWith("image/");
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const download = async () => {
    setDownloading(true); setDownloadError(null);
    try { await downloadContent(contentUrls.document(src.document_id, src.document_version), src.filename); }
    catch { setDownloadError("无法下载原件，请确认仍有查看权限后重试。"); }
    finally { setDownloading(false); }
  };
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-11 items-center gap-2 border-b border-line bg-card px-3 text-[13px] text-ink-2">
        <Icon name="doc" size={16} />
        <span className="truncate">{src.filename}</span>
        <button type="button" className="btn btn-ghost btn-sm ml-auto shrink-0" onClick={download} disabled={downloading}>{downloading ? "正在下载" : "下载原件"}</button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
        {downloadError && <ErrorBar text={downloadError} />}
        <div className="mb-3 rounded-ctl border border-warn-line bg-warn-bg px-3 py-2 text-[13px] text-warn">
          <Icon name="warn" size={14} className="mr-1 inline align-[-2px]" />
          {file.parse_status === "failed" ? "该原件解析失败，字段与译文尚未形成。" : "该原件尚未解析，字段与译文尚未形成。"}
        </div>
        {isImage ? (
          <ImagePage url={contentUrls.document(src.document_id, src.document_version)} zoom={1} fitWidth anchors={[]} filename={src.filename} />
        ) : src.mime.includes("pdf") ? (
          <div className="flex min-h-[600px]"><PdfPages url={contentUrls.document(src.document_id, src.document_version)} pageCount={file.page_count} zoom={1} fitWidth containerRef={{current:null}} navigationToken={0} anchors={[]} onVisiblePage={() => undefined} singlePage={null} /></div>
        ) : (
          <p className="text-[13px] text-ink-2">可在解析完成后查看结构化内容。当前仅提供原件下载与上传状态。</p>
        )}
      </div>
    </div>
  );
}

function Toolbar({
  src,
  pageCount,
  page,
  onPage,
  zoom,
  onZoom,
  fitWidth,
  onFitWidth,
  query,
  onQuery,
  isPdf,
  compact,
  showFullPage,
  onToggleFullPage,
}: {
  src: SourceVersion;
  pageCount: number;
  page: number;
  onPage: (p: number) => void;
  zoom: number;
  onZoom: (z: number) => void;
  fitWidth: boolean;
  onFitWidth: () => void;
  query: string;
  onQuery: (q: string) => void;
  isPdf: boolean;
  compact: boolean;
  showFullPage: boolean;
  onToggleFullPage: () => void;
}) {
  const idx = ZOOMS.findIndex(value => value >= zoom);
  return (
    <div className="flex h-11 shrink-0 items-center gap-1.5 border-b border-line bg-card px-2">
      <Icon name={src.mime.includes("pdf") ? "file-pdf" : src.mime.startsWith("image/") ? "file-image" : "file-word"} size={16} className="shrink-0 text-ink-2" />
      <span className="mr-1 min-w-0 flex-1 truncate text-[13px] text-ink" title={src.filename}>
        {src.filename}
      </span>
      {isPdf && (
        <span className="flex items-center gap-1 text-[12px] text-ink-2">
          <button type="button" className="btn btn-ghost btn-sm w-8 px-0" onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1} aria-label="上一页">
            <Icon name="chevron-left" size={14} />
          </button>
          <span className="num whitespace-nowrap">
            {page} / {pageCount}
          </span>
          <button type="button" className="btn btn-ghost btn-sm w-8 px-0" onClick={() => onPage(Math.min(pageCount, page + 1))} disabled={page >= pageCount} aria-label="下一页">
            <Icon name="chevron-right" size={14} />
          </button>
        </span>
      )}
      <span className="mx-1 h-5 w-px bg-line" />
      <button type="button" className="btn btn-ghost btn-sm w-8 px-0" onClick={() => onZoom(ZOOMS[Math.max(0, idx - 1)])} disabled={idx <= 0} aria-label="缩小">
        －
      </button>
      <span className="num w-11 text-center text-[12px] text-ink-2">{Math.round((fitWidth ? 1 : zoom) * 100)}%</span>
      <button type="button" className="btn btn-ghost btn-sm w-8 px-0" onClick={() => onZoom(ZOOMS[Math.min(ZOOMS.length - 1, idx + 1)])} disabled={idx >= ZOOMS.length - 1} aria-label="放大">
        ＋
      </button>
      <button type="button" className={`btn btn-sm ${fitWidth ? "btn-secondary" : "btn-ghost"}`} onClick={onFitWidth}>
        适宽
      </button>
      {compact && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onToggleFullPage}>
          {showFullPage ? "看局部" : "查看整页"}
        </button>
      )}
      <label className="relative ml-1 w-32">
        <Icon name="search" size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-ink-3" />
        <input className="input h-8 pl-6.5 text-[13px]" style={{ paddingLeft: 26 }} placeholder="搜索原文" value={query} onChange={(e) => onQuery(e.target.value)} aria-label="在当前原件中搜索" />
      </label>
    </div>
  );
}

const toneBorder: Record<AnchorBox["tone"], string> = {
  neutral: "border-line",
  risk: "border-risk-line bg-[rgba(209,70,70,.08)]",
  warn: "border-warn-line bg-[rgba(217,154,36,.1)]",
  ok: "border-ok bg-[rgba(23,108,69,.07)]",
  brand: "border-sel bg-[rgba(31,99,214,.07)]",
};

const toneText: Record<AnchorBox["tone"], string> = {
  neutral: "bg-page text-ink-2",
  risk: "bg-risk text-white",
  warn: "bg-warn text-white",
  ok: "bg-ok text-white",
  brand: "bg-sel text-white",
};

/** 只为确有可信坐标的锚点绘制矩形；没有坐标的一律不画，交由页内提示条说明。 */
function AnchorLayer({ anchors, selectedAnchorId, onPickAnchor, width, height, pageNo }: { anchors: AnchorBox[]; selectedAnchorId?: string | null; onPickAnchor?: (id: string) => void; width: number; height: number; pageNo: number }) {
  return (
    <>
      {anchors
        .filter((a) => a.page_no === pageNo && a.rect)
        .filter((a, i, arr) => arr.findIndex((x) => x.id === a.id) === i)
        .map((a) => {
          const [x0, y0, x1, y1] = a.rect!;
          const selected = a.id === selectedAnchorId;
          return (
            <button
              key={`${pageNo}-${a.id}`}
              type="button"
              className={`absolute rounded-[3px] border-2 ${selected ? "border-[#1f63d6] bg-[#1f63d6]/10 ring-2 ring-[#1f63d6] ring-offset-1" : toneBorder[a.tone]}`}
              style={{ left: x0 * width, top: y0 * height, width: (x1 - x0) * width, height: (y1 - y0) * height }}
              onClick={() => onPickAnchor?.(a.id)}
              aria-label={`${a.label}：${a.id}`}
              title={a.label}
            >
              <span className={`absolute -top-[15px] left-0 whitespace-nowrap rounded-t px-1 text-[10px] leading-[15px] ${selected ? "bg-[#1f63d6] text-white" : toneText[a.tone]}`}>{a.label}</span>
            </button>
          );
        })}
    </>
  );
}

/** 无可靠坐标时的风险定位条：写明风险、所在页与区段，并可跳到该处。 */
function AnchorNotice({ anchors, selectedAnchorId, onPickAnchor }: { anchors: AnchorBox[]; selectedAnchorId?: string | null; onPickAnchor?: (id: string) => void }) {
  const list = anchors.filter((a) => !a.rect).filter((a, i, arr) => arr.findIndex((x) => x.id === a.id) === i);
  if (!list.length) return null;
  const toneCls: Record<AnchorBox["tone"], string> = {
    neutral: "border-line bg-page text-ink-2",
    risk: "border-risk-line bg-risk-bg text-risk",
    warn: "border-warn-line bg-warn-bg text-warn",
    ok: "border-line bg-ok-bg text-ok",
    brand: "border-sel-line bg-sel-soft text-sel",
  };
  return (
    <div className="mx-auto mb-3 w-full max-w-[760px] space-y-1.5">
      <p className="text-[11px] text-ink-3">这份原件没有可信的框选坐标，以下风险只能定位到页与区段：</p>
      {list.map((a) => (
        <button
          key={a.id}
          type="button"
          className={`flex w-full items-center gap-2 rounded-ctl border px-2.5 py-1.5 text-left text-[12px] ${a.id === selectedAnchorId ? "border-[#1f63d6] bg-[#edf3fe] text-[#1f63d6] ring-2 ring-[#1f63d6]" : toneCls[a.tone]}`}
          onClick={() => onPickAnchor?.(a.id)}
        >
          <Icon name={a.tone === "risk" ? "alert" : a.tone === "warn" ? "warn" : a.tone === "ok" ? "check" : "info"} size={14} className="shrink-0" />
          <span className="flex-1">
            {a.label}
            {a.where ? ` · ${a.where}` : ""}
          </span>
          <span className="shrink-0 text-ink-3">第 {a.page_no} 页</span>
        </button>
      ))}
    </div>
  );
}

/** 按可见页渲染；不把整份 PDF 一次塞入状态。 */
function PdfPages({
  url,
  pageCount,
  zoom,
  fitWidth,
  containerRef,
  targetPage,
  navigationToken,
  targetRect,
  anchors,
  selectedAnchorId,
  onPickAnchor,
  onVisiblePage,
  singlePage,
  crop,
}: {
  url: string;
  pageCount: number;
  zoom: number;
  fitWidth: boolean;
  containerRef: React.RefObject<HTMLDivElement | null>;
  targetPage?: number | null;
  navigationToken: number;
  targetRect?: number[] | null;
  anchors: AnchorBox[];
  selectedAnchorId?: string | null;
  onPickAnchor?: (id: string) => void;
  onVisiblePage: (p: number) => void;
  singlePage: number | null;
  crop?: number[] | null;
}) {
  const [doc, setDoc] = useState<pdfjs.PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [availableWidth, setAvailableWidth] = useState(0);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setAvailableWidth(el.clientWidth));
    observer.observe(el);
    return () => observer.disconnect();
  }, [containerRef]);

  useEffect(() => {
    let cancelled = false;
    let loaded: pdfjs.PDFDocumentProxy | null = null;
    setError(null);
    setDoc(null);
    const task = pdfjs.getDocument({ url, withCredentials: true, disableAutoFetch: true, disableStream: true, rangeChunkSize: 65536 }).promise;
    task
      .then((d) => {
        if (cancelled) {
          void d.destroy();
          return;
        }
        loaded = d;
        setDoc(d);
      })
      .catch(() => {
        if (!cancelled) setError("原件加载失败");
      });
    return () => {
      cancelled = true;
      void loaded?.destroy();
    };
  }, [url, reloadKey]);

  if (error) {
    return (
      <div className="mx-auto max-w-[520px]">
        <ErrorBar text={`${error}。已保留文件与页码，可重试读取。`} action={{ label: "重试", onSelect: () => setReloadKey((k) => k + 1) }} />
      </div>
    );
  }
  if (!doc) {
    return (
      <div className="flex justify-center py-10">
        <Loading text="正在加载原件" />
      </div>
    );
  }

  const pages = singlePage ? [singlePage] : Array.from({ length: doc.numPages }, (_, i) => i + 1);
  return (
    <div className="mx-auto flex w-fit flex-col items-center gap-4">
      {pages.map((p) => (
        <PdfPage
          key={p}
          doc={doc}
          pageNo={p}
          zoom={zoom}
          fitWidth={fitWidth}
          availableWidth={availableWidth}
          containerRef={containerRef}
          scrollToMe={targetPage === p ? navigationToken : null}
          targetRect={targetPage === p ? targetRect : null}
          anchors={anchors}
          selectedAnchorId={selectedAnchorId}
          onPickAnchor={onPickAnchor}
          onVisible={onVisiblePage}
          crop={singlePage === p ? crop : null}
        />
      ))}
    </div>
  );
}

function PdfPage({
  doc,
  pageNo,
  zoom,
  fitWidth,
  availableWidth,
  containerRef,
  scrollToMe,
  targetRect,
  anchors,
  selectedAnchorId,
  onPickAnchor,
  onVisible,
  crop,
}: {
  doc: pdfjs.PDFDocumentProxy;
  pageNo: number;
  zoom: number;
  fitWidth: boolean;
  availableWidth: number;
  containerRef: React.RefObject<HTMLDivElement | null>;
  scrollToMe: number | null;
  targetRect?: number[] | null;
  anchors: AnchorBox[];
  selectedAnchorId?: string | null;
  onPickAnchor?: (id: string) => void;
  onVisible: (p: number) => void;
  crop?: number[] | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const cropHost = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState<{ w: number; h: number; scale: number; pt: number } | null>(null);
  const [near, setNear] = useState(pageNo <= 2);
  const [renderError, setRenderError] = useState(false);
  useEffect(() => { if (scrollToMe !== null) setNear(true); }, [scrollToMe]);

  // 只渲染进入视区附近的页
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            setNear(true);
            if (e.intersectionRatio > 0.5) onVisible(pageNo);
          }
        });
      },
      { root: containerRef.current, rootMargin: "400px 0px", threshold: [0, 0.5] },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [containerRef, pageNo, onVisible]);

  // 同一画布一次只允许一个渲染任务。取消是异步的，必须等上一次真正结束再开始，
  // 否则 PDF.js 会因同画布并发渲染报错。
  const task = useRef<pdfjs.RenderTask | null>(null);
  const settled = useRef<Promise<void>>(Promise.resolve());

  const draw = useCallback(async () => {
    if (!near || !canvas.current) return;
    const page = await doc.getPage(pageNo);
    if (!canvas.current) return;
    const base = page.getViewport({ scale: 1 });
    const avail = Math.max(120, (availableWidth || containerRef.current?.clientWidth || 800) - 48);
    const cropWidth = crop ? Math.min(1, crop[2] - crop[0] + .16) : 1;
    const scale = fitWidth ? Math.min(3, avail / (base.width * cropWidth)) : zoom;
    const vp = page.getViewport({ scale });
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cv = canvas.current;
    cv.width = vp.width * dpr;
    cv.height = vp.height * dpr;
    cv.style.width = `${vp.width}px`;
    cv.style.height = `${vp.height}px`;
    const ctx = cv.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const t = page.render({ canvasContext: ctx, viewport: vp });
    task.current = t;
    try {
      await t.promise;
      setSize({ w: vp.width, h: vp.height, scale, pt: base.height });
    } catch (e) {
      if ((e as Error)?.name !== "RenderingCancelledException") throw e;
    } finally {
      if (task.current === t) task.current = null;
    }
  }, [doc, pageNo, zoom, fitWidth, near, containerRef, availableWidth, crop?.join(",")]);

  // 同步接到上一次之后排队，保证同一画布上的渲染严格串行
  const render = useCallback(() => {
    task.current?.cancel();
    settled.current = settled.current.then(draw, draw);
    return settled.current;
  }, [draw]);

  useEffect(() => {
    setRenderError(false);
    void render().catch(() => setRenderError(true));
    return () => task.current?.cancel();
  }, [render]);

  // 高亮消退只改变标记，不重新滚动已裁剪的整页画布。
  useEffect(() => {
    if (scrollToMe === null || !host.current || !size) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.documentElement.dataset.motion === "reduce";
    const box = targetRect ?? anchors.find(anchor => anchor.id === selectedAnchorId && anchor.page_no === pageNo)?.rect;
    const container = containerRef.current;
    if (crop && container && cropHost.current) {
      const outer = container.getBoundingClientRect(); const inner = cropHost.current.getBoundingClientRect();
      container.scrollTo({top: container.scrollTop + inner.top - outer.top + inner.height / 2 - container.clientHeight / 2, left: container.scrollLeft + inner.left - outer.left + inner.width / 2 - container.clientWidth / 2, behavior: reduce ? "auto" : "smooth"});
    }
    else if (box && container) { const outer = container.getBoundingClientRect(); const inner = host.current.getBoundingClientRect(); container.scrollTo({top: container.scrollTop + inner.top - outer.top + (box[1] + box[3]) / 2 * size.h - container.clientHeight / 2, left: container.scrollLeft + inner.left - outer.left + (box[0] + box[2]) / 2 * size.w - container.clientWidth / 2, behavior: reduce ? "auto" : "smooth"}); }
    else host.current.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
  }, [scrollToMe, size, targetRect?.join(","), crop?.join(","), containerRef, pageNo]);

  const region = crop && size ? {x: Math.max(0,crop[0]-.08)*size.w, y: Math.max(0,crop[1]-.06)*size.h, w: (Math.min(1,crop[2]+.08)-Math.max(0,crop[0]-.08))*size.w, h: (Math.min(1,crop[3]+.06)-Math.max(0,crop[1]-.06))*size.h} : null;
  return (
    <div ref={cropHost} className={`relative ${region ? "overflow-hidden" : "overflow-visible"}`} style={region ? {width:region.w,height:region.h} : undefined}>
    <div ref={host} className="relative bg-white shadow-[0_1px_2px_rgba(17,20,24,.08),0_6px_20px_rgba(17,20,24,.08)]" style={{ width: size?.w ?? 620, height: size?.h ?? 876, transform:region ? `translate(${-region.x}px, ${-region.y}px)` : undefined }}>
      <canvas ref={canvas} className="block" />
      {renderError && <div className="absolute inset-0 flex items-center justify-center bg-white p-4"><ErrorBar text={`第 ${pageNo} 页读取失败，可重新打开原件。`} /></div>}
      {!size && !renderError && (
        <div className="absolute inset-0 flex items-center justify-center text-[12px] text-ink-3">
          <Loading text={`第 ${pageNo} 页`} />
        </div>
      )}
      {size && <AnchorLayer anchors={anchors} selectedAnchorId={selectedAnchorId} onPickAnchor={onPickAnchor} width={size.w} height={size.h} pageNo={pageNo} />}
      <span className="absolute -bottom-5 left-0 text-[11px] text-ink-3">第 {pageNo} 页</span>
    </div></div>
  );
}

function ImagePage({ url, zoom, fitWidth, anchors, selectedAnchorId, navigationToken = 0, following = true, containerRef, onPickAnchor, filename, crop }: { url: string; zoom: number; fitWidth: boolean; anchors: AnchorBox[]; selectedAnchorId?: string | null; navigationToken?: number; following?: boolean; containerRef?: React.RefObject<HTMLDivElement | null>; onPickAnchor?: (id: string) => void; filename: string; crop?: number[] | null }) {
  const [href, setHref] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const [rendered, setRendered] = useState({w:0,h:0});
  useEffect(() => {
    const el=imageRef.current;
    if (!el) return;
    const update=()=>setRendered({w:el.clientWidth,h:el.clientHeight});
    const observer=new ResizeObserver(update);observer.observe(el);update();
    return ()=>observer.disconnect();
  },[href,natural,fitWidth,zoom]);

  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    setError(false);
    setHref(null);
    setNatural(null);
    setRendered({ w: 0, h: 0 });
    fetchContent(url)
      .then((b) => {
        if (cancelled) return;
        revoked = URL.createObjectURL(b);
        setHref(revoked);
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      // 身份切换或卸载时立即释放，不在浏览器留存原件字节
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [url]);

  useEffect(() => {
    if (!following || !selectedAnchorId || !rendered.w || !rendered.h || !containerRef?.current || !imageRef.current) return;
    const box = anchors.find(anchor => anchor.id === selectedAnchorId)?.rect;
    if (!box) return;
    const container = containerRef.current;
    const outer = container.getBoundingClientRect();
    const inner = imageRef.current.getBoundingClientRect();
    container.scrollTo({ top: container.scrollTop + inner.top - outer.top + (box[1] + box[3]) / 2 * rendered.h - container.clientHeight / 2,
      left: container.scrollLeft + inner.left - outer.left + (box[0] + box[2]) / 2 * rendered.w - container.clientWidth / 2,
      behavior: document.documentElement.dataset.motion === "reduce" || window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }, [following, selectedAnchorId, navigationToken, rendered.w, rendered.h, crop?.join(","), containerRef, anchors]);

  if (error) return <ErrorBar text="原件图片加载失败，可稍后重试。" />;
  if (!href) return <div className="flex justify-center py-10"><Loading text="正在加载原件" /></div>;
  const scale = fitWidth ? 1 : zoom;
  const region = crop && rendered.w ? {x:Math.max(0,crop[0]-.08)*rendered.w,y:Math.max(0,crop[1]-.06)*rendered.h,w:(Math.min(1,crop[2]+.08)-Math.max(0,crop[0]-.08))*rendered.w,h:(Math.min(1,crop[3]+.06)-Math.max(0,crop[1]-.06))*rendered.h} : null;
  return (
    <div className="mx-auto w-fit bg-white p-1 shadow-[0_1px_2px_rgba(17,20,24,.08),0_6px_20px_rgba(17,20,24,.08)]">
      <div className="overflow-hidden" style={region ? {width:region.w,height:region.h} : undefined}><div className="relative" style={{transform:region ? `translate(${-region.x}px, ${-region.y}px)` : undefined}}>
        <img ref={imageRef} src={href} alt={`原件 ${filename}`} className={`block ${fitWidth ? "max-w-full" : "max-w-none"}`} style={{ imageOrientation: "from-image", ...(crop ? {width:Math.min(3000,600/Math.max(.15,crop[2]-crop[0]+.16)),maxWidth:"none"} : fitWidth ? {} : { width: (natural?.w ?? 0) * scale || undefined }) }} onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} />
        {rendered.w>0 && <AnchorLayer anchors={anchors} selectedAnchorId={selectedAnchorId} onPickAnchor={onPickAnchor} width={rendered.w} height={rendered.h} pageNo={1} />}
      </div></div>
    </div>
  );
}

type SourceRow={source_segment_id:string;logical_document_id?:string;section_id:string;structure:string;text:string;page_no?:number|null;table_row?:number|null;table_col?:number|null};
function rowTarget(file:ReviewFileNode,row:SourceRow):EvidenceTarget {
  return {case_id:file.source.case_id,document_id:file.source.document_id,document_version:file.source.document_version,
    parse_artifact_id:file.parse_artifact_id??undefined,logical_document_id:row.logical_document_id,section_id:row.section_id,source_segment_id:row.source_segment_id};
}
function SourceSearch({file,query,onLocate}:{file:ReviewFileNode;query:string;onLocate?:(target:EvidenceTarget)=>void}) {
  const [rows,setRows]=useState<SourceRow[]>([]);const [error,setError]=useState(false);const [busy,setBusy]=useState(false);
  useEffect(()=>{
    const controller=new AbortController();setBusy(true);setError(false);
    const timer=setTimeout(()=>{void api<ApiResponses["GET /api/v1/parse-artifacts/{parse_artifact_id}/reading-view"]["200"]>(`/parse-artifacts/${file.parse_artifact_id}/reading-view`,{query:{query,limit:50},signal:controller.signal})
      .then(result=>{if(!controller.signal.aborted)setRows(result.items);})
      .catch(()=>{if(!controller.signal.aborted)setError(true);})
      .finally(()=>{if(!controller.signal.aborted)setBusy(false);});},250);
    return()=>{clearTimeout(timer);controller.abort();};
  },[file.parse_artifact_id,query]);
  return <div className="mx-auto mb-3 max-w-[760px] rounded-ctl border border-line bg-white p-3" aria-label="原文搜索结果">
    <p className="text-[12px] text-ink-2">{busy?"正在搜索原文":error?"原文搜索失败":`当前解析版命中 ${rows.length} 个片段（最多显示50个）`}</p>
    {!busy&&!error&&rows.map(row=><button type="button" key={row.source_segment_id} className="mt-2 block w-full rounded-ctl p-2 text-left text-[12px] hover:bg-page" onClick={()=>onLocate?.(rowTarget(file,row))}><span className="mr-2 text-ink-3">{row.page_no?`第${row.page_no}页`:"原文段落"}</span>{row.text}</button>)}
  </div>;
}

/** DOCX 只显示受控段表，不在浏览器执行宏或加载外链。 */
function DocxReader({ file, query, resolution, navigationToken, following, onLocate }: { file: ReviewFileNode; query: string; resolution?: EvidenceResolution | null; navigationToken:number; following:boolean; onLocate?: (target:EvidenceTarget)=>void }) {
  const [rows, setRows] = useState<SourceRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const reader = useRef<HTMLDivElement>(null);
  const targetId=resolution?.normalized_target.document_id===file.source.document_id ? resolution.normalized_target.source_segment_id : undefined;
  const load = useCallback(async (after?: string) => {
    const current = generation.current;
    setLoading(true);setError(null);
    try {
      const result = await api<ApiResponses["GET /api/v1/parse-artifacts/{parse_artifact_id}/reading-view"]["200"]>(`/parse-artifacts/${file.parse_artifact_id}/reading-view`, {query: {query, cursor:after, source_segment_id:!after&&!query?targetId:undefined, limit:200}});
      if (current !== generation.current) return;
      setRows((old) => after ? [...old, ...result.items] : result.items);
      setCursor(result.next_cursor ?? null);
    } catch { if (current === generation.current) setError("原件段落读取失败"); }
    finally { if (current === generation.current) setLoading(false); }
  },[file.parse_artifact_id, query, targetId]);
  useEffect(() => {generation.current++;setRows([]);setCursor(null);void load();return () => {generation.current++;};},[load]);
  useEffect(()=>{
    if (!targetId || !following) return;
    const el=reader.current?.querySelector(`[data-source-segment="${targetId}"]`);
    if(el)el.scrollIntoView({block:"center",behavior:document.documentElement.dataset.motion==="reduce"?"auto":"smooth"});
    else if(cursor&&!loading&&!query)void load(cursor);
  },[targetId,navigationToken,rows,cursor,loading,query,load,following]);
  return <div ref={reader} className="mx-auto max-w-[760px] rounded-card bg-white p-5">
    <p className="mb-3 text-[12px] text-ink-2">识别阅读副本：按真实原文段落与表格定位；版式仍以原件为准。</p>
    {rows.map(row => <button type="button" key={row.source_segment_id} data-source-segment={row.source_segment_id} className={`mb-2 block w-full whitespace-pre-wrap rounded-ctl border px-3 py-2 text-left [font-size:var(--review-font-size,14px)] ${targetId===row.source_segment_id?"border-[#1f63d6] bg-[#edf3fe] ring-2 ring-[#1f63d6]":"border-line"}`} onClick={()=>onLocate?.(rowTarget(file,row))}>{row.table_row!=null&&<span className="mb-1 block text-[11px] text-ink-3">表格 · 第{row.table_row+1}行 / 第{(row.table_col??0)+1}列</span>}{row.text}</button>)}
    {error&&<ErrorBar text={error} action={{label:"重试",onSelect:()=>void load()}}/>}
    {loading&&<Loading text="正在读取原文"/>}
    {!loading&&!rows.length&&!error&&<p>没有匹配的原文段落</p>}
    {cursor&&<button type="button" className="btn btn-secondary" disabled={loading} onClick={()=>void load(cursor)}>加载更多原文</button>}
  </div>;
}
