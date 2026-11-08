// 源译片段列表（R05）：查看原译文字、对齐组、术语与 QA，并执行译段修订。
// 与 R06「译后版面」互补，不互相替代。
import { useEffect } from "react";
import type { ReviewView, TranslationSegmentView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { EmptyState, ErrorBar, Loading, StatusPill } from "../../components/Status";
import { useTranslations } from "./hooks";

const QA: Record<TranslationSegmentView["qa_status"], { text: string; tone: "ok" | "warn" | "hist" | "dis"; icon: "check" | "warn" | "history" | "info" }> = {
  passed: { text: "已通过检查", tone: "ok", icon: "check" },
  warning: { text: "待核查", tone: "warn", icon: "warn" },
  stale: { text: "待重算", tone: "hist", icon: "history" },
  not_translated: { text: "原文保留／未翻译", tone: "dis", icon: "info" },
};

export function TranslationViewer({
  view,
  documentId,
  sectionId,
  editable,
  selectedSegmentId,
  navigationToken = 0,
  onSelect,
  onLocateSource,
  onRevise,
}: {
  view: ReviewView;
  documentId: string | null;
  sectionId: string | null;
  editable: boolean;
  selectedSegmentId?: string | null;
  navigationToken?: number;
  onSelect: (s: TranslationSegmentView) => void;
  onLocateSource: (s: TranslationSegmentView) => void;
  onRevise: (s: TranslationSegmentView) => void;
}) {
  const q = useTranslations(view, documentId, sectionId);
  const items = q.items;
  const notApplicable = q.notApplicable;
  useEffect(() => {
    if (selectedSegmentId && !items.some(item=>item.translation_segment_id===selectedSegmentId) && q.hasNextPage && !q.isFetching && !q.error) void q.fetchNextPage();
  }, [selectedSegmentId, items, q.hasNextPage, q.isFetching, q.error, q.fetchNextPage]);

  useEffect(() => { if (!selectedSegmentId) return; document.querySelector(`[data-translation-id="${selectedSegmentId}"]`)?.scrollIntoView({block:"nearest"}); }, [selectedSegmentId, navigationToken, items.length]);

  if (!view.run_id || notApplicable) {
    return <EmptyState icon="doc" title="尚无译文" description="这次只做了识别与索引，没有生成译文。要得到双语译本，需要发起一次翻译任务。" />;
  }
  if (q.isPending) return <div className="p-6"><Loading text="正在读取译段" /></div>;
  if (q.error) return <div className="p-4"><ErrorBar text="译段读取失败，请刷新后重试。" /></div>;
  if (!items.length) {
    return <EmptyState icon="doc" title="当前选择下没有译段" description="可以切换文件或区段查看；若本次只翻译了指定范围，未选中的内容会保留原文。" />;
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto scroll-thin">
      {items.map((s) => {
        const qa = QA[s.qa_status];
        const selected = selectedSegmentId === s.translation_segment_id;
        const untranslated = s.qa_status === "not_translated";
        return (
          <article data-translation-id={s.translation_segment_id} key={s.translation_segment_id} className={`border-b border-line px-3 py-2.5 ${selected ? "bg-[#edf3fe] ring-2 ring-inset ring-[#1f63d6]" : "hover:bg-page"}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[11px] text-ink-3">{s.section_title}</span>
              <StatusPill tone={qa.tone} icon={qa.icon}>
                {qa.text}
              </StatusPill>
              <span className="num text-[11px] text-ink-3">译文版 {s.revision_no}</span>
            </div>
            <button type="button" className="mt-1.5 block w-full text-left" onClick={() => onSelect(s)}>
              <p className="break-words-all text-[length:var(--review-font-size,14px)] text-ink-2">
                <span className="text-[11px] text-ink-3">原文 </span>
                {s.source_text}
              </p>
              <p className={`mt-1 break-words-all text-[length:var(--review-font-size,14px)] ${untranslated ? "rounded-ctl border border-dashed border-line bg-page px-2 py-1 text-ink-3" : "text-ink"}`}>
                {untranslated ? "本次未翻译该段，交付时保留原文" : (
                  <>
                    <span className="text-[11px] text-ink-3">译文 </span>
                    {s.translated_text}
                  </>
                )}
              </p>
            </button>
            {s.terms.length > 0 && <p className="mt-1 text-[11px] text-ink-3">术语：{s.terms.join("、")}</p>}
            <div className="mt-1.5 flex items-center gap-1">
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onLocateSource(s)}>
                <Icon name="source" size={14} />
                定位原文
              </button>
              {editable && !untranslated && (
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => onRevise(s)}>
                  <Icon name="pencil" size={14} />
                  修订译文
                </button>
              )}
            </div>
          </article>
        );
      })}
      {q.hasNextPage && <div className="p-3"><button type="button" className="btn btn-secondary" disabled={q.isFetchingNextPage} onClick={()=>void q.fetchNextPage()}>{q.isFetchingNextPage ? "正在加载" : "加载更多译段"}</button></div>}
    </div>
  );
}
