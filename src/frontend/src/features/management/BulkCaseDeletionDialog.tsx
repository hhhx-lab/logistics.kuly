import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { errorText, isApiError, newIdempotencyKey } from "../../api/client";
import { managementApi } from "../../api/management";
import { Dialog } from "../../components/Overlay";
import { ErrorBar } from "../../components/Status";
import type { SelectableCase } from "./CaseSelection";

type Preview = Awaited<ReturnType<typeof managementApi.deletionPreview>>;
type Item = SelectableCase & { preview?: Preview; error?: string; key: string; uncertain?: boolean; deletionId?: string };

export function BulkCaseDeletionDialog({ cases, onClose, onQueued }: { cases: SelectableCase[]; onClose: () => void; onQueued: (caseId: string, deletionId: string) => void }) {
  const [items, setItems] = useState<Item[]>(() => cases.map(item => ({ ...item, key: newIdempotencyKey() })));
  const latest = useRef(items); latest.current = items;
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const qc = useQueryClient();
  const count = items.filter(item => !item.deletionId).length;
  const phrase = `删除${count}票`;
  const canSubmit = count > 0 && !loading && !busy && input.trim() === phrase && items.every(item => item.deletionId || (item.preview?.allowed && (!item.error || item.uncertain)));
  const update = (caseId: string, patch: Partial<Item>) => setItems(old => old.map(item => item.caseId === caseId ? { ...item, ...patch } : item));

  const read = async (item: Item) => {
    try { update(item.caseId, { preview: await managementApi.deletionPreview(item.caseId), error: undefined, key: newIdempotencyKey(), uncertain: false }); }
    catch (e) { update(item.caseId, { error: errorText(e) }); }
  };
  useEffect(() => {
    let active = true;
    const pending = [...latest.current];
    // 预览仅限已选项，有界并发；身份切换由全局请求取消保护。
    const readInitial = async () => {
      while (active && pending.length) {
        const item = pending.shift()!;
        try { const preview = await managementApi.deletionPreview(item.caseId); if (active) update(item.caseId, { preview }); }
        catch (e) { if (active) update(item.caseId, { error: errorText(e) }); }
      }
    };
    void Promise.all(Array.from({ length: Math.min(3, pending.length) }, readInitial)).then(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true); setInput("");
    try {
      for (const item of latest.current.filter(value => !value.deletionId)) {
        if (!item.preview) continue;
        try {
          const receipt = await managementApi.deleteCase(item.caseId, { expected_case_version: item.preview.case_version, confirm_short_code: item.preview.short_code }, item.key);
          update(item.caseId, { deletionId: receipt.deletion_id, error: undefined });
          qc.removeQueries({ predicate: query => JSON.stringify(query.queryKey).includes(item.caseId) });
          onQueued(item.caseId, receipt.deletion_id);
          await qc.invalidateQueries();
        } catch (e) {
          if (isApiError(e) && e.status === 409) {
            await read(item);
            update(item.caseId, { error: "单票已变化，请重新读取清单、核对后确认。" });
          } else {
            // 网络/服务端故障可能已经提交，保留原幂等键与原确认参数。
            const uncertain = !isApiError(e) || e.status >= 500;
            update(item.caseId, { error: errorText(e), uncertain });
          }
        }
      }
      await qc.invalidateQueries();
    } finally { setBusy(false); }
  };
  return <Dialog open title="批量删除整票" tone="risk" width={640} onClose={() => { if (!busy) onClose(); }} footer={<>
    <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>{count === 0 ? "完成" : "返回"}</button>
    {count > 0 && <button type="button" className="btn btn-danger" disabled={!canSubmit} onClick={() => void submit()}>{busy ? "正在逐票提交" : "确认删除所选整票"}</button>}
  </>}>
    <p className="mb-3 rounded-ctl border border-risk-line bg-risk-bg p-3 text-[13px] text-risk">将删除所选整票全部原件、草稿、历次处理和交付文件，无法恢复。提交成功后立即不可访问，物理清理在后台进行；其他票不受影响。</p>
    <ul className="max-h-80 overflow-auto scroll-thin rounded-ctl border border-line divide-y divide-line" aria-label="待删除整票清单">
      {items.map(item => <li key={item.caseId} className="p-3 text-[12px]">
        <p className="font-medium text-ink">{item.name} {item.preview && <span className="mono text-ink-3">· {item.preview.short_code}</span>}</p>
        {item.deletionId ? <p className="mt-1 text-ok">删除请求已提交，正在后台清理</p> : <>
          {item.preview && <p className="mt-1 text-ink-2">原件 {item.preview.source_count} 份 · 处理 {item.preview.run_count} 次 · 成功快照 {item.preview.snapshot_count} 份</p>}
          {!item.preview && !item.error && <p className="mt-1 text-ink-3">正在读取清单…</p>}
          {item.preview && !item.preview.allowed && <p className="mt-1 text-risk">{item.preview.disabled_reason}</p>}
          {item.error && <div className="mt-2"><ErrorBar text={item.uncertain ? `${item.error}；提交结果尚未确认，重新确认后使用原请求获取回执，不会重复创建删除。` : item.error} />{!item.uncertain && <button type="button" className="btn btn-ghost btn-sm" disabled={busy || loading} onClick={() => { setInput(""); void read(item); }}>重新读取清单</button>}</div>}
        </>}
      </li>)}
    </ul>
    <p className="mt-3 text-[11px] leading-relaxed text-ink-3">主存清理目标24小时内完成，加密备份最长7天轮换删除。已下载/外发副本无法远程收回；第三方服务保存情况以上传前数据处理说明为准。</p>
    {count > 0 && <label className="mt-3 block text-[12px] text-ink-2">请输入“{phrase}”以确认<input className="input mt-1" value={input} disabled={busy || loading} onChange={event => setInput(event.target.value)} autoComplete="off" /></label>}
  </Dialog>;
}
