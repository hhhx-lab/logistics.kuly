// 运行核查抽屉（O01/O02）：只附着在 Run 时间线，不进主导航。
// 前端权限只控制呈现，每次 GET/POST 仍由服务端复验；403 立即清表单与查询。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { errorText, isApiError, newIdempotencyKey } from "../../api/client";
import { opsApi } from "../../api/ops";
import type { OpsStepView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useEscLayer, useToast } from "../../components/Overlay";
import { ErrorBar, Loading, StatusPill, formatDateTime , shortId } from "../../components/Status";
import { useMigrationOptions, useOpsStep } from "./hooks";

const DECISIONS: { id: OpsStepView["allowed_decisions"][number]; label: string; meaning: string }[] = [
  { id: "verified_executed", label: "已确认外部已执行", meaning: "有依据表明外部服务确实执行了这次请求，处理将按已执行继续。" },
  { id: "confirmed_not_executed", label: "已确认外部未执行", meaning: "有依据表明外部未执行，可安全重试而不会重复计费。" },
  { id: "known_response_local_failure", label: "模型已响应，本地处理失败", meaning: "成功回执已有账本凭据，但本地未保存业务产物。恢复会保留原操作并重试，可能再次调用模型及计费。" },
  { id: "still_unknown", label: "仍无法确认", meaning: "暂时无法判断，保持待核查状态，不做任何推进。" },
  { id: "terminate_run", label: "终止这次处理", meaning: "放弃本次处理；需要时另建新任务，历史记录保留。" },
];

