import { languageName } from "../../i18n/languages";
// 批次列表：一个 Run 一行，绝不按文件数或执行轮次拆成多行。
// 主行显示该次查询实际展示的快照，当前工作状态与上次导出失败分开呈现。
import { useRef, useState } from "react";
import type { ManagedRunRow, MatchedSource } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Menu, Popover } from "../../components/Overlay";
import { EmptyState, IssueCounts, Loading, StatusPill, formatDateTime, formatTime, runTone, shortId } from "../../components/Status";

import { CaseCheckbox, SelectionBar, type CaseSelection } from "./CaseSelection";

export interface RowActions {
  onDeleteCase: (row: ManagedRunRow) => void;
  onOpenReview: (row: ManagedRunRow, useWorking?: boolean) => void;
  onAskAssistant: (row: ManagedRunRow) => void;
  onOpenCase: (caseId: string) => void;
  onReprocess: (row: ManagedRunRow) => void;
  onCancel: (row: ManagedRunRow) => void;
  onRetryExport: (row: ManagedRunRow) => void;
  onViewDelivery: (row: ManagedRunRow) => void;
}

const DATE_LABEL: Record<string, string> = { exported: "导出时间", uploaded: "上传时间", initiated: "发起时间", activity: "最近活动", reviewed: "复核时间" };

export function RunList({
  items,
  selection,
  onBulkDelete,
  dateType,
  isPending,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  expandedId,
  onExpand,
  actions,
  emptyKind,
  chips,
  onClearFilter,
  onNewRun,
  renderDetails,
  narrow = false,
  sort,
  onSort,
}: {
  items: ManagedRunRow[];
  selection: CaseSelection;
  onBulkDelete: () => void;
  dateType: string;
  isPending: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  expandedId: string | null;
  onExpand: (runId: string | null) => void;
  actions: RowActions;
  emptyKind: "never" | "filtered" | "stale_index";
  chips?: { key: string; label: string }[];
  onClearFilter: () => void;
  onNewRun: () => void;
  renderDetails: (row: ManagedRunRow) => React.ReactNode;
  narrow?: boolean;
  sort?: string;
  onSort?: (key: string) => void;
}) {
  if (isPending) {
    return (
      <div className="flex justify-center py-12">
        <Loading text="正在读取处理记录" />
      </div>
    );
  }
  if (!items.length) {
    if (emptyKind === "stale_index") {
      return (
        <EmptyState
          icon="clock"
          title="摘要索引正在更新"
          description="仍可按单票编号、文件名等元数据查询；摘要内容暂时不可用，稍后会自动恢复。"
        />
      );
    }
    return emptyKind === "never" ? (
      <EmptyState
        icon="docs"
        title="还没有任何处理记录"
        description="上传同一票单据并发起一次处理后，这里会出现对应记录与交付文件。"
        action={
          <button type="button" className="btn btn-primary" onClick={onNewRun}>
            新建处理
          </button>
        }
      />
    ) : (
      <EmptyState
        icon="search"
        title="当前筛选没有匹配的记录"
        description={
          <>
            <span>以下条件正在生效：</span>
            <span className="mt-1.5 flex flex-wrap justify-center gap-1.5">
              {(chips ?? []).map((c) => (
                <span key={c.key} className="tag border-line bg-page text-[12px] text-ink-2">
                  {c.label}
                </span>
              ))}
            </span>
          </>
        }
        action={
          <button type="button" className="btn btn-secondary" onClick={onClearFilter}>
            清除筛选条件
          </button>
        }
      />
    );
  }

  // 宽度不足 1144 px 时改为一列批次卡，不隐藏风险数与历史标签
  if (narrow) {
    return (
      <div className="space-y-2">
        <SelectionBar items={items.map(row => ({caseId: row.case_id, name: row.case_name}))} selection={selection} onDelete={onBulkDelete} />
        {items.map((row) => (
          <RunCard key={row.run_id} selection={selection} row={row} dateType={dateType} expanded={expandedId === row.run_id} onExpand={onExpand} actions={actions} renderDetails={renderDetails} />
        ))}
        {hasNextPage && (
          <div className="py-3 text-center">
            <button type="button" className="btn btn-secondary btn-sm" onClick={onLoadMore} disabled={isFetchingNextPage}>
              {isFetchingNextPage ? "正在加载" : "加载更多记录"}
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <SelectionBar items={items.map(row => ({caseId: row.case_id, name: row.case_name}))} selection={selection} onDelete={onBulkDelete} />
      <table className="w-full table-fixed border-collapse">
        <caption className="sr-only">处理记录列表，一行一次处理</caption>
        <thead className="sticky top-0 z-[1] bg-page">
          <tr className="border-b border-line text-left text-[12px] text-ink-3">
            <th className={`${selection.enabled ? "w-[68px]" : "w-[44px]"} py-2.5 font-normal`} scope="col">
              <span className="sr-only">选择与展开</span>
            </th>
            <th className="w-[236px] px-2 py-2.5 font-normal" scope="col">单票与处理</th>
            <th className="w-[126px] px-2 py-2.5 font-normal" scope="col">任务与语言</th>
            <th className="w-[108px] px-2 py-2.5 font-normal" scope="col">文件数与类型</th>
            <th className="w-[112px] px-2 py-2.5 font-normal" scope="col">处理人</th>
            <th className="w-[148px] px-2 py-2.5 font-normal" scope="col">
              {onSort ? (
                <button type="button" className="inline-flex items-center gap-1 hover:text-ink" onClick={() => onSort(sort === "exported_desc" ? "initiated_asc" : "exported_desc")} aria-sort={sort?.startsWith("exported") ? "descending" : "ascending"}>
                  时间
                  <Icon name={sort === "initiated_asc" ? "chevron-down" : "arrow-down"} size={12} />
                  <span className="text-[11px] font-normal text-ink-3">{sort === "initiated_asc" ? "按发起升序" : "按导出降序"}</span>
                </button>
              ) : (
                "时间"
              )}
            </th>
            <th className="w-[100px] px-2 py-2.5 font-normal" scope="col">疑点</th>
            <th className="w-[138px] px-2 py-2.5 font-normal" scope="col">版本与状态</th>
            <th className="w-[136px] px-2 py-2.5 font-normal" scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <RunRow key={row.run_id} selection={selection} row={row} dateType={dateType} expanded={expandedId === row.run_id} onExpand={onExpand} actions={actions} renderDetails={renderDetails} />
          ))}
        </tbody>
      </table>
      {hasNextPage && (
        <div className="py-4 text-center">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onLoadMore} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? "正在加载" : "加载更多记录"}
          </button>
        </div>
      )}
    </div>
  );
}

