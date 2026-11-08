import { CapabilityMarketLink } from "../../components/CapabilityMarketLink";
import { englishError, UiText } from "../../i18n/runtime";
import { useInterfaceText } from "../settings/useInterfaceText";
// Skill 与 MCP 仓库。行身点击只看详情，行尾开关才改启用意图；
// 内置路由只读，不渲染编辑／停用／归档控件。
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorText } from "../../api/client";
import type { ExtensionRow, ExtensionRowState } from "../../api/types";
import { Icon, type IconName } from "../../components/Icon";
import { Dialog, Menu, useToast } from "../../components/Overlay";
import { EmptyState, ErrorBar, StatusPill, formatDateTime, formatTime } from "../../components/Status";
import { AppShell } from "../shell/AppShell";
import { McpWizard, ToolGrantList } from "./McpWizard";
import { EMPTY_SKILL, ImportPanel, SkillEditor, type SkillDraft } from "./SkillEditor";
import { extensionsApi, newKey, useCalls, useExtensionCommand, useExtensionList, useMcp, useSkill, type KindFilter, type SortKey, type StatusFilter } from "./hooks";

/** 行状态文案：一律「图标＋文字」，并区分「校验通过待启用」与「可用」。 */
const ROW_STATE: Record<ExtensionRowState, { text: string; tone: "ok" | "warn" | "risk" | "info" | "dis"; icon: IconName }> = {
  enabled: { text: "可用", tone: "ok", icon: "check" },
  syncing: { text: "正在同步可用状态", tone: "info", icon: "clock" },
  ready_to_enable: { text: "已校验，待启用", tone: "info", icon: "info" },
  draft: { text: "草稿", tone: "dis", icon: "info" },
  validating: { text: "验证中", tone: "info", icon: "clock" },
  disabled: { text: "已停用", tone: "dis", icon: "close" },
  archived: { text: "已归档", tone: "dis", icon: "info" },
  validation_failed: { text: "验证失败", tone: "risk", icon: "alert" },
  connect_failed: { text: "连接失败", tone: "risk", icon: "alert" },
  needs_retest: { text: "待测试", tone: "warn", icon: "warn" },
  needs_recheck: { text: "待复核", tone: "warn", icon: "warn" },
};

/** 默认分组顺序：内置路由 → 可用自建 → 可用 MCP → 未启用／异常。 */
const GROUPS = [
  { id: "builtin", title: "内置路由" },
  { id: "skill", title: "可用自建 Skill" },
  { id: "mcp", title: "可用 MCP 连接" },
  { id: "other", title: "未启用与异常" },
] as const;

function groupOf(r: ExtensionRow): (typeof GROUPS)[number]["id"] {
  if (r.kind === "builtin_skill") return "builtin";
  if (r.row_state !== "enabled") return "other";
  return r.kind === "mcp" ? "mcp" : "skill";
}

