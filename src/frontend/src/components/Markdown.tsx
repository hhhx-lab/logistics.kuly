// 极简 Markdown 渲染：只支持标题、粗体、行内代码、列表、引用与段落。
// 不解析原始 HTML、不解析链接跳转，模型输出不会成为可执行内容。
import { Fragment, type ReactNode } from "react";

function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  // 依次匹配 **粗体**、`代码`
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    i += 1;
    if (tok.startsWith("**")) {
      out.push(
        <strong key={`${keyPrefix}-b${i}`} className="font-semibold text-ink">
          {tok.slice(2, -2)}
        </strong>,
      );
    } else {
      out.push(
        <code key={`${keyPrefix}-c${i}`} className="mono rounded bg-page px-1 py-0.5 text-[12.5px] text-ink-2">
          {tok.slice(1, -1)}
        </code>,
      );
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let list: string[] | null = null;
  let ordered = false;

  const flushList = () => {
    if (!list) return;
    const items = list;
    const Tag = ordered ? "ol" : "ul";
    blocks.push(
      <Tag key={`l${blocks.length}`} className={`my-1.5 space-y-1 pl-5 ${ordered ? "list-decimal" : "list-disc"} marker:text-ink-3`}>
        {items.map((it, i) => (
          <li key={i} className="leading-relaxed">
            {inline(it, `l${blocks.length}-${i}`)}
          </li>
        ))}
      </Tag>,
    );
    list = null;
  };

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const num = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || num) {
      const isOrdered = !!num;
      if (list && ordered !== isOrdered) flushList();
      ordered = isOrdered;
      list = [...(list ?? []), (bullet ?? num)![1]];
      return;
    }
    flushList();
    if (!line.trim()) return;
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const size = level === 1 ? "text-[15px]" : "text-[14px]";
      blocks.push(
        <p key={`h${idx}`} className={`mt-2.5 mb-1 font-semibold text-ink ${size}`}>
          {inline(heading[2], `h${idx}`)}
        </p>,
      );
      return;
    }
    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      blocks.push(
        <blockquote key={`q${idx}`} className="my-1.5 border-l-2 border-line pl-3 text-ink-2">
          {inline(quote[1], `q${idx}`)}
        </blockquote>,
      );
      return;
    }
    blocks.push(
      <p key={`p${idx}`} className="my-1.5 leading-relaxed first:mt-0 last:mb-0">
        {inline(line, `p${idx}`)}
      </p>,
    );
  });
  flushList();

  return <div className={`text-[14px] text-ink ${className}`}>{blocks.map((b, i) => <Fragment key={i}>{b}</Fragment>)}</div>;
}