function RunRow({
  row,
  selection,
  dateType,
  expanded,
  onExpand,
  actions,
  renderDetails,
}: {
  row: ManagedRunRow;
  selection: CaseSelection;
  dateType: string;
  expanded: boolean;
  onExpand: (id: string | null) => void;
  actions: RowActions;
  renderDetails: (row: ManagedRunRow) => React.ReactNode;
  narrow?: boolean;
  sort?: string;
  onSort?: (key: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);
  const tone = runTone(row.current_work_status);
  // 历史交付与当前工作版待复核是两件事，必须并列显示
  const historicalPending = row.has_successful_snapshot && row.working_version_differs === true;
  const exportFailed = row.latest_export_attempt_status === "failed_retryable" || row.latest_export_attempt_status === "failed_final";
  // 时间列跟随当前所选日期类型，避免口径与筛选不一致
  const shownTime = dateType === "exported" ? row.displayed_snapshot_at ?? row.last_activity_at : dateType === "initiated" ? row.initiated_at : row.last_activity_at;

  return (
    <>
      <tr
        className={`min-h-[72px] border-b border-line align-top ${expanded ? "bg-page" : "hover:bg-[#f8f9fa]"}`}
        onClick={(e) => {
          // 点行空白处等同展开／收起；点按钮与链接不触发
          if ((e.target as HTMLElement).closest("button,a,select,input")) return;
          onExpand(expanded ? null : row.run_id);
        }}
      >
        <td className="py-3 pl-1">
          <div className="flex items-center gap-1">
          <CaseCheckbox item={{caseId: row.case_id, name: row.case_name}} selection={selection} />
          <button
            type="button"
            className="btn btn-icon btn-ghost h-10 w-10"
            aria-expanded={expanded}
            aria-label={expanded ? "收起本次处理" : "展开本次处理"}
            onClick={() => onExpand(expanded ? null : row.run_id)}
          >
            <Icon name={expanded ? "chevron-down" : "chevron-right"} size={16} />
          </button>
          </div>
        </td>
        <td className="px-2 py-3">
          <button type="button" className="block max-w-full text-left text-[13px] font-medium text-ink underline-offset-2 hover:text-brand-ink hover:underline" onClick={() => actions.onOpenCase(row.case_id)}>
            <span className="line-clamp-2">{row.case_name}</span>
          </button>
          <p className="mt-0.5 text-[11px] text-ink-3">
            处理 {shortId(row.run_id)} · {row.case_short_code}
          </p>
        </td>
        <td className="px-2 py-3 text-[12px] text-ink-2">
          <p>{row.workflow_type === "read_index" ? "识别与索引" : row.workflow_type === "consignment_check" ? "同票核对" : "翻译与校验"}</p>
          <p className="mt-0.5 text-ink-3">{row.workflow_type !== "translate_review" ? "无译文" : languageName(row.target_language)}</p>
        </td>
        <td className="px-2 py-3 text-[12px] text-ink-2">
          <p className="num">{row.source_count} 份</p>
          <p className="mt-0.5 truncate text-ink-3" title={row.source_types.join("、")}>
            {row.source_types.slice(0, 2).join("、")}
            {row.source_types.length > 2 ? `，另有 ${row.source_types.length - 2} 类` : ""}
          </p>
        </td>
        <td className="px-2 py-3 text-[12px] text-ink-2">
          <p className="truncate" title={row.initiated_by}>
            {row.initiated_by}
          </p>
          <p className="mt-0.5 text-ink-3">发起人</p>
        </td>
        <td className="px-2 py-3 text-[12px] text-ink-2">
          <span title={formatDateTime(shownTime)}>{formatTime(shownTime)}</span>
          <p className="mt-0.5 text-ink-3">{DATE_LABEL[dateType] ?? "最近活动"}</p>
        </td>
        <td className="whitespace-nowrap px-2 py-3">
          <IssueCounts rulesStatus={row.rules_status} blocking={row.issue_counts.blocking} warning={row.issue_counts.warning} undetermined={row.issue_counts.undetermined} />
        </td>
        <td className="px-2 py-3">
          <div className="flex flex-col items-start gap-1">
            <StatusPill tone={tone.tone} icon={tone.icon} busy={tone.busy}>
              {tone.text}
            </StatusPill>
            {row.has_successful_snapshot && (
              <span className="text-[11px] text-ok">
                <Icon name="check" size={12} className="mr-0.5 inline align-[-1px]" />
                有成功交付
              </span>
            )}
            {historicalPending && (
              <span className="rounded bg-hist-bg px-1.5 py-0.5 text-[11px] text-hist">
                历史交付；当前版待复核
                <button type="button" className="ml-1 underline" onClick={() => actions.onOpenReview(row, true)}>
                  查看当前版
                </button>
              </span>
            )}
            {exportFailed && (
              <button type="button" className="rounded bg-risk-bg px-1.5 py-0.5 text-left text-[11px] text-risk underline" onClick={() => actions.onRetryExport(row)} title={row.latest_export_attempt_error_summary ?? undefined}>
                上次导出失败，可按服务端许可重试
                {row.latest_export_attempt_id ? `（尝试 ${shortId(row.latest_export_attempt_id)}）` : ""}
              </button>
            )}
          </div>
        </td>
        <td className="px-2 py-3">
          <div className="flex items-center gap-0.5">
            <button type="button" className="btn btn-ghost h-10 px-2.5 text-[13px]" onClick={() => actions.onOpenReview(row)}>
              查看本次
            </button>
            <button ref={menuRef} type="button" className="btn btn-icon btn-ghost" onClick={() => setMenuOpen(true)} aria-label={`${row.case_name} 更多操作`}>
              <Icon name="more" size={16} />
            </button>
            <Menu
              open={menuOpen}
              onClose={() => setMenuOpen(false)}
              anchor={menuRef}
              label="处理记录操作"
              width={220}
              items={[
                { label: "删除整票", icon: "close", tone: "risk", onSelect: () => actions.onDeleteCase(row), separatorBefore: true },
                { label: "向助手询问本次", icon: "logo", onSelect: () => actions.onAskAssistant(row) },
                ...(row.has_successful_snapshot ? [{ label: "查看交付", icon: "download" as const, onSelect: () => actions.onViewDelivery(row) }] : []),
                { label: "重新处理", icon: "retry", onSelect: () => actions.onReprocess(row), separatorBefore: true },
                ...(row.allowed_actions.includes("cancel") || row.allowed_actions.includes("request_cancel")
                  ? [{ label: row.allowed_actions.includes("cancel") ? "取消处理" : "请求取消", icon: "close" as const, tone: "risk" as const, onSelect: () => actions.onCancel(row) }]
                  : [{ label: "取消处理", icon: "close" as const, onSelect: () => undefined, disabled: true, disabledReason: row.disabled_reasons.cancel ?? "当前状态无需取消" }]),
              ]}
            />
          </div>
          {row.matched_sources.length > 0 && <MatchExplain sources={row.matched_sources} />}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-line bg-[#f8f9fa]">
          <td colSpan={9} className="px-4 py-3">
            {renderDetails(row)}
          </td>
        </tr>
      )}
    </>
  );
}