export function ExtensionsPage() {
  const uiText = useInterfaceText();
  const [kind, setKind] = useState<KindFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("grouped");
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [skillDraft, setSkillDraft] = useState<SkillDraft | null>(null);
  const [mcpWizard, setMcpWizard] = useState<{ connectionId: string | null } | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => window.innerWidth < 1024);
  // 关闭详情后把焦点还给原行
  const rowRefs = useRef(new Map<string, HTMLButtonElement | null>());

  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth < 1024);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const list = useExtensionList(kind, status, q);
  // 详情只存 ID，行数据从列表派生，条目消失时侧板自动关闭
  const selected = useMemo(() => list.items.find((r) => r.extension_id === selectedId) ?? null, [list.items, selectedId]);
  useEffect(() => {
    if (selectedId && !list.isPending && !selected) setSelectedId(null);
  }, [selectedId, selected, list.isPending]);

  const closeDetail = () => {
    const id = selectedId;
    setSelectedId(null);
    if (id) requestAnimationFrame(() => rowRefs.current.get(id)?.focus({ preventScroll: true }));
  };

  const sorted = useMemo(() => {
    const rank = (r: ExtensionRow) => GROUPS.findIndex((g) => g.id === groupOf(r));
    return [...list.items].sort((a, b) => (sort === "updated" ? b.updated_at.localeCompare(a.updated_at) : rank(a) - rank(b) || b.updated_at.localeCompare(a.updated_at)));
  }, [list.items, sort]);

  const filtering = !!q.trim() || status !== "all" || kind !== "all";
  const activeChips = [kind !== "all" && `类型：${kind === "skill" ? "Skill" : "MCP"}`, status !== "all" && `状态：${status === "available" ? "可用" : status === "inactive" ? "未启用" : "异常"}`, q.trim() && `搜索「${q.trim()}」`].filter(Boolean) as string[];
  const clearAll = () => {
    setQ("");
    setStatus("all");
    setKind("all");
  };

  const rowCard = (r: ExtensionRow) => (
    <ExtensionRowCard
      key={r.extension_id}
      row={r}
      selected={selectedId === r.extension_id}
      compact={!!selected}
      registerRef={(el) => rowRefs.current.set(r.extension_id, el)}
      onSelect={() => setSelectedId(r.extension_id)}
      onEditSkill={(d) => setSkillDraft(d)}
      onUpdateMcp={(id) => setMcpWizard({ connectionId: id })}
    />
  );

  return (
    <AppShell>
      <div className="flex min-h-0 flex-1">
        <div className={`flex min-w-0 flex-1 flex-col ${narrow && selected ? "hidden" : ""}`}>
          <header className="mx-auto flex w-full max-w-[1144px] min-h-16 shrink-0 flex-wrap items-center gap-3 px-8 pt-4">
            <div className="min-w-0 flex-1">
              <h1 className="text-[24px] font-semibold leading-tight text-ink">Skill 与 MCP</h1>
              <p className="mt-0.5 text-[13px] text-ink-3">{uiText("为对话添加可选辅助能力", "Add optional capabilities to your conversations")}</p>
            </div>
            <button type="button" className="btn btn-outline btn-lg" onClick={() => setImportOpen(true)}>
              导入
            </button>
            <button type="button" className="btn btn-secondary btn-lg" onClick={() => setMcpWizard({ connectionId: null })}>
              新增 MCP 连接
            </button>
            <button type="button" className="btn btn-primary btn-lg" onClick={() => setSkillDraft(EMPTY_SKILL)}>
              <Icon name="plus" size={16} />
              新建 Skill
            </button>
          </header>

          <CapabilityMarketLink />
          <div className="mx-auto mt-6 flex w-full max-w-[1144px] h-12 shrink-0 flex-wrap items-center gap-2 px-8">
            <div className="flex items-center rounded-ctl border border-line p-0.5" role="group" aria-label="类型">
              {([["all", "全部"], ["skill", "Skill"], ["mcp", "MCP"]] as const).map(([id, label]) => (
                <button key={id} type="button" className={`btn btn-sm h-10 min-w-[56px] ${kind === id ? "btn-secondary" : "btn-ghost"}`} onClick={() => setKind(id)} aria-pressed={kind === id}>
                  {label}
                </button>
              ))}
            </div>
            <label className="relative w-[280px] max-w-full">
              <Icon name="search" size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
              <input className="input h-10 pl-8" placeholder="搜索名称或用途" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜索扩展" />
            </label>
            <select className="input h-10 w-[124px]" value={status} onChange={(e) => setStatus(e.target.value as StatusFilter)} aria-label="状态筛选">
              <option value="all">全部状态</option>
              <option value="available">可用</option>
              <option value="inactive">未启用</option>
              <option value="abnormal">异常</option>
            </select>
            <select className="input h-10 w-[132px]" value={sort} onChange={(e) => setSort(e.target.value as SortKey)} aria-label="排序方式">
              <option value="grouped">默认分组</option>
              <option value="updated">按最近更新</option>
            </select>
            <span className="flex-1" />
            {!list.isPending && !list.isError && (
              <span className="text-[12px] text-ink-2">
                共 <span className="num font-medium text-ink">{sorted.length}</span> 项
              </span>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-auto scroll-thin px-8 py-4">
            {list.data?.warnings?.map((warning) => <p key={warning.code} role="status" className="mb-3 rounded-ctl bg-warn-bg px-3 py-2 text-[12px] text-warn">{warning.message}</p>)}
            <div className="mx-auto max-w-[1144px] space-y-2">
              {list.isPending && [0, 1, 2, 3].map((i) => <div key={i} className="card min-h-[72px] animate-pulse bg-page" aria-hidden />)}
              {list.isPending && <p className="sr-only">正在读取能力列表</p>}

              {list.isError && (
                <ErrorBar text={errorText(list.error)} action={{ label: "重试", onSelect: () => void list.refetch() }} />
              )}

              {!list.isPending && !list.isError && !sorted.length && (
                <EmptyState
                  icon="puzzle"
                  title={filtering ? "当前筛选没有匹配的能力" : "尚无自建能力"}
                  description={filtering ? `已生效条件：${activeChips.join("；")}。清除后可看到全部有权条目。` : "内置任务路由按服务配置与权限显示可用状态；你可以新建 Skill 或连接已有的 MCP 服务。"}
                  action={
                    filtering ? (
                      <button type="button" className="btn btn-secondary" onClick={clearAll}>
                        清除筛选
                      </button>
                    ) : undefined
                  }
                />
              )}

              {!list.isPending &&
                !list.isError &&
                sorted.length > 0 &&
                (sort === "updated"
                  ? sorted.map(rowCard)
                  : GROUPS.map((g) => {
                      const rows = sorted.filter((r) => groupOf(r) === g.id);
                      if (!rows.length) return null;
                      const isCollapsed = collapsed.includes(g.id);
                      return (
                        <section key={g.id} className="space-y-2">
                          <button
                            type="button"
                            className="flex h-9 w-full items-center gap-1.5 text-left text-[12px] font-medium text-ink-2"
                            onClick={() => setCollapsed((p) => (p.includes(g.id) ? p.filter((x) => x !== g.id) : [...p, g.id]))}
                            aria-expanded={!isCollapsed}
                          >
                            <Icon name={isCollapsed ? "chevron-right" : "chevron-down"} size={14} />
                            <UiText value={g.title} />
                            <span className="num text-ink-3">{rows.length}</span>
                          </button>
                          {!isCollapsed && rows.map(rowCard)}
                        </section>
                      );
                    }))}

            </div>
          </div>
        </div>

        {selected && (
          <DetailPanel
            row={selected}
            fullscreen={narrow}
            onClose={closeDetail}
            onEditSkill={(d) => setSkillDraft(d)}
            onUpdateMcp={(id) => setMcpWizard({ connectionId: id })}
          />
        )}
      </div>

      {skillDraft && <SkillEditor initial={skillDraft} onClose={() => setSkillDraft(null)} />}
      {mcpWizard && <McpWizard initialConnectionId={mcpWizard.connectionId} onClose={() => setMcpWizard(null)} />}
      {importOpen && <ImportPanel onClose={() => setImportOpen(false)} onDraft={(d) => { setImportOpen(false); setSkillDraft(d); }} onMcpDraft={(id) => { setImportOpen(false); setMcpWizard({ connectionId: id }); }} />}
    </AppShell>
  );
}

function kindIcon(kind: ExtensionRow["kind"]): IconName {
  return kind === "mcp" ? "external" : kind === "builtin_skill" ? "flow" : "doc";
}

function ExtensionRowCard({
  row,
  selected,
  compact,
  registerRef,
  onSelect,
  onEditSkill,
  onUpdateMcp,
}: {
  row: ExtensionRow;
  selected: boolean;
  compact: boolean;
  registerRef: (el: HTMLButtonElement | null) => void;
  onSelect: () => void;
  onEditSkill: (d: SkillDraft) => void;
  onUpdateMcp: (id: string) => void;
}) {
  const toast = useToast();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);
  const [confirm, setConfirm] = useState<"disable" | "archive" | null>(null);
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const st = ROW_STATE[row.row_state];
  const builtin = row.kind === "builtin_skill";
  const enabled = row.row_state === "enabled";

  const disable = useExtensionCommand<void, unknown>((_a, key) => (row.kind === "mcp" ? extensionsApi.disableMcp(row.extension_id, key) : extensionsApi.disableSkill(row.extension_id, key)));
  const archive = useExtensionCommand<void, unknown>((_a, key) => (row.kind === "mcp" ? extensionsApi.archiveMcp(row.extension_id, key) : extensionsApi.archiveSkill(row.extension_id, key)));
  const reenable = useExtensionCommand<void, unknown>(async (_a, key) => {
    // 重新启用必须再走一次有效校验，不能靠旧状态直接恢复
    const data = await extensionsApi.getSkill(row.extension_id);
    const target = [...data.versions].reverse().find((v) => v.ever_enabled) ?? data.versions[data.versions.length - 1];
    const validated = await extensionsApi.validateSkill(row.extension_id, target.version, newKey());
    if (validated.status !== "available") throw new Error("重新校验未通过，请先在编辑中修正后再启用");
    return extensionsApi.enableSkill(row.extension_id, target.version, key);
  });

  const openSkillDraft = async () => {
    setRowError(null);
    setBusy(true);
    try {
      const data = await extensionsApi.getSkill(row.extension_id);
      // 已存在未启用草稿时继续编辑它；否则基于启用版新建版本，绝不原位改曾启用的版本
      const draft = [...data.versions].reverse().find((v) => !v.ever_enabled);
      const v = draft ?? (await extensionsApi.newSkillVersion(row.extension_id, newKey()));
      onEditSkill({ skillId: row.extension_id, version: v.version, data: stripVersion(v), hasEnabledVersion: data.enabled_version !== null && data.enabled_version !== undefined });
    } catch (e) {
      setRowError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className={`card card-hover flex min-h-[72px] items-start gap-3 p-3 ${selected ? "opt-on" : ""}`}>
        <button ref={registerRef} type="button" className="flex min-w-0 flex-1 items-start gap-3 text-left" onClick={onSelect}>
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-ctl bg-page text-ink-2">
            <Icon name={kindIcon(row.kind)} size={18} />
          </span>
          <span className="min-w-0 flex-1 md:min-w-[280px]">
            <span className="flex flex-wrap items-center gap-1.5">
              <span className="text-[14px] font-medium text-ink">{row.kind === "builtin_skill" ? <UiText value={row.name} /> : row.name}</span>
            </span>
            <span className="mt-0.5 block truncate text-[12px] text-ink-2" title={row.summary}>
              {row.kind === "builtin_skill" ? <UiText value={row.summary} /> : row.summary}
            </span>
          </span>
        </button>

        {/* 来源与版本 */}
        {!compact && (
          <div className="w-[120px] shrink-0 text-[12px]">
            <p className="text-ink-2"><UiText value={row.source_label} /></p>
            <p className="num mt-0.5 text-ink-3">v{row.version}</p>
            {row.transport_label && <p className="mt-0.5 truncate text-[11px] text-ink-3" title={row.transport_label}>{row.transport_label}</p>}
          </div>
        )}

        {/* 权限或工具数 */}
        {!compact && (
          <div className="w-[180px] shrink-0 text-[12px] text-ink-2">
            <p className="line-clamp-2" title={row.permission}>
              <UiText value={row.permission} />
            </p>
            <p className="mt-0.5 text-ink-3">{row.kind === "builtin_skill" ? <UiText value={row.owner} /> : row.owner}</p>
          </div>
        )}

        {/* 状态与最近测试时间 */}
        <div className="w-[144px] shrink-0">
          <StatusPill tone={st.tone} icon={st.icon}>
            {st.text}
          </StatusPill>
          <p className="mt-1 text-[11px] text-ink-3">{row.tested_at ? `测试于 ${formatTime(row.tested_at)}` : `更新于 ${formatTime(row.updated_at)}`}</p>
          {row.error_class && <p className="mt-0.5 text-[11px] text-risk">{row.error_class}</p>}
        </div>

        {/* 启停与更多 */}
        <div className="flex w-[120px] shrink-0 items-center justify-end gap-0.5">
          {builtin ? (
            <span className="text-[11px] text-ink-3">只读</span>
          ) : (
            <>
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                aria-label={`${row.name} ${enabled ? "已启用，点击停用" : "未启用，点击启用"}`}
                disabled={busy || reenable.isPending}
                className={`flex h-10 items-center gap-1.5 rounded-ctl px-1.5 text-[11px] ${enabled ? "text-ink-2" : "text-ink-3"}`}
                onClick={async () => {
                  setRowError(null);
                  if (enabled) return setConfirm("disable");
                  if (row.kind === "mcp") return onUpdateMcp(row.extension_id);
                  if (row.row_state === "draft" || row.row_state === "validation_failed") return void openSkillDraft();
                  reenable.mutate(undefined, {
                    onSuccess: () => toast({ tone: "ok", text: "已重新启用，候选同步后即可在对话中选择" }),
                    onError: (e) => setRowError(errorText(e)),
                  });
                }}
              >
                <span className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${enabled ? "bg-brand" : "bg-dis-bg"}`}>
                  <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all ${enabled ? "left-3.5" : "left-0.5"}`} />
                </span>
                {enabled ? "已启用" : "未启用"}
              </button>
              <button ref={menuRef} type="button" className="btn btn-icon btn-ghost h-10 w-10" onClick={() => setMenuOpen(true)} aria-label={`${row.name} 更多操作`}>
                <Icon name="more" size={16} />
              </button>
              <Menu
                open={menuOpen}
                onClose={() => setMenuOpen(false)}
                anchor={menuRef}
                label="能力操作"
                items={
                  row.kind === "mcp"
                    ? [
                        { label: "更新配置／重新测试", icon: "pencil", onSelect: () => onUpdateMcp(row.extension_id) },
                        ...(enabled
                          ? [{ label: "停用连接", icon: "close" as const, onSelect: () => setConfirm("disable") }]
                          : [{ label: "重新测试并启用", icon: "retry" as const, onSelect: () => onUpdateMcp(row.extension_id) }]),
                        { label: "归档", icon: "trash", tone: "risk", separatorBefore: true, onSelect: () => setConfirm("archive") },
                      ]
                    : [
                        { label: "编辑（新建版本草稿）", icon: "pencil", onSelect: () => void openSkillDraft() },
                        ...(enabled
                          ? [{ label: "停用", icon: "close" as const, onSelect: () => setConfirm("disable") }]
                          : [
                              {
                                label: "重新校验并启用",
                                icon: "retry" as const,
                                onSelect: () =>
                                  reenable.mutate(undefined, {
                                    onSuccess: () => toast({ tone: "ok", text: "已重新启用" }),
                                    onError: (e) => setRowError(errorText(e)),
                                  }),
                              },
                            ]),
                        { label: "归档", icon: "trash", tone: "risk", separatorBefore: true, onSelect: () => setConfirm("archive") },
                      ]
                }
              />
            </>
          )}
        </div>
      </div>
      {rowError && (
        <p className="rounded-ctl bg-risk-bg px-2.5 py-1.5 text-[12px] text-risk" role="status">
          {rowError}
        </p>
      )}

      <Dialog
        open={confirm === "disable"}
        onClose={() => setConfirm(null)}
        title={`停用「${row.name}」`}
        description="停用后立即阻断新的调用；对话输入里已选的标签会变成不可用，但你输入的问题文本会保留。"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() =>
                disable.mutate(undefined, {
                  onSuccess: () => {
                    setConfirm(null);
                    toast({ tone: "ok", text: "已停用，新的调用会被拒绝" });
                  },
                })
              }
            >
              停用
            </button>
          </>
        }
      >
        <p className="text-[12px] text-ink-2">重新启用需要再次通过有效校验（Skill）或重新测试与授权（MCP）。</p>
        {disable.error && <div className="mt-2"><ErrorBarLite text={errorText(disable.error)} /></div>}
      </Dialog>

      <Dialog
        open={confirm === "archive"}
        onClose={() => setConfirm(null)}
        title={`归档「${row.name}」`}
        description="归档会停止新的调用并从常规列表隐藏，但历史版本与调用审计仍然保留，不会被删除。"
        width={480}
        tone="risk"
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() =>
                archive.mutate(undefined, {
                  onSuccess: () => {
                    setConfirm(null);
                    toast({ tone: "ok", text: "已归档，历史调用记录仍可查询" });
                  },
                })
              }
            >
              确认归档
            </button>
          </>
        }
      >
        {row.kind === "mcp" ? (
          <div className="text-[12px] leading-relaxed text-ink-2">
            <p>归档会撤销这个连接下全部 {row.discovered_tool_count ?? 0} 个工具（其中 {row.granted_tool_count ?? 0} 个已授权）的可用状态。</p>
            <p className="mt-1">这个连接保存的个人凭证若不再被任何连接引用，将按凭证存储策略销毁；历史调用记录仍按审计规则保留。</p>
          </div>
        ) : (
          <p className="text-[12px] text-ink-2">历史版本与调用审计按留存规则保留，不会因归档被改写。</p>
        )}
        {archive.error && <div className="mt-2"><ErrorBarLite text={errorText(archive.error)} /></div>}
      </Dialog>
    </>
  );
}

