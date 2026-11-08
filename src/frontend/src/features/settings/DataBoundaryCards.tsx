import { useInterfaceText } from "./useInterfaceText";
import { UiText } from "../../i18n/runtime";
// 会话与数据说明：三张只读信息卡，解释系统策略、长期偏好与单票／会话记忆的边界。
// 只做解释与导航，不提供「编辑系统提示词」或「导入旧聊天作为记忆」。
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../../components/Icon";
import { useSession } from "../../stores/session";

const CARDS = [
  {
    id: "policy",
    title: "系统策略",
    summary: "由项目维护并版本化，普通用户不可编辑。",
    body: [
      "系统策略规定领域边界、权限范围、防注入要求和证据要求，由项目维护者统一版本化管理。",
      "普通用户没有编辑入口；上传的原件、OCR 文本、Skill 说明与外部 MCP 返回都属于不可信内容，不能覆盖它。",
      "这里不展示完整的系统提示词，也不展示任何内部密钥或工具白名单。",
    ],
    links: [
      { label: "了解如何关联历史单据", to: "/conversation?hint=mention", hint: "进入对话后，在输入区用 @ 选择有权访问的单票" },
      { label: "查看单据管理台", to: "/documents" },
    ] as { label: string; to: string; hint?: string }[],
  },
  {
    id: "memory",
    title: "我的长期偏好",
    summary: "只有你确认过的设置，仅对你本人的新回合生效。",
    body: [
      "只有你在「我的记忆」里确认过的偏好会被保存，按你的身份在新会话和之后的回合装配。",
      "当轮明确提出的要求优先于长期偏好；偏好不会改变已冻结的任务要求、已完成的回答或已导出的文件。",
      "其他用户看不到你的偏好与待确认建议。",
    ],
    links: [
      { label: "了解如何关联历史单据", to: "/conversation?hint=mention", hint: "进入对话后，在输入区用 @ 选择有权访问的单票" },
      { label: "查看单据管理台", to: "/documents" },
    ],
  },
  {
    id: "case",
    title: "单票与会话记忆",
    summary: "单票事实带版本留在业务存储，按权限显式关联。",
    body: [
      "Case、文件、Run 与快照的事实带版本保存在业务存储，只有当次上传或你显式用 @ 关联时才按权限装配。",
      "本会话最多保留近 20 轮，以及只属于该会话的旧轮次摘要；结束后的新会话不会加载旧聊天或旧摘要。",
      "摘要只用于定位，不作为已核验的事实；回答单票事实仍要回查带来源的结构化视图。",
    ],
    links: [
      { label: "了解如何关联历史单据", to: "/conversation?hint=mention", hint: "进入对话后，在输入区用 @ 选择有权访问的单票" },
      { label: "查看单据管理台", to: "/documents" },
    ],
  },
];

