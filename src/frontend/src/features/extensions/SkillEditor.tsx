import { CapabilityMarketLink } from "../../components/CapabilityMarketLink";
import { englishError, UiText } from "../../i18n/runtime";
// 自建 Skill 向导：草稿 → 验证 → 启用。说明是「任务说明，不是系统提示词」，
// 只以纯文本预览，不执行任何导入内容。
import { useEffect, useRef, useState } from "react";
import { errorText } from "../../api/client";
import type { ImportDiffRow, SkillFieldError, SkillParam, SkillVersion } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, StatusPill } from "../../components/Status";
import { extensionsApi, newKey, useExtensionCommand, useSkillActions } from "./hooks";

export interface SkillDraft {
  skillId: string | null;
  version: number;
  data: Omit<SkillVersion, "version" | "status" | "validated_at" | "validation_errors" | "ever_enabled" | "expected_version">;
  /** 该 Skill 是否已有启用版本：决定编辑横幅文案。 */
  hasEnabledVersion?: boolean;
}

export const EMPTY_SKILL: SkillDraft = {
  skillId: null,
  version: 1,
  data: { name: "", identifier: "", purpose: "", when_to_use: "", when_not_to_use: "", parameters: [], allowed_actions: [], result_format: "", instructions: "" },
};

/** 名称到 snake_case 标识的建议值，仅作建议，用户可改。 */
function suggestIdentifier(name: string) {
  const ascii = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[a-z]/.test(ascii) ? ascii.slice(0, 40) : "";
}

const STEPS = ["填写", "验证", "启用"];