/** 窄屏批次卡：顺序为 Case／Run → 状态与版本 → 时间与处理人 → 文件与疑点 → 操作。 */
function RunCard({
  row,
  selection,
  dateType,
  expanded,
  onExpand,
  actions,
  renderDetails,
}: {
  row: ManagedRunRow;
  selection: CaseSelection;
  dateType: string;
  expanded: boolean;
  onExpand: (id: string | null) => void;
  actions: RowActions;
  renderDetails: (row: ManagedRunRow) => React.ReactNode;
}) {
  const tone = runTone(row.current_work_status);
  const historicalPending = row.has_successful_snapshot && row.working_version_differs === true;
  const exportFailed = row.latest_export_attempt_status === "failed_retryable" || row.latest_export_attempt_status === "failed_final";
  const shownTime = dateType === "exported" ? row.displayed_snapshot_at ?? row.last_activity_at : dateType === "initiated" ? row.initiated_at : row.last_activity_at;
  return (
    <article className="card p-3">
      {selection.enabled && <div className="mb-2 flex items-center gap-2"><CaseCheckbox item={{caseId: row.case_id, name: row.case_name}} selection={selection} /><span className="text-[11px] text-ink-3">选择整票</span></div>}
      <button type="button" className="group block w-full text-left" onClick={() => actions.onOpenCase(row.case_id)}>
        <span className="text-[14px] font-medium text-ink underline-offset-2 group-hover:text-brand-ink group-hover:underline">{row.case_name}</span>
      </button>
      <p className="mt-0.5 text-[11px] text-ink-3">
        处理 {shortId(row.run_id)} · {row.workflow_type === "read_index" ? "识别与索引（无译文）" : row.workflow_type === "consignment_check" ? "同票核对（无译文）" : `翻译与校验 · ${languageName(row.target_language)}`}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={tone.tone} icon={tone.icon} busy={tone.busy}>
          {tone.text}
        </StatusPill>
        {row.has_successful_snapshot && <StatusPill tone="ok" icon="check">有成功交付</StatusPill>}
        {historicalPending && <span className="rounded bg-hist-bg px-1.5 py-0.5 text-[11px] text-hist">历史交付；当前版待复核</span>}
        {exportFailed && <span className="rounded bg-risk-bg px-1.5 py-0.5 text-[11px] text-risk">上次导出失败</span>}
      </div>
      <p className="mt-1.5 text-[12px] text-ink-2">
        {formatTime(shownTime)} · {row.initiated_by}（发起人）
      </p>
      <p className="mt-0.5 text-[12px] text-ink-2">
        <span className="num">{row.source_count}</span> 份原件 · {row.source_types.slice(0, 2).join("、")}
      </p>
      <div className="mt-1.5">
        <IssueCounts rulesStatus={row.rules_status} blocking={row.issue_counts.blocking} warning={row.issue_counts.warning} undetermined={row.issue_counts.undetermined} />
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => onExpand(expanded ? null : row.run_id)} aria-expanded={expanded}>
          {expanded ? "收起详情" : "展开详情"}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => actions.onOpenReview(row)}>
          查看本次
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => actions.onAskAssistant(row)}>
          询问本次
        </button>
        <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={() => actions.onDeleteCase(row)}>删除整票</button>
        {historicalPending && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => actions.onOpenReview(row, true)}>
            查看当前版
          </button>
        )}
      </div>
      {expanded && <div className="mt-3 border-t border-line pt-3">{renderDetails(row)}</div>}
    </article>
  );
}

