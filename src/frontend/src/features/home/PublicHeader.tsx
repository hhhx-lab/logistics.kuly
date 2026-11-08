// 首页与公开文档页的顶栏：左上品牌与文档入口，右上界面语言和登录/注册。
// overlay 叠在首页视频上，solid 用于文档页的浅色底。
import { useEffect } from "react";
import { Link, NavLink } from "react-router-dom";
import { Icon } from "../../components/Icon";
import { LANGUAGES, languageCode, languageNativeName } from "../../i18n/languages";
import { getAnonymousLocale, setAnonymousLocale, setInterfaceLocale, UiText, useInterfaceLocale } from "../../i18n/runtime";
import { useSession } from "../../stores/session";
import { PUBLIC_DOCS } from "./publicDocs";

export function PublicHeader({ variant, onLogin }: { variant: "overlay" | "solid"; onLogin: () => void }) {
  const locale = useInterfaceLocale();
  const authenticated = useSession((s) => s.status === "authenticated");
  const overlay = variant === "overlay";

  useEffect(() => {
    if (!authenticated) setInterfaceLocale(getAnonymousLocale());
  }, [authenticated]);

  const linkCls = ({ isActive }: { isActive: boolean }) =>
    overlay
      ? `rounded-ctl px-2.5 py-1.5 text-[13px] anim ${isActive ? "bg-white/20 text-white" : "text-white/85 hover:bg-white/15 hover:text-white"}`
      : `rounded-ctl px-2.5 py-1.5 text-[13px] anim ${isActive ? "bg-brand-soft font-medium text-ink" : "text-ink-2 hover:bg-fill hover:text-ink"}`;

  return (
    <header className={`z-bar flex h-16 items-center gap-3 px-4 sm:px-6 ${overlay ? "public-header-overlay text-white" : "border-b border-line bg-card text-ink"}`}>
      <Link to="/" className="flex shrink-0 items-center gap-2" aria-label="物流单证智能体首页">
        <span className="brand-tile flex h-8 w-8 items-center justify-center rounded-ctl">
          <Icon name="logo" size={20} />
        </span>
        <span className={`hidden text-[15px] font-semibold lg:inline ${overlay ? "drop-shadow" : ""}`}>物流单证智能体</span>
      </Link>

      <nav className="scroll-thin flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto lg:ml-4" aria-label="文档">
        {PUBLIC_DOCS.map((doc) => (
          <NavLink key={doc.slug} to={`/docs/${doc.slug}`} className={linkCls}>
            <span className="whitespace-nowrap"><UiText value={doc.title} /></span>
          </NavLink>
        ))}
      </nav>

      <div className="flex shrink-0 items-center gap-2">
        {!authenticated && (
          <div className={`relative flex h-9 w-9 items-center justify-center rounded-full anim ${overlay ? "glass-icon-btn" : "border border-line bg-card text-ink-2 hover:border-line-strong hover:text-ink"}`}>
            <Icon name="globe" size={17} />
            <select
              className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
              aria-label="界面语言"
              title="界面语言"
              value={locale}
              onChange={(event) => {
                const selected = languageCode(event.target.value);
                if (selected) setAnonymousLocale(selected);
              }}
            >
              {LANGUAGES.map((item) => <option key={item.code} value={item.code} lang={item.code}>{languageNativeName(item.code)}</option>)}
            </select>
          </div>
        )}
        {authenticated ? (
          <Link to="/conversation" className={overlay ? "glass-btn" : "btn btn-primary"}>进入工作台</Link>
        ) : (
          <button type="button" className={overlay ? "glass-btn" : "btn btn-primary"} onClick={onLogin}>
            <Icon name="user" size={16} />
            登录 / 注册
          </button>
        )}
      </div>
    </header>
  );
}