function stripVersion(v: import("../../api/types").SkillVersion) {
  const { version: _v, status: _s, validated_at: _va, validation_errors: _ve, ever_enabled: _ee, ...rest } = v;
  void _v;
  void _s;
  void _va;
  void _ve;
  void _ee;
  return rest;
}

function ErrorBarLite({ text }: { text: string }) {
  return <p lang="en" className="rounded-ctl bg-risk-bg px-2.5 py-1.5 text-[12px] text-risk">{englishError(text)}</p>;
}

/** 详情侧板：说明、参数、授权与最近调用；不显示凭证与单证原文。 */
function DetailPanel({
  row,
  fullscreen,
  onClose,
  onEditSkill,
  onUpdateMcp,
}: {
  row: ExtensionRow;
  fullscreen: boolean;
  onClose: () => void;
  onEditSkill: (d: SkillDraft) => void;
  onUpdateMcp: (id: string) => void;
}) {
  const toast = useToast();
  const isMcp = row.kind === "mcp";
  const skill = useSkill(!isMcp ? row.extension_id : null);
  const mcp = useMcp(isMcp ? row.extension_id : null);
  const calls = useCalls(row.extension_id);
  const st = ROW_STATE[row.row_state];
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);
  const [confirm, setConfirm] = useState<"disable" | "archive" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const disable = useExtensionCommand<void, unknown>((_a, key) => (isMcp ? extensionsApi.disableMcp(row.extension_id, key) : extensionsApi.disableSkill(row.extension_id, key)));
  const archive = useExtensionCommand<void, unknown>((_a, key) => (isMcp ? extensionsApi.archiveMcp(row.extension_id, key) : extensionsApi.archiveSkill(row.extension_id, key)));
  const test = useExtensionCommand<void, unknown>(async (_a, key) => {
    const data = await extensionsApi.getMcp(row.extension_id);
    const latest = data.versions[data.versions.length - 1];
    return extensionsApi.testMcp(row.extension_id, latest.version, key);
  });
  const revoke = useExtensionCommand<string, unknown>((grantId, key) => extensionsApi.revokeGrant(grantId, key));

  useEffect(() => {
    // Esc 关闭详情，焦点由父级归还原行
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector('[role="dialog"][aria-modal="true"]')) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const skillVersion = skill.data?.versions.find((v) => v.version === (skill.data?.enabled_version ?? 0)) ?? skill.data?.versions[skill.data.versions.length - 1];
  const mcpVersion = mcp.data?.versions.find((v) => v.version === (mcp.data?.enabled_version ?? 0)) ?? mcp.data?.versions[mcp.data.versions.length - 1];

  const openSkillDraft = async () => {
    setError(null);
    try {
      const data = skill.data ?? (await extensionsApi.getSkill(row.extension_id));
      const draft = [...data.versions].reverse().find((v) => !v.ever_enabled);
      const v = draft ?? (await extensionsApi.newSkillVersion(row.extension_id, newKey()));
      onEditSkill({ skillId: row.extension_id, version: v.version, data: stripVersion(v), hasEnabledVersion: data.enabled_version !== null && data.enabled_version !== undefined });
    } catch (e) {
      setError(errorText(e));
    }
  };

  return (
    <aside
      className={fullscreen ? "fixed inset-0 z-panel flex flex-col bg-card" : "flex w-[440px] shrink-0 flex-col border-l border-line bg-card"}
      aria-label={`${row.name} 详情`}
    >
      <header className="flex h-bar shrink-0 items-center gap-2 border-b border-line px-4">
        {fullscreen && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            <Icon name="chevron-left" size={16} />
            返回仓库
          </button>
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-[14px] font-medium text-ink">{row.kind === "builtin_skill" ? <UiText value={row.name} /> : row.name}</p>
          <p className="text-[11px] text-ink-3">
            {row.kind === "builtin_skill" ? "内置 Skill" : row.kind === "mcp" ? "MCP 连接" : "自建 Skill"} · v{row.version}
          </p>
        </div>
        <StatusPill tone={st.tone} icon={st.icon}>
          {st.text}
        </StatusPill>
        {row.kind !== "builtin_skill" && (
          <>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => (isMcp ? onUpdateMcp(row.extension_id) : void openSkillDraft())}>
              {isMcp ? "更新配置" : "编辑"}
            </button>
            <button ref={menuRef} type="button" className="btn btn-icon btn-ghost h-10 w-10" onClick={() => setMenuOpen(true)} aria-label={`${row.name} 详情更多操作`}>
              <Icon name="more" size={16} />
            </button>
            <Menu
              open={menuOpen}
              onClose={() => setMenuOpen(false)}
              anchor={menuRef}
              label="能力操作"
              items={[
                ...(isMcp ? [{ label: "测试连接", icon: "retry" as const, onSelect: () => test.mutate(undefined, { onSuccess: () => toast({ tone: "ok", text: "已完成一次连接测试" }), onError: (e) => setError(errorText(e)) }) }] : []),
                ...(row.row_state === "enabled" ? [{ label: isMcp ? "停用连接" : "停用", icon: "close" as const, onSelect: () => setConfirm("disable") }] : []),
                { label: "归档", icon: "trash" as const, tone: "risk" as const, separatorBefore: true, onSelect: () => setConfirm("archive") },
              ]}
            />
          </>
        )}
        {!fullscreen && (
          <button type="button" className="btn btn-icon btn-ghost" onClick={onClose} aria-label="关闭详情">
            <Icon name="close" size={16} />
          </button>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
        {error && <div className="mb-3"><ErrorBarLite text={error} /></div>}

        {row.kind === "builtin_skill" && (
          <p className="mb-3 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
            这是系统内置能力，只读展示，不能编辑、停用或归档，也不能被同名的自建条目覆盖。
          </p>
        )}

        {!isMcp && skillVersion && (
          <>
            <Field label="用途">{skillVersion.purpose}</Field>
            <Field label="何时使用">{skillVersion.when_to_use || "未填写"}</Field>
            <Field label="何时不使用">{skillVersion.when_not_to_use || "未填写"}</Field>
            <Field label="返回形式">{skillVersion.result_format || "未填写"}</Field>
            <Field label="允许的业务动作">
              {row.permission}
              {!skillVersion.allowed_actions.length && <p className="mt-0.5 text-[12px] text-ink-2">仅提供说明，不执行单证工作流。</p>}
            </Field>
            {skillVersion.parameters.length > 0 && (
              <Field label="参数">
                <ul className="space-y-1">
                  {skillVersion.parameters.map((p) => (
                    <li key={p.name} className="text-[12px] text-ink-2">
                      <span className="mono text-ink">{p.name}</span>
                      {p.required ? "（必填）" : ""} · {p.description}
                      {p.options?.length ? ` · 取值 ${p.options.join("／")}` : ""}
                      {p.default_value ? ` · 默认 ${p.default_value}` : ""}
                    </li>
                  ))}
                </ul>
              </Field>
            )}
            <Field label="步骤说明">
              <p className="mb-1 text-[11px] text-ink-3">任务说明，不是系统提示词；其中的命令式文字只作低信任材料。</p>
              <pre className="whitespace-pre-wrap break-words-all rounded-ctl bg-page p-2 text-[12px] leading-relaxed text-ink">{skillVersion.instructions || "未填写"}</pre>
            </Field>
            {skillVersion.validation_errors?.length ? (
              <div className="mb-3 rounded-ctl border border-risk-line bg-risk-bg p-2.5">
                <p className="text-[12px] font-medium text-risk">校验未通过</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-risk">
                  {skillVersion.validation_errors.map((e, i) => (
                    <li key={`${e.field}-${i}`}>{e.message}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        )}

        {isMcp && mcpVersion && (
          <>
            <Field label="端点摘要">
              <span className="mono break-words-all text-[12px]">{mcpVersion.endpoint_summary}</span>
            </Field>
            <Field label="传输模板">{mcpVersion.template_label}</Field>
            <Field label="认证方式">{mcpVersion.auth_type === "bearer" ? `Bearer 令牌${mcpVersion.credential_set ? "（凭证已保存在服务端，不回显）" : "（尚未配置凭证）"}` : "无需认证"}</Field>
            {mcpVersion.tested_at && <Field label="最近测试">{formatDateTime(mcpVersion.tested_at)}，成功只说明当时可达</Field>}
            {mcpVersion.error_class && <Field label="错误类别"><span className="text-risk">{mcpVersion.error_class}</span></Field>}
            {mcpVersion.test_stages?.length ? (
              <Field label="测试过程">
                <ul className="space-y-1">
                  {mcpVersion.test_stages.map((s, i) => (
                    <li key={i} className="rounded-ctl border border-line px-2 py-1.5 text-[12px]">
                      <div className="flex items-center gap-1.5">
                        <Icon name={s.ok ? "check" : "alert"} size={12} className={s.ok ? "text-ok" : "text-risk"} />
                        <span className="flex-1 text-ink-2">{s.stage}</span>
                        <span className="num text-ink-3">{s.duration_ms} ms</span>
                      </div>
                      <p className="num mt-0.5 text-[11px] text-ink-3">
                        {formatDateTime(s.started_at)} → {formatDateTime(s.ended_at)}
                      </p>
                      {s.schema_hash && <p className="mono mt-0.5 text-[11px] text-ink-3">schema {s.schema_hash}</p>}
                      {s.error_class && <p className="mt-0.5 text-[11px] text-risk">{s.error_class}</p>}
                    </li>
                  ))}
                </ul>
              </Field>
            ) : null}
            <div className="mb-3">
              <ToolGrantList
                tools={mcpVersion.tools}
                granted={[]}
                readOnlyView
                onRevoke={(grantId) => revoke.mutate(grantId, { onSuccess: () => toast({ tone: "ok", text: "已撤销该工具授权" }), onError: (e) => setError(errorText(e)) })}
                onRecheck={() => onUpdateMcp(row.extension_id)}
              />
            </div>
          </>
        )}

        <div className="border-t border-line pt-3">
          <p className="mb-1.5 text-[13px] font-medium text-ink">最近调用</p>
          {calls.isPending && <div className="h-12 animate-pulse rounded-ctl bg-page" aria-hidden />}
          {!calls.isPending && !calls.data?.items.length && <p className="text-[12px] text-ink-2">还没有调用记录。</p>}
          <ul className="space-y-1.5">
            {(calls.data?.items ?? []).map((c) => (
              <li key={c.call_id} className="rounded-ctl border border-line p-2 text-[12px]">
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusPill tone={c.status === "succeeded" ? "ok" : c.status === "rejected" ? "dis" : "risk"} icon={c.status === "succeeded" ? "check" : c.status === "rejected" ? "close" : "alert"}>
                    {c.status === "succeeded" ? "成功" : c.status === "rejected" ? (c.reject_class ?? "已拒绝") : "失败"}
                  </StatusPill>
                  <span className="min-w-0 flex-1 truncate text-ink">{c.name}</span>
                </div>
                <p className="mt-0.5 text-ink-2">{c.action}</p>
                <p className="mt-0.5 text-ink-3">
                  {formatTime(c.at)} · 调用者 {c.caller} · 扩展 v{c.extension_version}
                  {c.tool_version ? ` · 工具 ${c.tool_version}` : ""}
                  {c.case_ref ? ` · 单票 ${c.case_ref}` : ""}
                </p>
                <p className="mt-0.5 text-ink-3">{c.error_class ?? c.result_redacted}</p>
                <details className="mt-2 text-ink-2">
                  <summary className="cursor-pointer">调用详情与来源范围</summary>
                  <p className="mt-1">目的地：{c.destination ?? "历史记录未保存"}</p>
                  <p>开始：{c.started_at ? formatDateTime(c.started_at) : "未记录"} · 结束：{c.finished_at ? formatDateTime(c.finished_at) : "未记录"}</p>
                  <p>{c.parent_turn_id && c.destination === "项目内受控业务动作" ? "自回合创建至记录完成" : "耗时"}：{c.duration_ms === null || c.duration_ms === undefined ? "未记录" : `${c.duration_ms} ms`}</p>
                  <pre className="mt-1 whitespace-pre-wrap break-all text-[11px]">{c.params_redacted ?? "历史参数摘要未保存"}</pre>
                  {c.reference_snapshot_id && <p className="mt-1 break-all">冻结引用：{c.reference_snapshot_id}</p>}
                  <div className="mt-2 flex gap-3">
                    {c.conversation_id && <Link className="text-brand-ink underline underline-offset-2" to={`/conversation/${c.conversation_id}${c.message_id ? `?message_id=${c.message_id}` : ""}`}>查看关联消息</Link>}
                    {c.case_ref && <Link className="text-brand-ink underline underline-offset-2" to={`/documents/cases/${c.case_ref}`}>查看单票</Link>}
                  </div>
                </details>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-ink-3">调用记录经过脱敏，不显示单证原文或秘密参数。</p>
        </div>
      </div>

      <Dialog
        open={confirm === "disable"}
        onClose={() => setConfirm(null)}
        title={`停用「${row.name}」`}
        description="停用后立即阻断新的调用；对话输入里已选的标签会变成不可用，但你输入的问题文本会保留。"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
              返回
            </button>
            <button type="button" className="btn btn-danger" onClick={() => disable.mutate(undefined, { onSuccess: () => { setConfirm(null); toast({ tone: "ok", text: "已停用" }); } })}>
              停用
            </button>
          </>
        }
      />
      <Dialog
        open={confirm === "archive"}
        onClose={() => setConfirm(null)}
        title={`归档「${row.name}」`}
        description="归档会停止新的调用并从常规列表隐藏，但历史版本与调用审计仍然保留。"
        width={480}
        tone="risk"
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
              返回
            </button>
            <button type="button" className="btn btn-danger" onClick={() => archive.mutate(undefined, { onSuccess: () => { setConfirm(null); toast({ tone: "ok", text: "已归档" }); onClose(); } })}>
              确认归档
            </button>
          </>
        }
      >
        {isMcp && <p className="text-[12px] leading-relaxed text-ink-2">个人凭证若不再被任何连接引用，将按凭证存储策略销毁；历史调用记录仍按审计规则保留。</p>}
      </Dialog>
    </aside>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <p className="mb-0.5 text-[11px] text-ink-3">{label}</p>
      <div className="text-[13px] leading-relaxed text-ink">{children}</div>
    </div>
  );
}
