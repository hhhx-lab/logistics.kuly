import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { consignmentApi, scopePreconditions } from "../../api/consignment";
import { errorText } from "../../api/client";
import { workflowApi } from "../../api/workflow";
import { useSession } from "../../stores/session";
import { UiText } from "../../i18n/runtime";
import { ErrorBar, Loading, StatusPill, formatTime } from "../../components/Status";
import { useCommand } from "../conversation/hooks";

export function CaseComparisonSummary({ caseId, onOpenRun, onUpload }: { caseId: string; onOpenRun: (runId: string) => void; onUpload: () => void }) {
  const generation = useSession(state => state.generation);
  const [pendingRun, setPendingRun] = useState<string | null>(null);
  const scope = useQuery({ queryKey: ["consignment", generation, caseId, "scope", "current"], queryFn: ({ signal }) => consignmentApi.scope(caseId, null, signal), retry: false });
  const batches = useQuery({ queryKey: ["consignment", generation, caseId, "batches", "current"], queryFn: ({ signal }) => consignmentApi.batches(caseId, undefined, signal), retry: false, refetchInterval: pendingRun ? 2500 : false });
  const pending = useQuery({ queryKey: ["w-run", generation, pendingRun], queryFn: () => workflowApi.run(pendingRun!), enabled: !!pendingRun, retry: false, refetchInterval: query => ["queued", "processing", "awaiting_input"].includes(query.state.data?.status ?? "queued") ? 2500 : false });
  useEffect(() => {
    if (!pending.data || ["queued", "processing", "awaiting_input"].includes(pending.data.status)) return;
    setPendingRun(null);
    void scope.refetch(); void batches.refetch();
  }, [pending.data?.status, scope.refetch, batches.refetch]);
  const batch = batches.data?.items.find(item => item.current) ?? batches.data?.items[0];
  const stale = !!scope.data?.stale || !!batch?.stale;
  const recompute = useCommand<void, { run_id: string | null }>((_args, key) => {
    if (!scope.data) return Promise.reject(new Error("The current case scope could not be loaded."));
    return consignmentApi.recompute(caseId, { ...scopePreconditions(scope.data), previous_batch_id: batch?.batch_id ?? null }, key);
  }, ["consignment", "m-runs", "review-view"]);
  return <section className="card p-3">
    <h2 className="mb-2 text-[14px] font-medium">同票核对</h2>
    {scope.isPending || batches.isPending ? <Loading /> : scope.error || batches.error ? <ErrorBar text={errorText(scope.error ?? batches.error)} action={{ label: "重试", onSelect: () => { void scope.refetch(); void batches.refetch(); } }} /> : <>
      <StatusPill tone={stale ? "warn" : batch?.status === "failed" ? "risk" : "info"}><UiText value={stale ? "新版本待重算" : batch?.status === "completed" ? "核对完成" : batch?.status === "failed" ? "当前批次失败" : batch ? "正在核对" : "核对未执行"} /></StatusPill>
      <p className="mt-2 text-[12px] text-ink-3">当前材料 {scope.data?.payload.materials.filter(item => item.purpose !== "excluded").length ?? 0} 份{batch ? ` · ${formatTime(batch.created_at)}` : ""}</p>
      {stale && <p className="mt-1 text-[12px] text-warn">新材料尚未纳入上次核对</p>}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {batch && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenRun(batch.run_id)}>查看同票对比</button>}
        <button type="button" className="btn btn-ghost btn-sm" onClick={onUpload}>补充上传</button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={!scope.data || recompute.isPending || !!pendingRun} onClick={() => recompute.mutate(undefined, { onSuccess: result => { setPendingRun(result.run_id); if (result.run_id) onOpenRun(result.run_id); } })}>{recompute.isPending || pendingRun ? "重核对中" : "重新核对"}</button>
      </div>
    </>}
    {recompute.error && <div className="mt-2"><ErrorBar text={errorText(recompute.error)} /></div>}
    {pending.error && <div className="mt-2"><ErrorBar text={errorText(pending.error)} action={{ label: "重试", onSelect: () => void pending.refetch() }} /></div>}
  </section>;
}
