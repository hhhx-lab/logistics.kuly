import { useNavigationPreference } from "../../stores/navigation";
// 应用外壳：管理台、扩展仓库与设置共用的左导航。
// 对话工作台自带会话导航，不使用本外壳。
import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, useNavigate } from "react-router-dom";
import { authApi } from "../../api/auth";
import { Icon, type IconName } from "../../components/Icon";
import { Menu } from "../../components/Overlay";
import { useSession } from "../../stores/session";
import { useRef } from "react";

const LINKS: { to: string; label: string; icon: IconName }[] = [
  { to: "/conversation", label: "对话工作台", icon: "chat" },
  { to: "/documents", label: "单据管理台", icon: "docs" },
  { to: "/extensions", label: "Skill / MCP", icon: "puzzle" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { collapsed, toggle } = useNavigationPreference();
  const identity = useSession((s) => s.identity);
  const setAnonymous = useSession((s) => s.setAnonymous);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [menuOpen, setMenuOpen] = useState(false);
  const avatarRef = useRef<HTMLButtonElement>(null);

  return (
    <div className="flex h-full min-h-0">
      <aside data-app-nav className={`flex shrink-0 flex-col border-r border-line bg-nav ${collapsed ? "w-navc" : "w-nav"}`}>
        <div className={collapsed ? "flex flex-col items-center gap-1.5 py-3" : "flex h-bar items-center gap-2 px-3"}>
          <span className="brand-tile flex h-8 w-8 shrink-0 items-center justify-center rounded-ctl">
            <Icon name="logo" size={20} />
          </span>
          {!collapsed && <span className="min-w-0 flex-1 truncate text-[14px] font-semibold text-ink">物流单证智能体</span>}
          <button type="button" className="btn btn-icon btn-ghost h-8 w-8" onClick={toggle} aria-label={collapsed ? "展开导航" : "收起导航"}>
            <Icon name={collapsed ? "expand" : "collapse"} size={16} />
          </button>
        </div>

        <nav className="flex-1 space-y-0.5 px-2 py-2" aria-label="主导航">
          {LINKS.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              title={collapsed ? l.label : undefined}
              className={({ isActive }) =>
                `group flex h-10 items-center gap-2.5 rounded-ctl px-2.5 text-[13px] anim ${isActive ? "bg-brand-soft font-medium text-ink" : "text-ink-2 hover:bg-fill hover:text-ink"}`
              }
            >
              {({ isActive }) => (
                <>
                  <Icon name={l.icon} size={18} className={`shrink-0 anim ${isActive ? "text-brand" : "text-ink-4 group-hover:text-ink-2"}`} />
                  {!collapsed && <span className="min-w-0 flex-1 truncate">{l.label}</span>}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-line p-2">
          <button ref={avatarRef} type="button" className="flex h-10 w-full items-center gap-2 rounded-ctl px-2 text-left hover:bg-fill" onClick={() => setMenuOpen(true)} aria-label="账号菜单">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ink text-[12px] font-medium text-white">{identity?.avatar_initial ?? "—"}</span>
            {!collapsed && <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{identity?.display_name ?? "未登录"}</span>}
          </button>
          <Menu
            open={menuOpen}
            onClose={() => setMenuOpen(false)}
            anchor={avatarRef}
            label="账号菜单"
            align="start"
            items={[
              { label: "设置", icon: "settings", onSelect: () => navigate("/settings") },
              { label: "我的记忆", icon: "memory", onSelect: () => navigate("/settings/memory") },
              {
                label: "退出登录",
                icon: "logout",
                tone: "risk",
                separatorBefore: true,
                onSelect: async () => {
                  try {
                    await authApi.logout();
                  } finally {
                    setAnonymous();
                    qc.clear();
                  }
                },
              },
            ]}
          />
        </div>
      </aside>
      <main style={{ paddingRight: "var(--review-reserved-width, 0px)" }} className="flex min-w-0 flex-1 flex-col">{children}</main>
    </div>
  );
}
