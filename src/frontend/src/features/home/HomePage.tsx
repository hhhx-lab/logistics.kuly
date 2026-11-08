// 未登录首页：宣传片《一张单据的旅程》全屏静音循环播放，顶栏叠在视频上。
// 减少动效时不自动播放，只显示海报和播放按钮；浏览器拒绝自动播放时同样保留播放入口。
import { useEffect, useRef, useState } from "react";
import { LoginDialog } from "./LoginDialog";
import { PublicHeader } from "./PublicHeader";

const VIDEO_SRC = "/media/document-journey_home.mp4";
const POSTER_SRC = "/media/document-journey_poster.jpg";

function prefersReducedMotion() {
  const pref = document.documentElement.dataset.motion;
  if (pref === "reduce") return true;
  if (pref === "standard") return false;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

export function HomePage() {
  const [loginOpen, setLoginOpen] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  // 只有自动播放被拒绝或处于减少动效模式时，才在画面中央给出大号播放按钮
  const [needsTap, setNeedsTap] = useState(false);

  useEffect(() => {
    const v = video.current;
    if (!v) return;
    // React 不会把 muted 写成 DOM 属性，部分浏览器据此拒绝自动播放，这里显式设置
    v.muted = true;
    if (prefersReducedMotion()) setNeedsTap(true);
    else v.play().catch(() => { setPlaying(false); setNeedsTap(true); });
  }, []);

  const toggle = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) v.play().catch(() => setPlaying(false));
    else v.pause();
  };

  return (
    <div className="flex min-h-full flex-col bg-[#0f1b24] md:relative md:block md:h-full md:min-h-[560px] md:overflow-hidden">
      <div className="md:absolute md:inset-x-0 md:top-0 md:z-bar">
        <PublicHeader variant="overlay" onLogin={() => setLoginOpen(true)} />
      </div>

      <div className="relative aspect-video w-full md:absolute md:inset-0 md:aspect-auto">
        <video
          ref={video}
          className="absolute inset-0 h-full w-full object-cover"
          src={VIDEO_SRC}
          poster={POSTER_SRC}
          muted
          loop
          playsInline
          preload="auto"
          aria-label="宣传片《一张单据的旅程》，无声情景演示动画"
          onPlay={() => { setPlaying(true); setNeedsTap(false); }}
          onPause={() => setPlaying(false)}
          onEnded={(e) => {
            // loop 已保证循环；个别浏览器忽略 loop 时在结束处手动重播
            e.currentTarget.currentTime = 0;
            void e.currentTarget.play().catch(() => setPlaying(false));
          }}
        />
        {needsTap && (
          <button type="button" className="glass-icon-btn absolute left-1/2 top-1/2 z-bar h-16 w-16 -translate-x-1/2 -translate-y-1/2" onClick={toggle} aria-label="播放宣传片">
            <PlayGlyph size={28} />
          </button>
        )}
        <button
          type="button"
          className="glass-icon-btn absolute bottom-4 left-4 z-bar h-10 w-10 md:bottom-6 md:left-6"
          onClick={toggle}
          aria-label={playing ? "暂停宣传片" : "播放宣传片"}
          title={playing ? "暂停宣传片" : "播放宣传片"}
        >
          {playing ? <PauseGlyph /> : <PlayGlyph />}
        </button>
      </div>

      <section className="home-cta px-5 py-6 md:absolute md:bottom-24 md:right-6 md:z-bar md:w-[320px] md:rounded-2xl md:p-5 lg:right-8">
        <h1 className="text-[20px] font-semibold leading-snug text-white">让单据流转更清楚</h1>
        <p className="mt-1.5 text-[13px] leading-relaxed text-white/80">对话发起识别与翻译，逐项复核后再交付。</p>
        <button type="button" className="btn btn-primary btn-lg mt-4 w-full" onClick={() => setLoginOpen(true)}>
          体验单据翻译与校验
        </button>
        <p className="mt-2 text-[12px] text-white/60">宣传片为情景演示，人物与样票均为虚构。</p>
      </section>

      <LoginDialog open={loginOpen} onClose={() => setLoginOpen(false)} />
    </div>
  );
}

function PlayGlyph({ size = 18 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" fill="currentColor">
      <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z" />
    </svg>
  );
}

function PauseGlyph() {
  return (
    <svg viewBox="0 0 24 24" width={18} height={18} aria-hidden="true" fill="currentColor">
      <rect x="6.5" y="5" width="4" height="14" rx="1" />
      <rect x="13.5" y="5" width="4" height="14" rx="1" />
    </svg>
  );
}