export function SkillEditor({ initial, onClose }: { initial: SkillDraft; onClose: () => void }) {
  const toast = useToast();
  const [d, setD] = useState(initial.data);
  const [skillId, setSkillId] = useState(initial.skillId);
  const [version, setVersion] = useState(initial.version);
  const [errors, setErrors] = useState<SkillFieldError[]>([]);
  const [validated, setValidated] = useState(false);
  const [confirmEnable, setConfirmEnable] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [askClose, setAskClose] = useState(false);
  const [identTouched, setIdentTouched] = useState(!!initial.data.identifier);
  const [confirmDelParam, setConfirmDelParam] = useState<number | null>(null);
  const panel = useRef<HTMLElement>(null);
  const actions = useSkillActions();

  const save = useExtensionCommand<void, { skill_id: string; version: SkillVersion }>(async (_a, key) => {
    if (skillId) {
      const v = await extensionsApi.patchSkillVersion(skillId, version, d, key);
      return { skill_id: skillId, version: v };
    }
    return extensionsApi.createSkill(d, key);
  });
  const validate = useExtensionCommand<void, SkillVersion>((_a, key) => extensionsApi.validateSkill(skillId!, version, key));
  const enable = useExtensionCommand<void, SkillVersion>((_a, key) => extensionsApi.enableSkill(skillId!, version, key));

  const set = <K extends keyof typeof d>(k: K, v: (typeof d)[K]) => {
    setD((p) => ({ ...p, [k]: v }));
    setDirty(true);
    setValidated(false);
    setErrors([]);
    // 内容变了就换幂等键，否则第二次保存会被当成重放而静默丢弃
    save.renewKey();
    validate.renewKey();
  };

  const errFor = (field: string) => errors.find((e) => e.field === field)?.message;
  const addParam = () => set("parameters", [...d.parameters, { name: "", type: "string", required: false, description: "" }]);
  const setParam = (i: number, patch: Partial<SkillParam>) => set("parameters", d.parameters.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  const removeParam = (i: number) => set("parameters", d.parameters.filter((_, idx) => idx !== i));

  const step = validated ? 2 : skillId ? 1 : 0;
  const closeGuarded = () => (dirty ? setAskClose(true) : onClose());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return; // 只关最上层
      e.stopPropagation();
      closeGuarded();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  });

  useEffect(() => {
    panel.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true });
  }, []);

  const doSave = () =>
    save.mutate(undefined, {
      onSuccess: (r) => {
        setSkillId(r.skill_id);
        setVersion(r.version.version);
        setDirty(false);
        toast({ tone: "ok", text: "已保存为未启用草稿，对话中还不会出现这个能力" });
      },
    });

  const doValidate = () =>
    validate.mutate(undefined, {
      onSuccess: (v) => {
        const errs = v.validation_errors ?? [];
        setErrors(errs);
        setValidated(v.status === "available");
        if (v.status === "available") toast({ tone: "ok", text: "校验通过，可以启用" });
        else {
          // 焦点移动到首个错误字段
          const first = errs[0];
          requestAnimationFrame(() => panel.current?.querySelector<HTMLElement>(`[data-field="${first?.field}"]`)?.focus({ preventScroll: true }));
        }
      },
    });

  const readOnlyActions = (actions.data?.items ?? []).filter((a) => a.group === "read_only");
  const writeActions = (actions.data?.items ?? []).filter((a) => a.group === "controlled_write");
  const policy = actions.data?.combination_policy;
  const readonlySelected = d.allowed_actions.filter(action => policy?.readonly_actions.includes(action));
  const businessSelected = d.allowed_actions.filter(action => !policy?.readonly_actions.includes(action));
  const combinationError = !policy ? "正在读取可执行动作范围" : businessSelected.length > policy.max_business_steps || (!policy.allow_mixed && businessSelected.length > 0 && readonlySelected.length > 0) ? "仅支持一个业务动作；不能组合多个业务动作或混合只读动作" : readonlySelected.length > policy.max_readonly_steps ? `最多支持 ${policy.max_readonly_steps} 个只读动作` : null;
  const labelOf = (id: string) => actions.data?.items.find((a) => a.action_id === id)?.label ?? id;

  return (
    <>
      <aside ref={panel} className="fixed inset-y-0 right-0 z-panel flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-panel anim-slide-in" role="dialog" aria-label="Skill 编辑">
        <header className="shrink-0 border-b border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <p className="text-[14px] font-medium text-ink">{initial.skillId ? `编辑草稿 v${version}` : "新建 Skill"}</p>
              <p className="text-[11px] text-ink-3">{skillId ? (validated ? "已校验，待启用" : "未启用草稿") : "尚未保存"}</p>
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
          <CapabilityMarketLink compact />
          {initial.skillId && (
            <p className="mb-3 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
              {initial.hasEnabledVersion
                ? "这是基于已启用版本创建的新草稿。在新版本启用前，对话里仍然使用旧的启用版本，历史消息也继续指向它。"
                : "这是一个尚未启用的草稿版本，可以继续修改；它现在还不会出现在对话的能力选择器里。"}
            </p>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="label">名称</span>
              <input
                className="input mt-1"
                data-autofocus
                data-field="name"
                value={d.name}
                aria-invalid={!!errFor("name")}
                onChange={(e) => {
                  const name = e.target.value;
                  setD((p) => ({ ...p, name, identifier: identTouched ? p.identifier : suggestIdentifier(name) }));
                  setDirty(true);
                  setValidated(false);
                  setErrors([]);
                  save.renewKey();
                  validate.renewKey();
                }}
              />
              {errFor("name") && <FieldError text={errFor("name")!} />}
            </label>
            <label className="block">
              <span className="label">唯一标识</span>
              <input
                className="input mono mt-1"
                data-field="identifier"
                value={d.identifier}
                aria-invalid={!!errFor("identifier")}
                onChange={(e) => {
                  setIdentTouched(true);
                  set("identifier", e.target.value.toLowerCase());
                }}
                onBlur={() => {
                  // 失焦即检查本人作用域与内置标识冲突
                  if (!d.identifier.trim() || !skillId) return;
                  void extensionsApi
                    .validateSkill(skillId, version, newKey())
                    .then((v) => setErrors((v.validation_errors ?? []).filter((x) => x.field === "identifier" || x.field === "name")))
                    .catch(() => undefined);
                }}
                placeholder="port_code_normalize"
              />
              {errFor("identifier") ? <FieldError text={errFor("identifier")!} /> : <span className="mt-1 block text-[11px] text-ink-3">按名称自动建议，可自行修改；不能占用内置标识。</span>}
            </label>
          </div>

          <label className="mt-3 block">
            <span className="label">一句用途</span>
            <input className="input mt-1" data-field="purpose" value={d.purpose} aria-invalid={!!errFor("purpose")} onChange={(e) => set("purpose", e.target.value)} />
            {errFor("purpose") && <FieldError text={errFor("purpose")!} />}
          </label>
          <label className="mt-3 block">
            <span className="label">何时使用</span>
            <textarea className="input mt-1" rows={2} data-field="when_to_use" value={d.when_to_use} aria-invalid={!!errFor("when_to_use")} onChange={(e) => set("when_to_use", e.target.value)} />
            {errFor("when_to_use") && <FieldError text={errFor("when_to_use")!} />}
          </label>
          <label className="mt-3 block">
            <span className="label">何时不使用</span>
            <textarea className="input mt-1" rows={2} value={d.when_not_to_use} onChange={(e) => set("when_not_to_use", e.target.value)} />
          </label>

          <fieldset className="mt-4">
            <legend className="label mb-1.5">参数定义</legend>
            {d.parameters.map((p, i) => (
              <div key={i} className="mb-2 rounded-ctl border border-line p-2">
                <div className="grid gap-2 sm:grid-cols-2">
                  <input className="input h-8 text-[13px]" data-field={`parameters.${i}.name`} placeholder="参数名" value={p.name} onChange={(e) => setParam(i, { name: e.target.value })} />
                  <select className="input h-8 text-[13px]" value={p.type} onChange={(e) => setParam(i, { type: e.target.value as SkillParam["type"] })}>
                    <option value="string">文本</option>
                    <option value="enum">枚举</option>
                    <option value="boolean">开关</option>
                  </select>
                </div>
                {errFor(`parameters.${i}.name`) && <FieldError text={errFor(`parameters.${i}.name`)!} />}
                <input className="input mt-2 h-8 text-[13px]" placeholder="说明" value={p.description} onChange={(e) => setParam(i, { description: e.target.value })} />
                {p.type === "enum" && (
                  <>
                    <input
                      className="input mt-2 h-8 text-[13px]"
                      data-field={`parameters.${i}.options`}
                      placeholder="可选取值，用顿号或逗号分隔"
                      value={(p.options ?? []).join("、")}
                      onChange={(e) => setParam(i, { options: e.target.value.split(/[、,，]/).map((s) => s.trim()).filter(Boolean) })}
                    />
                    {errFor(`parameters.${i}.options`) && <FieldError text={errFor(`parameters.${i}.options`)!} />}
                  </>
                )}
                <input className="input mt-2 h-8 text-[13px]" placeholder={p.type === "boolean" ? "默认值：true / false（可留空）" : "默认值（可留空）"} value={String(p.default_value ?? "")} onChange={(e) => {
                  const value = e.target.value;
                  setParam(i, { default_value: p.type === "boolean" && /^(true|false)$/.test(value) ? value === "true" : value });
                }} />
                <div className="mt-2 flex items-center justify-between">
                  <label className="flex items-center gap-1.5 text-[12px] text-ink-2">
                    <input type="checkbox" checked={p.required} onChange={(e) => setParam(i, { required: e.target.checked })} />
                    必填
                  </label>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm text-risk"
                    onClick={() => {
                      // 已有输入的行删除前先确认
                      const filled = p.name.trim() || (p.description ?? "").trim() || (p.options ?? []).length || String(p.default_value ?? "").trim();
                      if (filled) setConfirmDelParam(i);
                      else removeParam(i);
                    }}
                  >
                    删除这行
                  </button>
                </div>
              </div>
            ))}
            <button type="button" className="btn btn-secondary btn-sm" onClick={addParam}>
              <Icon name="plus" size={14} />
              增加参数
            </button>
          </fieldset>

          <fieldset className="mt-4" data-field="allowed_actions">
            <legend className="label mb-1.5">允许的业务动作</legend>
            {actions.isPending && <div className="h-8 animate-pulse rounded-ctl bg-page" aria-hidden />}
            <p className="mb-1.5 text-[11px] text-ink-3">只读动作</p>
            <div className="flex flex-wrap gap-1.5">
              {readOnlyActions.map((a) => (
                <label key={a.action_id} className={`tag cursor-pointer ${d.allowed_actions.includes(a.action_id) ? "tag-on" : "border-line"}`}>
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={d.allowed_actions.includes(a.action_id)}
                    onChange={() => set("allowed_actions", d.allowed_actions.includes(a.action_id) ? d.allowed_actions.filter((x) => x !== a.action_id) : [...d.allowed_actions, a.action_id])}
                  />
                  {a.label}
                </label>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-ink-3">{policy ? `可组合最多 ${policy.max_readonly_steps} 个只读动作，或单独选择一个有权限的业务动作。` : "正在读取执行范围。"}</p>
            <p className="mb-1.5 mt-2 text-[11px] text-ink-3">受控业务动作</p>
            <div className="flex flex-wrap gap-1.5">
              {writeActions.map((a) => (
                <label key={a.action_id} className={`tag ${a.available ? "cursor-pointer" : "bg-dis-bg text-dis"} ${d.allowed_actions.includes(a.action_id) ? "tag-on" : "border-line"}`} title={a.disabled_reason ?? undefined}>
                  <input type="checkbox" className="sr-only" checked={d.allowed_actions.includes(a.action_id)} disabled={!a.available} onChange={() => set("allowed_actions", d.allowed_actions.includes(a.action_id) ? d.allowed_actions.filter(x => x !== a.action_id) : [...d.allowed_actions, a.action_id])} />{a.label}
                </label>
              ))}
            </div>
            {combinationError && <FieldError text={combinationError} />}
            {errFor("allowed_actions") && <FieldError text={errFor("allowed_actions")!} />}
            {!d.allowed_actions.length && <p className="mt-1.5 text-[11px] text-ink-3">未选择任何动作时，这个 Skill 只提供说明，不执行单证工作流。</p>}
          </fieldset>

          <label className="mt-4 block">
            <span className="label">返回形式</span>
            <input className="input mt-1" data-field="result_format" placeholder="例如：列出候选值与依据区段" value={d.result_format} aria-invalid={!!errFor("result_format")} onChange={(e) => set("result_format", e.target.value)} />
            {errFor("result_format") && <FieldError text={errFor("result_format")!} />}
          </label>

          <label className="mt-4 block">
            <span className="label">步骤说明</span>
            <span className="mt-0.5 block text-[11px] text-ink-3">这是任务说明，不是系统提示词。其中的命令式文字只作低信任材料，不会改变系统规则或审核门禁。</span>
            <textarea className="input mt-1" rows={6} data-field="instructions" value={d.instructions} aria-invalid={!!errFor("instructions")} onChange={(e) => set("instructions", e.target.value)} />
            {errFor("instructions") && <FieldError text={errFor("instructions")!} />}
          </label>
          {d.instructions && (
            <div className="mt-2 rounded-ctl border border-line bg-page p-2.5">
              <p className="mb-1 text-[11px] text-ink-3">纯文本预览</p>
              <pre className="whitespace-pre-wrap break-words-all text-[12px] leading-relaxed text-ink">{d.instructions}</pre>
            </div>
          )}

          {validated && (
            <div className="mt-4 rounded-ctl border border-line p-3">
              <p className="mb-2 text-[13px] font-medium text-ink">对话里将看到什么</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <p className="text-[11px] text-ink-3">在 / 中的名称</p>
                  <p className="text-[13px] text-ink">{d.name}</p>
                  <p className="mt-1.5 text-[11px] text-ink-3">用途</p>
                  <p className="text-[13px] text-ink">{d.purpose}</p>
                  <p className="mt-1.5 text-[11px] text-ink-3">版本</p>
                  <p className="num text-[13px] text-ink">v{version}</p>
                </div>
                <div>
                  <p className="text-[11px] text-ink-3">参数</p>
                  <p className="text-[13px] text-ink">{d.parameters.length ? d.parameters.map((p) => `${p.name}${p.required ? "（必填）" : ""}`).join("、") : "无"}</p>
                  <p className="mt-1.5 text-[11px] text-ink-3">允许动作</p>
                  <p className="text-[13px] text-ink">{d.allowed_actions.length ? d.allowed_actions.map(labelOf).join("、") : "仅提供说明，不执行单证工作流"}</p>
                </div>
              </div>
            </div>
          )}

          {errors.length > 0 && (
            <div className="mt-3 rounded-ctl border border-risk-line bg-risk-bg p-3">
              <p className="text-[13px] font-medium text-risk">校验未通过，共 {errors.length} 处</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-risk">
                {errors.map((e, i) => (
                  <li lang="en" key={`${e.field}-${i}`}>{englishError(e.message)}</li>
                ))}
              </ul>
            </div>
          )}
          {(save.error || validate.error || enable.error) && <div className="mt-3"><ErrorBar text={errorText(save.error ?? validate.error ?? enable.error)} /></div>}
        </div>

        <footer className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-3">
          <span className="flex-1 text-[11px] text-ink-3">{validated ? "校验已通过" : skillId ? "保存后可校验" : "先保存草稿"}</span>
          <button type="button" className="btn btn-secondary" onClick={closeGuarded}>
            取消
          </button>
          <button type="button" className="btn btn-secondary" onClick={doSave} disabled={save.isPending || !d.name.trim() || !!combinationError}>
            {save.isPending ? "正在保存" : "保存草稿"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={doValidate} disabled={!skillId || dirty || validate.isPending || !!combinationError} title={!skillId ? "请先保存草稿" : dirty ? "有未保存的修改，请先保存" : undefined}>
            {validate.isPending ? "正在校验" : "验证"}
          </button>
          <button type="button" className="btn btn-primary" onClick={() => setConfirmEnable(true)} disabled={!validated || !!combinationError}>
            启用
          </button>
        </footer>
      </aside>

      <Dialog
        open={confirmDelParam !== null}
        onClose={() => setConfirmDelParam(null)}
        title="删除这个参数"
        description="该行已有输入，删除后这些内容不会保留。"
        width={420}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setConfirmDelParam(null)}>
              继续编辑
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => {
                if (confirmDelParam !== null) removeParam(confirmDelParam);
                setConfirmDelParam(null);
              }}
            >
              删除
            </button>
          </>
        }
      />

      <Dialog
        open={confirmEnable}
        onClose={() => setConfirmEnable(false)}
        title="启用这个 Skill"
        description="启用后它才会出现在对话的能力选择器里。"
        width={480}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirmEnable(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-autofocus
              onClick={() =>
                enable.mutate(undefined, {
                  onSuccess: () => {
                    setConfirmEnable(false);
                    toast({ tone: "ok", text: "已启用。若候选列表尚未同步，会显示为正在同步" });
                    onClose();
                  },
                })
              }
            >
              确认启用
            </button>
          </>
        }
      >
        <dl className="grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-3">对话中显示名称</dt>
          <dd className="text-ink">{d.name}</dd>
          <dt className="text-ink-3">用途</dt>
          <dd className="text-ink">{d.purpose}</dd>
          <dt className="text-ink-3">版本</dt>
          <dd className="num text-ink">v{version}</dd>
          <dt className="text-ink-3">参数</dt>
          <dd className="text-ink">{d.parameters.length ? d.parameters.map((p) => `${p.name}${p.required ? "（必填）" : ""}`).join("、") : "无"}</dd>
          <dt className="text-ink-3">允许动作</dt>
          <dd className="text-ink">{d.allowed_actions.length ? d.allowed_actions.map(labelOf).join("、") : "仅提供说明，不执行单证工作流"}</dd>
        </dl>
      </Dialog>

      <Dialog
        open={askClose}
        onClose={() => setAskClose(false)}
        title="还有未保存的修改"
        width={420}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setAskClose(false)} data-autofocus>
              继续编辑
            </button>
            <button type="button" className="btn btn-danger" onClick={onClose}>
              丢弃本地输入
            </button>
          </>
        }
      >
        <p className="text-[12px] text-ink-2">已保存的草稿版本仍保留在服务端，这里只丢弃尚未保存的本地输入。</p>
      </Dialog>
    </>
  );
}

