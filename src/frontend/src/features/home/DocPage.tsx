// 公开文档页：使用指南、智能体介绍、隐私与说明、联系我们。无需登录即可访问。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Navigate, NavLink, useParams } from "react-router-dom";
import { authApi } from "../../api/auth";
import { translateUi, UiText, useInterfaceLocale } from "../../i18n/runtime";
import { useSession } from "../../stores/session";
import { LoginDialog } from "./LoginDialog";
import { PublicHeader } from "./PublicHeader";
import { CONTACT, PUBLIC_DOCS } from "./publicDocs";

export function DocPage() {
  const { slug } = useParams();
  const [loginOpen, setLoginOpen] = useState(false);
  const { status, generation, setIdentity } = useSession();

  // 直接打开文档页时也确认一次登录状态，已登录则右上角显示「进入工作台」
  const me = useQuery({ queryKey: ["me", generation], queryFn: authApi.me, retry: false, staleTime: 5 * 60_000, enabled: status === "loading" });
  useEffect(() => {
    if (me.data) setIdentity(me.data);
  }, [me.data, setIdentity]);

  const locale = useInterfaceLocale();
  const doc = PUBLIC_DOCS.find((d) => d.slug === slug);
  useEffect(() => {
    if (doc) document.title = `${translateUi(doc.title, locale)} · ${translateUi("物流单证智能体", locale)}`;
    return () => {
      document.title = translateUi("物流单证智能体", locale);
    };
  }, [doc, locale]);

  if (!doc) return <Navigate to={`/docs/${PUBLIC_DOCS[0].slug}`} replace />;

  return (
    <div className="flex h-full flex-col bg-page">
      <PublicHeader variant="solid" onLogin={() => setLoginOpen(true)} />
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1040px] gap-8 px-5 py-8 sm:px-8 lg:py-12">
          <aside className="hidden w-[180px] shrink-0 lg:block">
            <nav className="sticky top-0 space-y-0.5" aria-label="文档目录">
              {PUBLIC_DOCS.map((d) => (
                <NavLink
                  key={d.slug}
                  to={`/docs/${d.slug}`}
                  className={({ isActive }) => `block rounded-ctl px-3 py-2 text-[13px] anim ${isActive ? "bg-brand-soft font-medium text-ink" : "text-ink-2 hover:bg-fill hover:text-ink"}`}
                >
                  <span><UiText value={d.title} /></span>
                </NavLink>
              ))}
            </nav>
          </aside>

          <article className="min-w-0 max-w-read flex-1">
            <h1 className="text-title font-semibold text-ink"><UiText value={doc.title} /></h1>
            <p className="mt-2 text-[15px] leading-relaxed text-ink-2"><UiText value={doc.summary} /></p>

            {doc.slug === "contact" && <ContactCard />}

            {doc.sections.map((s) => (
              <section key={s.heading} className="mt-8">
                <h2 className="text-cardt font-semibold text-ink"><UiText value={s.heading} /></h2>
                {s.paragraphs?.map((p) => (
                  <p key={p} className="mt-2 leading-relaxed text-ink-2"><UiText value={p} /></p>
                ))}
                {s.items && (
                  <ul className="mt-2 list-disc space-y-1.5 pl-5 leading-relaxed text-ink-2 marker:text-ink-4">
                    {s.items.map((item) => <li key={item}><UiText value={item} /></li>)}
                  </ul>
                )}
              </section>
            ))}
          </article>
        </div>
      </div>
      <LoginDialog open={loginOpen} onClose={() => setLoginOpen(false)} />
    </div>
  );
}

function ContactCard() {
  const rows = [
    { label: "团队", value: CONTACT.team },
    { label: "邮箱", value: CONTACT.email, href: CONTACT.email ? `mailto:${CONTACT.email}` : "" },
    { label: "电话", value: CONTACT.phone, href: CONTACT.phone ? `tel:${CONTACT.phone.replace(/\s+/g, "")}` : "" },
  ].filter((r) => r.value);

  return (
    <div className="card mt-6 p-5">
      {rows.length ? (
        <dl className="space-y-2">
          {rows.map((r) => (
            <div key={r.label} className="flex gap-4">
              <dt className="w-12 shrink-0 text-ink-3">{r.label}</dt>
              <dd className="min-w-0 break-words-all text-ink">
                {r.href ? <a className="text-brand-ink underline-offset-2 hover:underline" href={r.href}>{r.value}</a> : <span>{r.value}</span>}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-ink-2">联系方式暂未公布，请联系为你开通本系统的部署方或管理员。</p>
      )}
    </div>
  );
}