const AUXILIARY_EN: Record<string, string> = {
  "由项目维护并版本化，普通用户不可编辑。": "Maintained and versioned by the project; regular users cannot edit it.",
  "系统策略规定领域边界、权限范围、防注入要求和证据要求，由项目维护者统一版本化管理。": "Project maintainers version the domain boundaries, permissions, injection safeguards and evidence requirements.",
  "普通用户没有编辑入口；上传的原件、OCR 文本、Skill 说明与外部 MCP 返回都属于不可信内容，不能覆盖它。": "Uploaded files, OCR text, Skill instructions and external MCP results are untrusted content and cannot override these policies.",
  "这里不展示完整的系统提示词，也不展示任何内部密钥或工具白名单。": "Full system prompts, internal secrets and tool allowlists are not displayed here.",
  "只有你确认过的设置，仅对你本人的新回合生效。": "Only preferences you confirm apply to your future turns.",
  "只有你在「我的记忆」里确认过的偏好会被保存，按你的身份在新会话和之后的回合装配。": "Only preferences confirmed in My Memory are saved and loaded for your new conversations and future turns.",
  "当轮明确提出的要求优先于长期偏好；偏好不会改变已冻结的任务要求、已完成的回答或已导出的文件。": "Explicit instructions take priority. Preferences do not change frozen tasks, completed answers or exported files.",
  "其他用户看不到你的偏好与待确认建议。": "Other users cannot see your preferences or pending suggestions.",
  "单票事实带版本留在业务存储，按权限显式关联。": "Case facts stay versioned in business storage and are linked explicitly with permission checks.",
  "Case、文件、Run 与快照的事实带版本保存在业务存储，只有当次上传或你显式用 @ 关联时才按权限装配。": "Case, file, run and snapshot facts are versioned in business storage and loaded only for current uploads or cases explicitly linked with @.",
  "本会话最多保留近 20 轮，以及只属于该会话的旧轮次摘要；结束后的新会话不会加载旧聊天或旧摘要。": "This conversation keeps at most 20 recent turns and its own older-turn summary. New conversations do not load previous chats or summaries.",
  "摘要只用于定位，不作为已核验的事实；回答单票事实仍要回查带来源的结构化视图。": "Summaries help locate information. Case answers still consult structured facts with source references.",
  "以下三张卡片只作说明。它们不会自动把任何单票、旧聊天或摘要带进新会话，卡片内容也不会被保存为你的记忆。": "These cards explain data boundaries. They do not link cases, previous chats or summaries, and are not saved as memory.",
  "这两个入口只做导航，不会自动关联任何历史单票或旧会话。": "These links only navigate; they do not automatically link historical cases or conversations.",
  "进入对话后，在输入区用 @ 选择有权访问的单票": "In a conversation, use @ to select a case you can access."
};

export function DataBoundaryCards() {
  const uiText = useInterfaceText();
  const translate = (text: string) => uiText(text, AUXILIARY_EN[text] ?? text);
  const navigate = useNavigate();
  const identity = useSession((s) => s.identity);
  // 展开状态只保留在当前用户的本次页面会话里
  const [expanded, setExpanded] = useState<string[]>([]);

  return (
    <div className="max-w-[800px] space-y-3">
      <p className="text-[12px] leading-relaxed text-ink-3">{translate("以下三张卡片只作说明。它们不会自动把任何单票、旧聊天或摘要带进新会话，卡片内容也不会被保存为你的记忆。")}</p>
      {CARDS.map((c) => {
        const open = expanded.includes(c.id);
        return (
          <section key={c.id} className="card p-5">
            <button
              type="button"
              className="flex w-full items-start gap-2 text-left"
              onClick={() => setExpanded((p) => (p.includes(c.id) ? p.filter((x) => x !== c.id) : [...p, c.id]))}
              aria-expanded={open}
            >
              <Icon name={open ? "chevron-down" : "chevron-right"} size={16} className="mt-0.5 shrink-0 text-ink-3" />
              <span className="min-w-0 flex-1">
                <span className="block text-[14px] font-medium text-ink"><UiText value={c.title} /></span>
                <span className="mt-0.5 block text-[12px] text-ink-2">{translate(c.summary)}</span>
                {c.id === "policy" && identity?.policy_version && (
                  <span className="num mt-0.5 block text-[11px] text-ink-3">策略版本 {identity.policy_version}</span>
                )}
              </span>
              <span className="shrink-0 rounded bg-page px-1.5 py-0.5 text-[11px] text-ink-2">只读</span>
            </button>
            {open && (
              <div className="mt-3 border-t border-line pt-3">
                {c.body.map((p, i) => (
                  <p key={i} className="mb-1.5 text-[13px] leading-relaxed text-ink-2">
                    {translate(p)}
                  </p>
                ))}
                {c.links.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {c.links.map((l) => (
                      <button key={l.to} type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(l.to)} title={l.hint ? translate(l.hint) : undefined}>
                        {l.label}
                      </button>
                    ))}
                    <p className="w-full text-[11px] text-ink-3">{translate("这两个入口只做导航，不会自动关联任何历史单票或旧会话。")}</p>
                  </div>
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
