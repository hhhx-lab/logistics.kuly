// 设置页：界面偏好、我的记忆、会话与数据说明三段同页，二级导航切换。
// 身份变化时先清空旧草稿再读新用户资料，不让前一个身份的内容过渡显示。
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Dialog } from "../../components/Overlay";
import { formatDateTime } from "../../components/Status";
import { useSession } from "../../stores/session";
import { AppShell } from "../shell/AppShell";
import { DataBoundaryCards } from "./DataBoundaryCards";
import { MemorySection } from "./MemoryList";
import { PreferenceForm } from "./PreferenceForm";
import { usePreferences } from "./hooks";
import { ArchivedConversations } from "./ArchivedConversations";

type SectionId = "preferences" | "memory" | "boundary" | "conversations";

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "preferences", label: "界面偏好" },
  { id: "memory", label: "我的记忆" },
  { id: "conversations", label: "归档对话" },
  { id: "boundary", label: "会话与数据说明" },
];

export function SettingsPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const identity = useSession((s) => s.identity);
  const generation = useSession((s) => s.generation);
  const pref = usePreferences();
  const initial: SectionId = location.pathname.endsWith("/memory") ? "memory" : location.pathname.endsWith("/conversations") ? "conversations" : "preferences";
  const [section, setSection] = useState<SectionId>(initial);
  const [prefDirty, setPrefDirty] = useState(false);
  const [pendingSection, setPendingSection] = useState<SectionId | null>(null);

  // 深链只决定「是否直达记忆段」；页面内切到会话说明时路径仍是 /settings，
  // 因此这里不能把非 /memory 一律推回界面偏好，否则会覆盖刚选中的分区。
  useEffect(() => {
    if (location.pathname.endsWith("/memory")) setSection("memory");
    else if (location.pathname.endsWith("/conversations")) setSection("conversations");
    else setSection((s) => (s === "memory" || s === "conversations" ? "preferences" : s));
  }, [location.pathname]);

  // 切换身份后丢弃前一身份的草稿与选中段
  useEffect(() => {
    setPrefDirty(false);
    setPendingSection(null);
  }, [generation]);

  // 刷新或关闭标签页前提示未保存草稿（页内路由跳转由各表单自身的确认层负责）
  useEffect(() => {
    if (!prefDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [prefDirty]);

  const go = (next: SectionId) => {
    // 离开有未保存草稿的界面偏好前先确认
    if (section === "preferences" && next !== "preferences" && prefDirty) {
      setPendingSection(next);
      return;
    }
    setSection(next);
    navigate(next === "memory" ? "/settings/memory" : next === "conversations" ? "/settings/conversations" : "/settings", { replace: true });
  };

  return (
    <AppShell>
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="mx-auto flex w-full max-w-[1144px] min-h-16 shrink-0 flex-wrap items-center gap-3 px-8 pt-4">
          <div className="min-w-0 flex-1">
            <h1 className="text-[24px] font-semibold leading-tight text-ink">设置</h1>
            {(identity?.display_name || pref.data?.saved_at) && (
              <p className="mt-0.5 text-[13px] text-ink-3">
                {identity?.display_name}
                {pref.data?.saved_at ? `${identity?.display_name ? " · " : ""}上次保存 ${formatDateTime(pref.data.saved_at)}` : ""}
              </p>
            )}
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-auto scroll-thin px-8 py-6">
          <div className="mx-auto flex max-w-[1144px] flex-col gap-8 min-[900px]:flex-row">
            <nav className="w-full shrink-0 min-[900px]:w-[200px]" aria-label="设置分区">
              <ul className="flex gap-1 overflow-x-auto min-[900px]:block min-[900px]:space-y-0.5 min-[900px]:overflow-visible">
                {SECTIONS.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      className={`flex h-10 w-full items-center whitespace-nowrap rounded-ctl px-3 text-left text-[13px] min-[900px]:rounded-none min-[900px]:border-l-[3px] ${
                        section === s.id ? "bg-page font-medium text-ink min-[900px]:border-brand min-[900px]:bg-transparent" : "text-ink-2 hover:bg-page min-[900px]:border-transparent"
                      }`}
                      aria-current={section === s.id ? "page" : undefined}
                      onClick={() => go(s.id)}
                    >
                      {s.label}
                    </button>
                  </li>
                ))}
              </ul>
            </nav>

            <div className="min-w-0 flex-1">
              {/* 三块各自保留未保存草稿：切换分区时组件保持挂载。
                  generation 作为 key：切换身份时整体重挂载，前一身份的草稿不会延续给新用户。 */}
              <div key={generation} className="contents">
                <div hidden={section !== "preferences"}>
                  <PreferenceForm onDirtyChange={setPrefDirty} />
                </div>
                <div hidden={section !== "memory"}>
                  <MemorySection />
                </div>
                <div hidden={section !== "boundary"}>
                  <DataBoundaryCards />
                </div>
                <div hidden={section !== "conversations"}>
                  <ArchivedConversations enabled={section === "conversations"} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <Dialog
        open={pendingSection !== null}
        onClose={() => setPendingSection(null)}
        title="界面偏好还有未保存的修改"
        description="离开这一段不会丢失草稿，它会保留到你保存或取消为止。"
        width={440}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setPendingSection(null)}>
              留在界面偏好
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                const next = pendingSection!;
                setPendingSection(null);
                setSection(next);
                navigate(next === "memory" ? "/settings/memory" : next === "conversations" ? "/settings/conversations" : "/settings", { replace: true });
              }}
            >
              先去其他分区
            </button>
          </>
        }
      />
    </AppShell>
  );
}