export function OpsRunDrawer({
  runId,
  stepId,
  canReconcile,
  canMigrate,
  initialTab,
  onClose,
}: {
  runId: string;
  stepId: string | null;
  canReconcile: boolean;
  canMigrate: boolean;
  initialTab: "reconcile" | "migrate";
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"reconcile" | "migrate">(initialTab);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const hasBoth = canReconcile && canMigrate;
  useEscLayer(!confirmOpen, onClose);

  // 无权的分区不预取
  const effectiveTab = tab === "reconcile" && !canReconcile ? "migrate" : tab === "migrate" && !canMigrate ? "reconcile" : tab;

  return (
    <div className="fixed inset-y-0 right-0 z-panel flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-panel anim-slide-in" role="dialog" aria-label="运营操作">
      <header className="flex h-bar shrink-0 items-center gap-2 border-b border-line px-4">
        <div className="min-w-0 flex-1">
          <p className="text-[14px] font-medium text-ink">运营操作</p>
          <p className="text-[11px] text-ink-3">处理 {shortId(runId)}</p>
        </div>
        <button type="button" className="btn btn-icon btn-ghost" onClick={onClose} aria-label="关闭运营抽屉">
          <Icon name="close" size={16} />
        </button>
      </header>

      {hasBoth && (
        <div className="flex shrink-0 gap-0.5 border-b border-line px-3" role="tablist">
          {(["reconcile", "migrate"] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={effectiveTab === t} className={`relative px-3 py-2.5 text-[13px] ${effectiveTab === t ? "font-medium text-ink" : "text-ink-2"}`} onClick={() => setTab(t)}>
              {t === "reconcile" ? "外部请求核查" : "迁移模型"}
              {effectiveTab === t && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-brand" />}
            </button>
          ))}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto scroll-thin">
        {effectiveTab === "reconcile" ? (
          canReconcile && stepId ? (
            <ReconcileSection stepId={stepId} runId={runId} onDone={onClose} onConfirmOpen={setConfirmOpen} />
          ) : (
            <p className="p-4 text-[13px] text-ink-2">没有可核查的受控步骤，或你没有核查权限。</p>
          )
        ) : canMigrate ? (
          <MigrateSection runId={runId} onDone={onClose} onConfirmOpen={setConfirmOpen} />
        ) : (
          <p className="p-4 text-[13px] text-ink-2">你没有迁移模型的权限。</p>
        )}
      </div>
    </div>
  );
}

function ReconcileSection({ stepId, runId, onDone, onConfirmOpen }: { stepId: string; runId: string; onDone: () => void; onConfirmOpen: (v: boolean) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useOpsStep(stepId, true);
  const [decision, setDecision] = useState<string>("");
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const keyRef = useRef(newIdempotencyKey());

  useEffect(() => onConfirmOpen(confirm), [confirm, onConfirmOpen]);

  const step = q.data;
  const canSubmit = !!decision && evidence.trim().length > 0 && reason.trim().length > 0;

  const submit = async () => {
    if (!step) return;
    setError(null);
    setConflict(null);
    try {
      await opsApi.reconcile(step.step_execution_id, { expected_step_version: step.step_version, decision, evidence_reference: evidence.trim(), reason: reason.trim() }, keyRef.current);
      await qc.invalidateQueries();
      toast({ tone: "ok", text: "已提交核查结论" });
      onDone();
    } catch (e) {
      if (isApiError(e) && e.status === 409) {
        setConflict(e.current);
        await q.refetch();
      } else if (isApiError(e) && e.status === 403) {
        toast({ tone: "risk", text: "你的权限已变化，已关闭运营抽屉" });
        onDone();
      } else {
        setError(errorText(e));
      }
    } finally {
      setConfirm(false);
    }
  };

  if (q.isPending) return <div className="p-4"><Loading text="正在读取步骤状态" /></div>;
  if (q.error || !step) return <div className="p-4"><ErrorBar text={errorText(q.error) || "无法读取该步骤"} /></div>;

  return (
    <div className="p-4">
      <dl className="grid gap-x-3 gap-y-1 rounded-ctl bg-page px-3 py-2.5 text-[12px] sm:grid-cols-[auto_1fr]">
        <dt className="text-ink-3">步骤</dt>
        <dd className="text-ink">{step.step_name}</dd>
        <dt className="text-ink-3">当前状态</dt>
        <dd>
          <StatusPill tone="warn" icon="shield">
            {step.allowed_decisions.includes("known_response_local_failure") ? "模型已响应，本地处理失败" : "外部执行结果未知"}
          </StatusPill>
        </dd>
        <dt className="text-ink-3">请求键摘要</dt>
        <dd className="mono text-ink">{step.request_key_summary}</dd>
        <dt className="text-ink-3">外部任务</dt>
        <dd className="text-ink">{step.external_task_summary}</dd>
        <dt className="text-ink-3">提交时间</dt>
        <dd className="text-ink">{formatDateTime(step.submitted_at)}</dd>
        <dt className="text-ink-3">步骤版本</dt>
        <dd className="num text-ink">{step.step_version}</dd>
      </dl>

      <fieldset className="mt-3">
        <legend className="mb-1.5 text-[13px] font-medium text-ink">核查结论</legend>
        <div className="space-y-1.5">
          {DECISIONS.map((d) => {
            const allowed = step.allowed_decisions.includes(d.id);
            return (
              <label key={d.id} className={`flex cursor-pointer items-start gap-2 rounded-ctl border p-2.5 ${decision === d.id ? "opt-on" : "border-line"} ${allowed ? "" : "cursor-not-allowed opacity-60"}`}>
                <input type="radio" name="decision" className="mt-0.5" disabled={!allowed} checked={decision === d.id} onChange={() => setDecision(d.id)} />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] text-ink">{d.label}</span>
                  <span className="block text-[12px] text-ink-2">{d.meaning}</span>
                  {!allowed && <span className="mt-0.5 block text-[11px] text-ink-3">服务端当前不允许这个结论</span>}
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <label className="mt-3 block">
        <span className="label">依据引用（必填）</span>
        <input className="input mt-1" value={evidence} onChange={(e) => setEvidence(e.target.value)} placeholder="例如：供应商工单 TX-91c2 的对账截图编号" />
      </label>
      <label className="mt-3 block">
        <span className="label">处理理由（必填）</span>
        <textarea className="input mt-1" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>

      {error && <div className="mt-3"><ErrorBar text={error} /></div>}
      {conflict && (
        <div className="mt-3 rounded-ctl border border-warn-line bg-warn-bg p-3 text-[12px] text-warn">
          <p className="font-medium">这个步骤已被其他人核查</p>
          <p className="mt-1">你的输入仍然保留。请核对最新状态后重新选择结论再提交。</p>
        </div>
      )}

      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="btn btn-secondary" onClick={onDone}>
          取消
        </button>
        <button type="button" className="btn btn-primary" disabled={!canSubmit} onClick={() => setConfirm(true)} title={canSubmit ? undefined : "请选择结论并填写依据与理由"}>
          提交裁决
        </button>
      </div>

      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="确认提交核查结论"
        description="提交后会写入审计记录，且无法由系统自动撤销。"
        tone="risk"
        width={480}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(false)}>
              返回
            </button>
            <button type="button" className="btn btn-danger" onClick={() => void submit()} data-autofocus>
              确认提交
            </button>
          </>
        }
      >
        <dl className="grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-3">处理</dt>
          <dd className="text-ink">{shortId(runId)}</dd>
          <dt className="text-ink-3">步骤</dt>
          <dd className="text-ink">{step.step_name}</dd>
          <dt className="text-ink-3">结论</dt>
          <dd className="text-ink">{DECISIONS.find((d) => d.id === decision)?.label}</dd>
          <dt className="text-ink-3">依据</dt>
          <dd className="text-ink">{evidence}</dd>
        </dl>
      </Dialog>
    </div>
  );
}

function MigrateSection({ runId, onDone, onConfirmOpen }: { runId: string; onDone: () => void; onConfirmOpen: (v: boolean) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useMigrationOptions(runId, true);
  const [picked, setPicked] = useState("");
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef(newIdempotencyKey());

  useEffect(() => onConfirmOpen(confirm), [confirm, onConfirmOpen]);

  const opts = q.data;
  const canSubmit = !!picked && reason.trim().length > 0 && !!opts?.safe_now;

  if (q.isPending) return <div className="p-4"><Loading text="正在读取迁移选项" /></div>;
  if (q.error || !opts) return <div className="p-4"><ErrorBar text={errorText(q.error) || "无法读取迁移选项"} /></div>;

  return (
    <div className="p-4">
      <div className="rounded-ctl bg-page px-3 py-2.5 text-[12px]">
        <p className="text-ink-3">当前配置</p>
        <p className="mt-0.5 text-[13px] text-ink">
          {opts.current.label} · {opts.current.role}
        </p>
        <p className="mono mt-0.5 text-[11px] text-ink-3">{opts.current.fingerprint}</p>
        <p className="num mt-1 text-[11px] text-ink-3">执行轮次 {opts.execution_epoch}</p>
      </div>

      {!opts.safe_now && (
        <div className="mt-3">
          <ErrorBar tone="warn" text={opts.unsafe_reason ?? "当前不适合迁移，请稍后再试。"} />
        </div>
      )}

      <fieldset className="mt-3">
        <legend className="mb-1.5 text-[13px] font-medium text-ink">候选配置</legend>
        <div className="space-y-1.5">
          {opts.options.map((o) => {
            const disabled = !!o.disabled_reason || !opts.safe_now;
            return (
              <label key={o.fingerprint} className={`flex cursor-pointer items-start gap-2 rounded-ctl border p-2.5 ${picked === o.fingerprint ? "opt-on" : "border-line"} ${disabled ? "cursor-not-allowed opacity-60" : ""}`}>
                <input type="radio" name="candidate" className="mt-0.5" disabled={disabled} checked={picked === o.fingerprint} onChange={() => setPicked(o.fingerprint)} />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] text-ink">{o.label}</span>
                  <span className="mono block text-[11px] text-ink-3">{o.fingerprint}</span>
                  <span className="block text-[12px] text-ink-2">能力检查：{o.capability_check}</span>
                  <span className="block text-[12px] text-ink-2">
                    将失效步骤：{o.invalidated_steps.length ? o.invalidated_steps.join("、") : "无"} · 成本级别：{o.cost_level}
                  </span>
                  {o.disabled_reason && <span className="mt-0.5 block text-[11px] text-risk">{o.disabled_reason}</span>}
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <label className="mt-3 block">
        <span className="label">迁移原因（必填）</span>
        <textarea className="input mt-1" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {error && <div className="mt-3"><ErrorBar text={error} /></div>}

      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="btn btn-secondary" onClick={onDone}>
          取消
        </button>
        <button type="button" className="btn btn-primary" disabled={!canSubmit} onClick={() => setConfirm(true)}>
          提交迁移
        </button>
      </div>

      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="确认迁移模型配置"
        description="迁移只影响这次处理的后续步骤，已形成的交付快照不会被改写。"
        width={480}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-autofocus
              onClick={async () => {
                setError(null);
                try {
                  await opsApi.migrate(runId, { candidate_config_fingerprint: picked, expected_execution_epoch: opts.execution_epoch, reason: reason.trim() }, keyRef.current);
                  await qc.invalidateQueries();
                  toast({ tone: "ok", text: "已提交迁移，后续步骤按新配置执行" });
                  onDone();
                } catch (e) {
                  if (isApiError(e) && e.status === 409) {
                    setError("这次处理已推进到新一轮执行，请重新读取选项后再迁移。");
                    await q.refetch();
                  } else {
                    setError(errorText(e));
                  }
                } finally {
                  setConfirm(false);
                }
              }}
            >
              确认迁移
            </button>
          </>
        }
      />
    </div>
  );
}