function FieldError({ text }: { text: string }) {
  return (
    <span lang="en" className="mt-1 block text-[11px] text-risk" role="status">
      {englishError(text)}
    </span>
  );
}

/** 导入：只解析不执行，逐项列出可用／未知动作／越权／不支持脚本／低信任指令。 */
export function ImportPanel({ onClose, onDraft, onMcpDraft }: { onClose: () => void; onDraft: (d: SkillDraft) => void; onMcpDraft: (connectionId: string) => void }) {
  const toast = useToast();
  const [kind, setKind] = useState<"skill" | "mcp">("skill");
  const [text, setText] = useState("");
  const [fileInfo, setFileInfo] = useState<{ name: string; size: number } | null>(null);
  const [diff, setDiff] = useState<ImportDiffRow[] | null>(null);
  const [skillDraft, setSkillDraft] = useState<Partial<SkillVersion> | null>(null);
  const [mcpDraft, setMcpDraft] = useState<import("../../api/generated/request-contracts").McpImportDraftView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [saving, setSaving] = useState(false);
  const [askClose, setAskClose] = useState(false);

  const verdictTone: Record<ImportDiffRow["verdict"], "ok" | "warn" | "risk" | "dis"> = {
    可用: "ok",
    未知动作: "warn",
    越权: "risk",
    不支持脚本: "dis",
    "不支持的传输": "dis",
    低信任指令: "risk",
  };

  const dirty = !!text.trim();
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

  const preview = async () => {
    setPending(true);
    setError(null);
    setDiff(null);
    setSkillDraft(null);
    setMcpDraft(null);
    try {
      if (kind === "skill") {
        const r = await extensionsApi.importSkillPreview(text);
        setDiff(r.diff);
        setSkillDraft(r.draft);
      } else {
        const r = await extensionsApi.importMcpPreview(text);
        setDiff(r.diff);
        setMcpDraft(r.draft);
      }
    } catch (e) {
      // 解析失败保留用户选择的文件名与错误，允许重选
      setError(errorText(e));
    } finally {
      setPending(false);
    }
  };

  const pickFile = async (f: File | undefined) => {
    if (!f) return;
    setFileInfo({ name: f.name, size: f.size });
    setDiff(null);
    setError(null);
    try {
      setText(await f.text());
    } catch {
      setError("无法读取所选文件，请改为粘贴内容或重新选择。");
    }
  };

  /** 真正落库：Skill 走 E03 建未启用草稿，MCP 走 E06 建未启用连接后进向导补凭证与测试。 */
  const saveDraft = async () => {
    setSaving(true);
    setError(null);
    try {
      if (kind === "skill" && skillDraft) {
        const body = {
          name: skillDraft.name ?? "",
          identifier: skillDraft.identifier ?? "",
          purpose: skillDraft.purpose ?? "",
          when_to_use: skillDraft.when_to_use ?? "",
          when_not_to_use: skillDraft.when_not_to_use ?? "",
          parameters: skillDraft.parameters ?? [],
          allowed_actions: skillDraft.allowed_actions ?? [],
          result_format: skillDraft.result_format ?? "",
          instructions: skillDraft.instructions ?? "",
        };
        const r = await extensionsApi.createSkill(body, newKey());
        toast({ tone: "ok", text: "已保存为未启用草稿，尚未进入对话候选" });
        onDraft({ skillId: r.skill_id, version: r.version.version, data: body, hasEnabledVersion: false });
      } else if (kind === "mcp" && mcpDraft) {
        const r = await extensionsApi.createMcp({ name: mcpDraft.name ?? "导入的连接", endpoint: mcpDraft.endpoint ?? "", auth_type: "none" }, newKey());
        toast({ tone: "ok", text: "已保存连接草稿，请补齐认证并测试连接" });
        onMcpDraft(r.connection_id);
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  const savable = kind === "skill" ? !!skillDraft?.name : !!mcpDraft?.endpoint;

  return (
    <>
      <aside className="fixed inset-y-0 right-0 z-panel flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-panel anim-slide-in" role="dialog" aria-label="导入">
        <header className="flex h-bar shrink-0 items-center gap-2 border-b border-line px-4">
          <p className="flex-1 text-[14px] font-medium text-ink">导入</p>
          <button type="button" className="btn btn-icon btn-ghost" onClick={closeGuarded} aria-label="关闭">
            <Icon name="close" size={16} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
          <CapabilityMarketLink compact />
          <div className="flex gap-1.5">
            {([["skill", "Skill 说明／manifest"], ["mcp", "MCP 连接描述"]] as const).map(([id, label]) => (
              <label key={id} className={`tag cursor-pointer ${kind === id ? "tag-on" : "border-line"}`}>
                <input type="radio" name="import-kind" className="sr-only" checked={kind === id} onChange={() => { setKind(id); setDiff(null); setSkillDraft(null); setMcpDraft(null); }} />
                {label}
              </label>
            ))}
          </div>
          <p className="mt-2 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
            只解析文本内容，不会运行其中的脚本、命令或网络请求。可执行包不受支持。
          </p>

          <div className="mt-3">
            <span className="label">选择本地文件</span>
            <input
              type="file"
              accept=".txt,.md,.json,.yaml,.yml,text/plain,application/json"
              className="mt-1 block w-full text-[12px] text-ink-2"
              aria-label="选择本地说明或 manifest 文件"
              onChange={(e) => void pickFile(e.target.files?.[0])}
            />
            {fileInfo && (
              <p className="mt-1 text-[11px] text-ink-3">
                已选择 <span className="text-ink">{fileInfo.name}</span> · <span className="num">{(fileInfo.size / 1024).toFixed(1)}</span> KB
                {error ? "（解析失败，可重新选择或改为粘贴）" : ""}
              </p>
            )}
          </div>

          <label className="mt-3 block">
            <span className="label">或粘贴内容</span>
            <textarea
              className="input mono mt-1 text-[12px]"
              rows={10}
              value={text}
              onChange={(e) => { setText(e.target.value); setDiff(null); }}
              placeholder={kind === "skill" ? "name: 港口代码规范化\nidentifier: port_code_normalize\npurpose: ...\nactions: query_case_facts" : '{"mcpServers": {"ports": {"url": "https://..."}}}'}
              data-autofocus
            />
          </label>
          <button type="button" className="btn btn-secondary mt-2" onClick={() => void preview()} disabled={!text.trim() || pending}>
            {pending ? "正在解析" : "解析并预览差异"}
          </button>
          {error && <div className="mt-3"><ErrorBar text={error} /></div>}

          {diff && (
            <div className="mt-3">
              <p className="mb-1.5 text-[13px] font-medium text-ink">解析结果</p>
              <ul className="space-y-1.5">
                {diff.map((r, i) => (
                  <li key={i} className={`rounded-ctl border p-2 ${r.verdict === "低信任指令" || r.verdict === "越权" ? "border-risk-line bg-risk-bg" : "border-line"}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusPill tone={verdictTone[r.verdict]} icon={r.verdict === "可用" ? "check" : r.verdict === "越权" || r.verdict === "低信任指令" ? "alert" : "warn"}>
                        {r.verdict}
                      </StatusPill>
                      <span className="min-w-0 flex-1 break-words-all text-[13px] text-ink">{r.item}</span>
                    </div>
                    <p className={`mt-0.5 text-[12px] ${r.verdict === "低信任指令" || r.verdict === "越权" ? "font-medium text-risk" : "text-ink-2"}`}>
                      {r.verdict === "低信任指令" ? `不可生效：${r.detail}` : r.detail}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-4 py-3">
          <button type="button" className="btn btn-secondary" onClick={closeGuarded}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!savable || saving}
            title={!savable ? "请先解析出可导入的内容" : kind === "mcp" ? "保存后进入连接向导补齐认证并测试" : undefined}
            onClick={() => void saveDraft()}
          >
            {saving ? "正在保存" : "保存为未启用草稿"}
          </button>
        </footer>
      </aside>

      <Dialog
        open={askClose}
        onClose={() => setAskClose(false)}
        title="放弃这次导入"
        description="尚未保存的解析内容会被丢弃；已经保存过的草稿不受影响。"
        width={420}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setAskClose(false)}>
              继续导入
            </button>
            <button type="button" className="btn btn-danger" onClick={onClose}>
              丢弃
            </button>
          </>
        }
      />
    </>
  );
}
