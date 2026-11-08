import { CapabilityMarketLink } from "../../components/CapabilityMarketLink";
import { UiText } from "../../i18n/runtime";
// MCP 连接向导：选模板 → 填端点与认证 → 保存草稿并真实测试 → 逐工具授权后启用。
// 不提供任意传输或 shell 输入；凭证只提交给服务端，不回显。
import { useEffect, useState } from "react";
import { errorText } from "../../api/client";
import type { McpTool, McpToolCategory, McpVersion } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, StatusPill, formatDateTime } from "../../components/Status";
import { extensionsApi, useExtensionCommand, useMcpTemplates } from "./hooks";

const STEPS = ["选择模板", "端点与认证", "测试连接", "工具授权"];

const CATEGORY: Record<McpToolCategory, { text: string; tone: "ok" | "warn" | "risk" }> = {
  read_only: { text: "只读", tone: "ok" },
  write: { text: "写入", tone: "risk" },
  egress: { text: "外发", tone: "warn" },
};

export function McpWizard({ initialConnectionId, onClose }: { initialConnectionId?: string | null; onClose: () => void }) {
  const toast = useToast();
  const updating = !!initialConnectionId;
  const [step, setStep] = useState(0);
  const [connectionId, setConnectionId] = useState<string | null>(initialConnectionId ?? null);
  const [version, setVersion] = useState<McpVersion | null>(null);
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [authType, setAuthType] = useState<"none" | "bearer">("none");
  const [credential, setCredential] = useState("");
  const [granted, setGranted] = useState<string[]>([]);
  const [confirmEnable, setConfirmEnable] = useState(false);
  const [askClose, setAskClose] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(updating);
  const templates = useMcpTemplates();
  const tpl = templates.data?.items[0];
  const publicReady = tpl?.public_registry_ready === true;

  const create = useExtensionCommand<void, { connection_id: string; version: McpVersion }>((_a, key) => extensionsApi.createMcp({ name, endpoint, auth_type: authType, credential: credential || undefined }, key));
  const patch = useExtensionCommand<void, McpVersion>((_a, key) => extensionsApi.patchMcpVersion(connectionId!, version!.version, { name, endpoint, auth_type: authType, ...(credential ? { credential } : {}) }, key));
  const test = useExtensionCommand<void, McpVersion>((_a, key) => extensionsApi.testMcp(connectionId!, version!.version, key));
  const grant = useExtensionCommand<void, McpVersion>((_a, key) => extensionsApi.grantTools(connectionId!, version!.version, granted, key));
  const enable = useExtensionCommand<void, McpVersion>((_a, key) => extensionsApi.enableMcp(connectionId!, version!.version, key));

  // 更新配置：先读现有连接的非秘密字段，再新建一个未测试版本，绝不原位改已启用版
  useEffect(() => {
    if (!initialConnectionId) return;
    let alive = true;
    void (async () => {
      try {
        const data = await extensionsApi.getMcp(initialConnectionId);
        const base = data.versions.find((v) => v.version === data.enabled_version) ?? data.versions[data.versions.length - 1];
        const draft = await extensionsApi.newMcpVersion(initialConnectionId, crypto.randomUUID());
        if (!alive) return;
        setName(base.name);
        setEndpoint(base.endpoint);
        setAuthType(base.auth_type);
        setVersion(draft);
        setGranted([]);
        setStep(1);
      } catch (e) {
        if (alive) setError(errorText(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [initialConnectionId]);

  const dirty = !!name.trim() || !!endpoint.trim() || !!credential;
  const closeGuarded = () => (dirty ? setAskClose(true) : onClose());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      e.stopPropagation();
      closeGuarded();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  });

  const localUrlOk = /^https:\/\/[^\s]+$/.test(endpoint.trim());
  const authOption = tpl?.auth_types.find((a) => a.id === authType);
  const credentialBlocked = authType === "bearer" && (!tpl?.credential_store_ready || authOption?.available === false);
  const tested = publicReady && version?.status === "available" && !!version.tested_at;
  const grantableTools = (version?.tools ?? []).filter((t) => t.category === "read_only" && t.public_scope_ready === true && !t.schema_changed);
  const selectedToolsReady = publicReady && granted.length > 0 && granted.every((id) => grantableTools.some((t) => t.tool_id === id));

  const saveStep = () => {
    setError(null);
    if (updating && version) {
      patch.mutate(undefined, {
        onSuccess: (v) => {
          setVersion(v);
          setCredential("");
          setStep(2);
          toast({ tone: "ok", text: "配置已更新为待测试草稿，受影响工具的授权已暂停" });
        },
      });
      return;
    }
    create.mutate(undefined, {
      onSuccess: (r) => {
        setConnectionId(r.connection_id);
        setVersion(r.version);
        setCredential("");
        setStep(2);
        toast({ tone: "ok", text: "已保存连接草稿，尚未启用" });
      },
    });
  };

  return (
    <>
      <aside className="fixed inset-y-0 right-0 z-panel flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-panel anim-slide-in" role="dialog" aria-label="MCP 连接向导">
        <header className="shrink-0 border-b border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <p className="text-[14px] font-medium text-ink">{updating ? "更新连接配置" : "新增 MCP 连接"}</p>
              <p className="text-[11px] text-ink-3">{updating ? (version ? `待测试草稿 v${version.version}` : "正在读取当前配置") : version ? `未启用草稿 v${version.version}` : "尚未保存"}</p>
            </div>
            <button type="button" className="btn btn-icon btn-ghost" onClick={closeGuarded} aria-label="关闭">
              <Icon name="close" size={16} />
            </button>
          </div>
          <ol className="mt-2 flex items-center gap-1 text-[11px]">
            {STEPS.map((s, i) => (
              <li key={s} className={`flex items-center gap-1 ${i === step ? "font-medium text-brand-ink" : i < step ? "text-ok" : "text-ink-3"}`}>
                <span className={`flex h-4 w-4 items-center justify-center rounded-full text-[10px] ${i === step ? "bg-brand text-white" : i < step ? "bg-ok text-white" : "bg-dis-bg text-dis"}`}>{i < step ? "✓" : i + 1}</span>
                <UiText value={s} />
                {i < STEPS.length - 1 && <Icon name="chevron-right" size={12} className="text-ink-3" />}
              </li>
            ))}
          </ol>
        </header>

        <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
          {step === 0 && <CapabilityMarketLink compact />}
          {loading && <div className="h-20 animate-pulse rounded-ctl bg-page" aria-hidden />}
          {templates.error && <ErrorBar text={errorText(templates.error)} />}
          {tpl && !publicReady && <p role="status" className="mb-3 rounded-ctl bg-warn-bg px-3 py-2 text-[12px] text-warn">{tpl.unavailable_reason ?? "公开工具配置尚未就绪"}。可以保存连接草稿，当前无法测试或启用。</p>}

          {step === 0 && !loading && (
            <div>
              <p className="mb-2 text-[13px] text-ink">选择受支持的连接模板</p>
              {templates.isPending && <div className="h-16 animate-pulse rounded-ctl bg-page" aria-hidden />}
              {tpl && (
                <label className="flex cursor-pointer items-start gap-2 rounded-ctl border opt-on p-3">
                  <input type="radio" name="tpl" className="mt-0.5" defaultChecked />
                  <span>
                    <span className="block text-[13px] font-medium text-ink">{tpl.label}</span>
                    <span className="block text-[12px] text-ink-2">{tpl.description}</span>
                  </span>
                </label>
              )}
              <p className="mt-2 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
                这里是为已有服务建立连接与授权记录，不会在页面里编写或托管 MCP 服务代码，也不执行本地命令。
              </p>
            </div>
          )}

          {step === 1 && !loading && (
            <div className="space-y-3">
              <label className="block">
                <span className="label">显示名</span>
                <input className="input mt-1" value={name} onChange={(e) => setName(e.target.value)} data-autofocus />
              </label>
              <label className="block">
                <span className="label">HTTPS 端点</span>
                <input className="input mono mt-1 text-[13px]" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://example.com/mcp" />
                {endpoint.trim() && !localUrlOk && <span className="mt-1 block text-[11px] text-risk">端点必须是 HTTPS 地址。实际的网络目标与重定向仍由服务端复核。</span>}
              </label>
              <fieldset>
                <legend className="label mb-1.5">认证方式</legend>
                <div className="flex gap-1.5">
                  {(tpl?.auth_types ?? []).map((a) => (
                    <label
                      key={a.id}
                      className={`tag ${a.available ? "cursor-pointer" : "cursor-not-allowed bg-dis-bg text-dis"} ${authType === a.id && a.available ? "tag-on" : "border-line"}`}
                      title={a.disabled_reason ?? undefined}
                    >
                      <input type="radio" name="auth" className="sr-only" checked={authType === a.id} disabled={!a.available} onChange={() => setAuthType(a.id)} />
                      {a.label}
                    </label>
                  ))}
                </div>
                {(tpl?.auth_types ?? []).filter((a) => !a.available).map((a) => (
                  <p key={a.id} className="mt-1 text-[11px] text-ink-3">
                    {a.label}：{a.disabled_reason ?? "当前部署不支持这种认证方式，无法启用"}
                  </p>
                ))}
              </fieldset>
              {authType === "bearer" && (
                <label className="block">
                  <span className="label">访问凭证</span>
                  <input className="input mt-1" type="password" value={credential} onChange={(e) => setCredential(e.target.value)} autoComplete="off" disabled={credentialBlocked} />
                  <span className="mt-1 block text-[11px] text-ink-3">{credentialBlocked ? "个人凭证安全存储尚未就绪，暂不收集明文密钥；可改用无需个人密钥的模板。" : tpl?.credential_store_note}</span>
                  {updating && <span className="mt-1 block text-[11px] text-ink-3">留空表示沿用服务端已保存的凭证，页面不会回显它。</span>}
                </label>
              )}
            </div>
          )}

          {step === 2 && !loading && (
            <div>
              <p className="mb-2 text-[13px] text-ink">真实测试这个连接</p>
              {version?.test_stages?.length ? (
                <ul className="space-y-1.5">
                  {version.test_stages.map((s, i) => (
                    <li key={i} className="rounded-ctl border border-line px-3 py-2">
                      <div className="flex items-center gap-2">
                        <StatusPill tone={s.ok ? "ok" : "risk"} icon={s.ok ? "check" : "alert"}>
                          {s.ok ? "通过" : "失败"}
                        </StatusPill>
                        <span className="min-w-0 flex-1 text-[13px] text-ink">{s.stage}</span>
                        <span className="num shrink-0 text-[11px] text-ink-3">{s.duration_ms} ms</span>
                      </div>
                      <p className="num mt-0.5 text-[11px] text-ink-3">
                        开始 {formatDateTime(s.started_at)} · 结束 {formatDateTime(s.ended_at)}
                      </p>
                      {s.schema_hash && <p className="mono mt-0.5 text-[11px] text-ink-3">schema {s.schema_hash}</p>}
                      {s.error_class && <p className="mt-0.5 text-[11px] text-risk">{s.error_class}</p>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="rounded-ctl bg-page px-3 py-2 text-[12px] text-ink-2">将依次执行「初始化握手 → 获取工具清单 → schema 校验」，并显示每一段的真实起止时间、耗时与结果。</p>
              )}
              {test.isPending && <p className="mt-2 text-[12px] text-ink-2">正在测试，请勿重复提交……</p>}
              {version?.tested_at && (
                <p className="mt-2 text-[11px] text-ink-3">
                  最近测试：{formatDateTime(version.tested_at)}。测试成功只说明测试当时可达；每次真实调用仍会重新检查连接、schema 与权限。
                </p>
              )}
            </div>
          )}

          {step === 3 && version && (
            <ToolGrantList tools={version.tools} granted={granted} onToggle={(id) => setGranted((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))} />
          )}

          {error && <div className="mt-3"><ErrorBar text={error} /></div>}
          {(create.error || patch.error || test.error || grant.error || enable.error) && (
            <div className="mt-3">
              <ErrorBar text={errorText(create.error ?? patch.error ?? test.error ?? grant.error ?? enable.error)} />
            </div>
          )}
        </div>

        <footer className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-3">
          {step > (updating ? 1 : 0) && (
            <button type="button" className="btn btn-ghost" onClick={() => setStep(step - 1)}>
              上一步
            </button>
          )}
          <span className="flex-1" />
          <button type="button" className="btn btn-secondary" onClick={closeGuarded}>
            取消
          </button>
          {step === 0 && (
            <button type="button" className="btn btn-primary" onClick={() => setStep(1)} disabled={!tpl}>
              下一步
            </button>
          )}
          {step === 1 && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!name.trim() || !localUrlOk || (authType === "bearer" && !credential && !updating) || credentialBlocked || create.isPending || patch.isPending}
              onClick={saveStep}
            >
              {create.isPending || patch.isPending ? "正在保存" : updating ? "保存并待测试" : "保存连接草稿"}
            </button>
          )}
          {step === 2 && (
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={!publicReady || test.isPending || !connectionId || !version}
                onClick={() =>
                  test.mutate(undefined, {
                    onSuccess: (v) => {
                      setVersion(v);
                      if (v.status === "available") toast({ tone: "ok", text: "连接测试通过，已获取工具清单" });
                      else toast({ tone: "risk", text: "连接测试失败，草稿与已填配置已保留" });
                    },
                  })
                }
              >
                {test.isPending ? "正在测试" : "测试连接"}
              </button>
              <button type="button" className="btn btn-primary" disabled={!tested} onClick={() => setStep(3)} title={tested ? undefined : "请先完成一次成功的连接测试"}>
                下一步
              </button>
            </>
          )}
          {step === 3 && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!selectedToolsReady}
              onClick={() => setConfirmEnable(true)}
              title={granted.length ? undefined : grantableTools.length ? "请至少授权一个只读工具" : "这个连接没有可授权的只读工具"}
            >
              启用连接
            </button>
          )}
        </footer>
      </aside>

      <Dialog
        open={confirmEnable}
        onClose={() => setConfirmEnable(false)}
        title="启用这个连接"
        description="启用后这些工具才会出现在对话的能力选择器里。"
        width={520}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirmEnable(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-autofocus
              disabled={!selectedToolsReady || grant.isPending || enable.isPending}
              onClick={() =>
                grant.mutate(undefined, {
                  onSuccess: () =>
                    enable.mutate(undefined, {
                      onSuccess: () => {
                        setConfirmEnable(false);
                        toast({ tone: "ok", text: "已启用连接与所选工具" });
                        onClose();
                      },
                    }),
                })
              }
            >
              确认启用
            </button>
          </>
        }
      >
        <dl className="grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-3">服务目标</dt>
          <dd className="mono text-ink">{version?.endpoint_summary ?? endpoint}</dd>
          <dt className="text-ink-3">连接版本</dt>
          <dd className="num text-ink">v{version?.version ?? 1}</dd>
        </dl>
        <p className="mt-2 mb-1 text-[12px] font-medium text-ink">已授权工具与可读数据范围</p>
        <ul className="space-y-1">
          {(version?.tools ?? [])
            .filter((t) => granted.includes(t.tool_id))
            .map((t) => (
              <li key={t.tool_id} className="rounded-ctl border border-line px-2 py-1.5 text-[12px]">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="mono text-ink">{t.name}</span>
                  <StatusPill tone={CATEGORY[t.category].tone} icon="check">
                    {CATEGORY[t.category].text}
                  </StatusPill>
                  <span className="mono text-[11px] text-ink-3">schema {t.schema_hash}</span>
                </div>
                <p className="mt-0.5 text-ink-2">
                  可读范围：{t.scope} · 数据级别：{t.data_level}
                </p>
              </li>
            ))}
        </ul>
        <p className="mt-2 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
          这里仅授权已发布的公开参数范围。当前不支持向 MCP 发送本票敏感字段。
        </p>
      </Dialog>

      <Dialog
        open={askClose}
        onClose={() => setAskClose(false)}
        title="还有未保存的配置"
        description="尚未保存的端点与认证输入会被丢弃；已经保存的连接草稿仍然保留。"
        width={420}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setAskClose(false)}>
              继续编辑
            </button>
            <button type="button" className="btn btn-danger" onClick={onClose}>
              丢弃本地输入
            </button>
          </>
        }
      />
    </>
  );
}

/** 逐工具授权：默认全不勾，写入／外发工具首版不可启用。 */
export function ToolGrantList({
  tools,
  granted,
  onToggle,
  readOnlyView = false,
  onRevoke,
  onRecheck,
}: {
  tools: McpTool[];
  granted: string[];
  onToggle?: (id: string) => void;
  readOnlyView?: boolean;
  onRevoke?: (grantId: string) => void;
  onRecheck?: () => void;
}) {
  const [diffFor, setDiffFor] = useState<McpTool | null>(null);
  if (!tools.length) return <p className="text-[13px] text-ink-2">这个连接还没有发现任何工具，请先完成连接测试。</p>;
  return (
    <div>
      <p className="mb-2 text-[13px] text-ink">发现的工具</p>
      <ul className="space-y-1.5">
        {tools.map((t) => {
          const cat = CATEGORY[t.category];
          // 首版只能授权经评估的只读工具；schema 变化的工具必须重测重授权
          const blocked = t.category !== "read_only" || t.public_scope_ready !== true || !!t.schema_changed;
          const checked = readOnlyView ? t.granted : granted.includes(t.tool_id);
          return (
            <li key={t.tool_id} className={`rounded-ctl border p-2.5 ${checked ? "opt-on" : "border-line"}`}>
              <div className="flex items-start gap-2">
                {!readOnlyView && (
                  <input type="checkbox" className="mt-1" checked={checked} disabled={blocked} onChange={() => onToggle?.(t.tool_id)} aria-label={`授权 ${t.name}`} />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="mono text-[13px] font-medium text-ink">{t.name}</span>
                    <StatusPill tone={cat.tone} icon={t.category === "read_only" ? "check" : "alert"}>
                      {cat.text}
                    </StatusPill>
                    {t.schema_changed && (
                      <button type="button" className="rounded bg-warn-bg px-1.5 py-0.5 text-[11px] text-warn underline" onClick={() => setDiffFor(t)}>
                        待复核：schema 已变化
                      </button>
                    )}
                    {readOnlyView && t.granted && <StatusPill tone="ok" icon="check">已授权</StatusPill>}
                  </div>
                  <p className="mt-0.5 text-[12px] text-ink-2">{t.description}</p>
                  <p className="mt-0.5 text-[12px] text-ink-2">
                    可读范围：{t.scope} · 数据级别：{t.data_level}
                  </p>
                  <p className="mono mt-0.5 text-[11px] text-ink-3">输入 schema {t.input_schema_summary}</p>
                  <p className="mono mt-0.5 text-[11px] text-ink-3">schema {t.schema_hash}</p>
                  {t.category !== "read_only" && <p className="mt-0.5 text-[11px] text-risk">首版只能启用经评估的只读工具，{cat.text}类工具不可授权。</p>}
                  {t.public_scope_ready !== true && <p className="mt-0.5 text-[11px] text-warn">{t.unavailable_reason ?? "维护者尚未发布有效的公开参数范围"}，暂不可授权。</p>}
                  {readOnlyView && t.granted && t.grant_id && onRevoke && (
                    <button type="button" className="btn btn-ghost btn-sm mt-1 text-risk" onClick={() => onRevoke(t.grant_id!)}>
                      撤销这个工具的授权
                    </button>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      <Dialog
        open={!!diffFor}
        onClose={() => setDiffFor(null)}
        title="工具 schema 变化"
        description="旧的授权已失效，需要重新测试并重新授权后才能使用。"
        width={520}
        footer={
          onRecheck ? (
            <>
              <button type="button" className="btn btn-secondary" onClick={() => setDiffFor(null)}>
                关闭
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  setDiffFor(null);
                  onRecheck();
                }}
              >
                重新测试并授权
              </button>
            </>
          ) : undefined
        }
      >
        <p className="mb-2 text-[12px] text-ink-2">
          工具 <span className="mono text-ink">{diffFor?.name}</span> 的输入 schema 发生了变化，逐项对照如下：
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="rounded-ctl border border-line p-2">
            <p className="mb-1 text-[11px] text-ink-3">变化前</p>
            <p className="mono text-[12px] text-ink">{diffFor?.previous_schema_summary ?? "无记录"}</p>
            <p className="mono mt-1 text-[11px] text-ink-3">schema {diffFor?.previous_schema_hash ?? "—"}</p>
          </div>
          <div className="rounded-ctl border border-brand-line bg-brand-soft p-2">
            <p className="mb-1 text-[11px] text-ink-3">变化后</p>
            <p className="mono text-[12px] text-ink">{diffFor?.input_schema_summary}</p>
            <p className="mono mt-1 text-[11px] text-ink-3">schema {diffFor?.schema_hash}</p>
          </div>
        </div>
        <p className="mt-2 text-[12px] text-ink-2">连接测试成功不能长期替代调用时的 schema 校验，因此这个工具在重新授权前不会被调用。</p>
      </Dialog>
    </div>
  );
}
