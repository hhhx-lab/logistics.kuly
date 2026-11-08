import { ApiError } from "./client";

// 原件、预览与交付字节统一经同源 API 代理读取，每次请求重新鉴权；
// 页面不持有对象存储地址，也不把字节写入浏览器长期存储。

type ContentFetcher = (url: string, signal?: AbortSignal) => Promise<Blob>;

const fetchContentReal: ContentFetcher = async (url, signal) => {
  const res = await fetch(url, { credentials: "include", cache: "no-store", signal });
  if (!res.ok) throw new ApiError(res.status, undefined);
  return res.blob();
};

export function fetchContent(url: string, signal?: AbortSignal) {
  return fetchContentReal(url, signal);
}

/** 以临时对象地址触发浏览器下载，完成后立即释放。 */
export async function downloadContent(url: string, filename: string) {
  const blob = await fetchContent(url);
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