/** 命中说明：逐条件列出实际命中的文件、逻辑单据与字段角色，不把 A/B 文件拼成一条。 */
function MatchExplain({ sources }: { sources: MatchedSource[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={ref} type="button" className="btn btn-link btn-sm mt-1 px-0 text-[11px]" onClick={() => setOpen(true)}>
        为何命中（{sources.length}）
      </button>
      <Popover open={open} onClose={() => setOpen(false)} anchor={ref} width={340} label="命中说明" align="end">
        <div className="p-3">
          <p className="mb-1.5 text-[12px] font-medium text-ink">这一行是这样被命中的</p>
          <ul className="space-y-2">
            {sources.map((s, i) => (
              <li key={i} className="rounded-ctl border border-line p-2 text-[12px]">
                <p className="text-ink-3">{s.condition}</p>
                <p className="mt-0.5 truncate text-ink" title={s.filename}>
                  {s.filename} · v{s.document_version}
                </p>
                <p className="text-ink-2">
                  {s.logical_document}
                  {s.field_role ? ` · ${s.field_role}` : ""}
                </p>
                <StatusPill tone={s.state === "confirmed" ? "ok" : "warn"} icon={s.state === "confirmed" ? "check" : "warn"}>
                  {s.state === "confirmed" ? "已确认值" : "识别候选／待复核"}
                </StatusPill>
              </li>
            ))}
          </ul>
        </div>
      </Popover>
    </>
  );
}
