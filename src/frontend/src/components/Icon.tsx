// 图标：路径数据取自 Lucide 1.53（ISC 协议），不引入图标库；每个状态都以「图标 + 文字」成对出现。
import { createElement, useId } from "react";

type Shape = [tag: "path" | "rect" | "circle" | "line" | "polyline", attrs: Record<string, string | number>];

const shapes = {
  globe: [["circle",{cx:12,cy:12,r:10}],["path",{d:"M2 12h20"}],["path",{d:"M12 2a17 17 0 0 1 0 20 17 17 0 0 1 0-20"}]],
  collapse: [["rect",{width:18,height:18,x:3,y:3,rx:2}],["path",{d:"M9 3v18"}],["path",{d:"m16 15-3-3 3-3"}]],
  expand: [["rect",{width:18,height:18,x:3,y:3,rx:2}],["path",{d:"M9 3v18"}],["path",{d:"m14 9 3 3-3 3"}]],
  plus: [["path",{d:"M5 12h14"}],["path",{d:"M12 5v14"}]],
  spinner: [["path",{d:"M21 12a9 9 0 1 1-6.219-8.56"}]],
  search: [["path",{d:"m21 21-4.34-4.34"}],["circle",{cx:11,cy:11,r:8}]],
  more: [["circle",{cx:12,cy:12,r:1}],["circle",{cx:19,cy:12,r:1}],["circle",{cx:5,cy:12,r:1}]],
  pencil: [["path",{d:"M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"}],["path",{d:"m15 5 4 4"}]],
  check: [["path",{d:"M20 6 9 17l-5-5"}]],
  close: [["path",{d:"M18 6 6 18"}],["path",{d:"m6 6 12 12"}]],
  alert: [["path",{d:"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"}],["path",{d:"M12 9v4"}],["path",{d:"M12 17h.01"}]],
  warn: [["circle",{cx:12,cy:12,r:10}],["line",{x1:12,x2:12,y1:8,y2:12}],["line",{x1:12,x2:12.01,y1:16,y2:16}]],
  info: [["circle",{cx:12,cy:12,r:10}],["path",{d:"M12 16v-4"}],["path",{d:"M12 8h.01"}]],
  question: [["circle",{cx:12,cy:12,r:10}],["path",{d:"M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"}],["path",{d:"M12 17h.01"}]],
  clock: [["circle",{cx:12,cy:12,r:10}],["path",{d:"M12 6v6l4 2"}]],
  doc: [["path",{d:"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}],["path",{d:"M14 2v5a1 1 0 0 0 1 1h5"}],["path",{d:"M10 9H8"}],["path",{d:"M16 13H8"}],["path",{d:"M16 17H8"}]],
  docs: [["path",{d:"M15 2h-4a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8"}],["path",{d:"M16.706 2.706A2.4 2.4 0 0 0 15 2v5a1 1 0 0 0 1 1h5a2.4 2.4 0 0 0-.706-1.706z"}],["path",{d:"M5 7a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h8a2 2 0 0 0 1.732-1"}]],
  "file-pdf": [["path",{d:"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}],["path",{d:"M14 2v5a1 1 0 0 0 1 1h5"}],["path",{d:"M10 9H8"}],["path",{d:"M16 13H8"}],["path",{d:"M16 17H8"}]],
  "file-image": [["path",{d:"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}],["path",{d:"M14 2v5a1 1 0 0 0 1 1h5"}],["circle",{cx:10,cy:12,r:2}],["path",{d:"m20 17-1.296-1.296a2.41 2.41 0 0 0-3.408 0L9 22"}]],
  "file-word": [["path",{d:"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}],["path",{d:"M14 2v5a1 1 0 0 0 1 1h5"}],["path",{d:"M11 18h2"}],["path",{d:"M12 12v6"}],["path",{d:"M9 13v-.5a.5.5 0 0 1 .5-.5h5a.5.5 0 0 1 .5.5v.5"}]],
  paperclip: [["path",{d:"m16 6-8.414 8.586a2 2 0 0 0 2.829 2.829l8.414-8.586a4 4 0 1 0-5.657-5.657l-8.379 8.551a6 6 0 1 0 8.485 8.485l8.379-8.551"}]],
  send: [["path",{d:"m5 12 7-7 7 7"}],["path",{d:"M12 19V5"}]],
  download: [["path",{d:"M12 15V3"}],["path",{d:"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"}],["path",{d:"m7 10 5 5 5-5"}]],
  external: [["path",{d:"M15 3h6v6"}],["path",{d:"M10 14 21 3"}],["path",{d:"M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"}]],
  "chevron-down": [["path",{d:"m6 9 6 6 6-6"}]],
  "chevron-right": [["path",{d:"m9 18 6-6-6-6"}]],
  "chevron-left": [["path",{d:"m15 18-6-6 6-6"}]],
  "arrow-down": [["path",{d:"M12 5v14"}],["path",{d:"m19 12-7 7-7-7"}]],
  puzzle: [["path",{d:"M10 22V7a1 1 0 0 0-1-1H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5a1 1 0 0 0-1-1H2"}],["rect",{x:14,y:2,width:8,height:8,rx:1}]],
  settings: [["path",{d:"M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"}],["circle",{cx:12,cy:12,r:3}]],
  memory: [["path",{d:"M12 18V5"}],["path",{d:"M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4"}],["path",{d:"M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5"}],["path",{d:"M17.997 5.125a4 4 0 0 1 2.526 5.77"}],["path",{d:"M18 18a4 4 0 0 0 2-7.464"}],["path",{d:"M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517"}],["path",{d:"M6 18a4 4 0 0 1-2-7.464"}],["path",{d:"M6.003 5.125a4 4 0 0 0-2.526 5.77"}]],
  logout: [["path",{d:"m16 17 5-5-5-5"}],["path",{d:"M21 12H9"}],["path",{d:"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"}]],
  user: [["path",{d:"M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"}],["circle",{cx:12,cy:7,r:4}]],
  copy: [["rect",{width:14,height:14,x:8,y:8,rx:2,ry:2}],["path",{d:"M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"}]],
  source: [["path",{d:"M17 5H3"}],["path",{d:"M21 12H8"}],["path",{d:"M21 19H8"}],["path",{d:"M3 12v7"}]],
  retry: [["path",{d:"M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"}],["path",{d:"M3 3v5h5"}]],
  trash: [["path",{d:"M10 11v6"}],["path",{d:"M14 11v6"}],["path",{d:"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"}],["path",{d:"M3 6h18"}],["path",{d:"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"}]],
  eye: [["path",{d:"M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"}],["circle",{cx:12,cy:12,r:3}]],
  panel: [["rect",{width:18,height:18,x:3,y:3,rx:2}],["path",{d:"M15 3v18"}]],
  flow: [["rect",{width:8,height:8,x:3,y:3,rx:2}],["path",{d:"M7 11v4a2 2 0 0 0 2 2h4"}],["rect",{width:8,height:8,x:13,y:13,rx:2}]],
  shield: [["path",{d:"M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"}],["path",{d:"m9 12 2 2 4-4"}]],
  history: [["path",{d:"M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"}],["path",{d:"M3 3v5h5"}],["path",{d:"M12 7v5l4 2"}]],
  chat: [["path",{d:"M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"}]],
} satisfies Record<string, Shape[]>;

export type IconName = "logo" | keyof typeof shapes;

/** 按显示尺寸补偿描边：小图标略粗、大图标略细，让各档实际线宽接近。strokeWidth 按相对粗细处理，1.5 为常规。 */
function strokeFor(size: number, strokeWidth: number) {
  const px = size <= 12 ? 1.25 : size <= 14 ? 1.3 : size <= 16 ? 1.4 : 1.5;
  return ((px * 24) / size) * (strokeWidth / 1.5);
}

export function Icon({ name, size = 20, className = "", strokeWidth = 1.5 }: { name: IconName; size?: number; className?: string; strokeWidth?: number }) {
  if (name === "logo") return <BrandMark size={size} className={className} />;
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={className}
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeFor(size, strokeWidth)}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {shapes[name].map(([tag, attrs], i) => createElement(tag, { key: i, ...attrs }))}
    </svg>
  );
}

/** 品牌标志「以单为帆」：折角单据作帆、船头朝右、两道浪线。单据上的文字行用遮罩镂空，放在任何底色上都能透出底色。 */
function BrandMark({ size, className }: { size: number; className: string }) {
  const mask = `brand-mark-${useId().replace(/:/g, "")}`;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className} aria-hidden="true" focusable="false" fill="currentColor">
      <mask id={mask}>
        <rect width="24" height="24" fill="#fff" />
        <path d="M9.8 7.6h5.4M9.8 10.2h3.6" stroke="#000" strokeWidth="1.4" strokeLinecap="round" />
      </mask>
      <path d="M7.4 1.2h6.2v4.2H18v7.8H7.4Z" mask={`url(#${mask})`} />
      <path d="M14.4 1.2 18 4.8h-3.6Z" opacity=".6" />
      <path d="M2.6 14.4h19.2l-3 3.9a2 2 0 0 1-1.6.8H5.4a1.8 1.8 0 0 1-1.6-1Z" />
      <path d="M2.5 22.2c1.3 0 1.9-1 3.2-1s1.9 1 3.2 1 1.9-1 3.1-1 1.9 1 3.2 1 1.9-1 3.2-1 1.9 1 3.1 1" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity=".8" />
    </svg>
  );
}

/** 按 MIME 选择文件图标。 */
export function fileIcon(mime: string): IconName {
  if (mime.startsWith("image/")) return "file-image";
  if (mime.includes("word")) return "file-word";
  if (mime.includes("pdf")) return "file-pdf";
  return "doc";
}

export function Spinner({ size = 16, className = "" }: { size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={`anim-spin ${className}`} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth={strokeFor(size, 1.8)} strokeLinecap="round">
      <circle cx="12" cy="12" r="9" opacity=".2" />
      <path d="M12 3a9 9 0 0 1 9 9" />
    </svg>
  );
}
