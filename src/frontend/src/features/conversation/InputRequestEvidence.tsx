import { useResolveEvidence } from "../review/hooks";
import type { EvidenceTarget, InputRequestView } from "../../api/types";

type SourceLabel = { document_id: string; document_version: number; filename: string };

export function InputRequestEvidence({ request, sources, onLocate }: {
  request: InputRequestView;
  sources: SourceLabel[];
  onLocate: (target: EvidenceTarget) => void;
}) {
  const option = request.options.find(item => item.source_ref);
  const target = option?.source_ref ?? null;
  const location = useResolveEvidence(target);
  const source = sources.find(item => item.document_id === target?.document_id && item.document_version === target?.document_version);
  if (!target) return null;
  return <section className="mt-3 rounded-ctl border border-line p-3 text-[12px] text-ink-2" aria-label="待确认原件">
    <p className="break-words font-medium text-ink">{source?.filename ?? "待确认原件"}{location.data?.page_no ? ` · 第 ${location.data.page_no} 页` : ""}</p>
    {option?.detail && <p className="mt-1">{option.detail}</p>}
    {option?.excerpt && <p className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-ctl bg-page p-2">{option.excerpt}</p>}
    <button type="button" className="btn btn-link btn-sm mt-1 px-0" onClick={() => onLocate(target)}>在原件中定位</button>
  </section>;
}
