import { useReviewPreference } from "../../stores/reviewPreference";
// 文件树：物理源文件 → 逻辑单据 → 区段。左箭头只展开，文件名才切换；
// 待确认类型用黄徽标，不显示成红色假冲突。
import { useState } from "react";
import type { ReviewFileNode, ReviewView } from "../../api/types";
import { fileIcon, Icon } from "../../components/Icon";
import { StatusPill } from "../../components/Status";

export function FileTree({
  view,
  selectedDocumentId,
  selectedSectionId,
  onSelectFile,
  onSelectSection,
  onAssignSection,
  editable,
}: {
  view: ReviewView;
  selectedDocumentId: string | null;
  selectedSectionId: string | null;
  onSelectFile: (documentId: string) => void;
  onSelectSection: (documentId: string, logicalDocumentId: string, sectionId: string) => void;
  onAssignSection: (sectionId: string, suggestion: string) => void;
  editable: boolean;
}) {
  const [expanded, setExpanded] = useReviewPreference<Record<string,boolean>>(`${view.case_id}:${view.snapshot_id ?? view.business_revision_id}:file-tree`, view.files[0] ? {[view.files[0].source.document_id]:true} : {});
  const toggle = (id: string) => setExpanded((p) => ({ ...p, [id]: !p[id] }));

  return (
    <nav className="min-h-0 flex-1 overflow-auto scroll-thin py-1" aria-label="文件树">
      {view.files.map((f) => (
        <FileRow
          key={f.source.document_id}
          file={f}
          expanded={!!expanded[f.source.document_id]}
          selected={selectedDocumentId === f.source.document_id}
          selectedSectionId={selectedSectionId}
          onToggle={() => toggle(f.source.document_id)}
          onSelectFile={() => onSelectFile(f.source.document_id)}
          onSelectSection={(ld, s) => onSelectSection(f.source.document_id, ld, s)}
          onAssignSection={onAssignSection}
          editable={editable}
        />
      ))}
    </nav>
  );
}

function FileRow({
  file,
  expanded,
  selected,
  selectedSectionId,
  onToggle,
  onSelectFile,
  onSelectSection,
  onAssignSection,
  editable,
}: {
  file: ReviewFileNode;
  expanded: boolean;
  selected: boolean;
  selectedSectionId: string | null;
  onToggle: () => void;
  onSelectFile: () => void;
  onSelectSection: (logicalDocumentId: string, sectionId: string) => void;
  onAssignSection: (sectionId: string, suggestion: string) => void;
  editable: boolean;
}) {
  const [logicalExpanded,setLogicalExpanded] = useReviewPreference<Record<string,boolean>>(`${file.source.case_id}:${file.source.document_id}:${file.parse_artifact_id}:logical-tree`, {});
  const counts = file.issue_counts;
  const todo = counts.blocking + counts.warning + counts.undetermined;
  const parsed = file.parse_status === "parsed";
  return (
    <div>
      <div className={`flex min-h-[44px] items-center gap-0.5 pr-2 ${selected ? "bg-fill-strong" : "hover:bg-fill"}`}>
        {parsed ? (
          <button type="button" className="flex h-9 w-6 shrink-0 items-center justify-center text-ink-3" onClick={onToggle} aria-label={expanded ? "收起" : "展开"} aria-expanded={expanded}>
            <Icon name={expanded ? "chevron-down" : "chevron-right"} size={14} />
          </button>
        ) : (
          <span className="w-6 shrink-0" />
        )}
        <button type="button" className="flex min-w-0 flex-1 items-start gap-1.5 py-1.5 text-left" onClick={onSelectFile} aria-current={selected || undefined}>
          <Icon name={fileIcon(file.source.mime)} size={16} className="mt-0.5 shrink-0 text-ink-2" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] text-ink" title={file.source.filename}>
              {file.source.filename}
            </span>
            <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-3">
              <span className="num">{file.source.mime.includes("word") ? "段落／表格" : file.page_count ? `${file.page_count} 页` : "页数待确认"}</span>
              {!parsed && <span className="text-warn">{file.parse_status === "failed" ? "解析失败" : "尚未解析"}</span>}
              {todo > 0 && (
                <span className={counts.blocking ? "text-risk" : "text-warn"}>
                  {counts.blocking ? `${counts.blocking} 红` : ""}
                  {counts.warning ? ` ${counts.warning} 黄` : ""}
                  {counts.undetermined ? ` ${counts.undetermined} 待判` : ""}
                </span>
              )}
            </span>
          </span>
        </button>
      </div>
      {expanded &&
        file.logical_documents.map((ld) => (
          <div key={ld.logical_document_id}>
            <div className="flex min-h-[36px] items-center gap-1.5 pl-8 pr-2 py-1">
              <button type="button" className="btn btn-ghost btn-icon h-8 w-8" aria-label="展开或收起逻辑单据" aria-expanded={logicalExpanded[ld.logical_document_id] !== false} onClick={() => setLogicalExpanded(values => ({...values,[ld.logical_document_id]:values[ld.logical_document_id] === false}))}><Icon name={logicalExpanded[ld.logical_document_id] === false ? "chevron-right" : "chevron-down"} size={14} /></button>
              <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">{ld.label}</span>
              <span className="num shrink-0 text-[11px] text-ink-3">{ld.page_range}</span>
              {ld.needs_confirmation && (
                <StatusPill tone="warn" icon="warn" title="解析未能确定这部分属于哪种单据">
                  待确认
                </StatusPill>
              )}
            </div>
            {editable && ld.sections.length > 0 && (
              <div className="pb-1 pl-8">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => onAssignSection(ld.sections.find(section => section.section_id === selectedSectionId)?.section_id ?? ld.sections[0].section_id, ld.label)}>
                  {ld.needs_confirmation ? "确认类型或区段" : "调整类型与货物领域"}
                </button>
              </div>
            )}
            {logicalExpanded[ld.logical_document_id] !== false && ld.sections.map((s) => (
              <button
                key={`${ld.logical_document_id}-${s.section_id}`}
                type="button"
                className={`flex min-h-[32px] w-full items-center gap-1.5 py-1 pl-12 pr-2 text-left text-[12px] ${selectedSectionId === s.section_id ? "bg-fill-strong font-medium text-ink" : "text-ink-2 hover:bg-fill"}`}
                onClick={() => onSelectSection(ld.logical_document_id, s.section_id)}
              >
                <span className="min-w-0 flex-1 truncate">{s.title}</span>
                {!s.translated && <span className="shrink-0 text-[11px] text-ink-3">未译</span>}
              </button>
            ))}
          </div>
        ))}
    </div>
  );
}
