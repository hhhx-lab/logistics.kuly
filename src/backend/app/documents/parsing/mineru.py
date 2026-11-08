"""
MinerU 薄适配（单证工作流方案 5.1，协议细节以 D0 探测记录为准）。

协议：`POST {base}/file-urls/batch` 取 batch_id 与签名 PUT 地址 → PUT 原件字节（不得带任何额外请求头，
带 Authorization 会被拒）→ `GET {base}/extract-results/batch/{batch_id}` 轮询 → `done` 时下载
`full_zip_url`，取 `*_content_list.json`。

为什么同步：解析只在 Celery Worker 中执行，方案第 11 节规定 Worker 使用同步 HTTP 客户端，
不在任务里临时 `asyncio.run()`。

崩溃恢复：batch_id 在上传原件之前就拿到，且 MinerU 支持按 batch_id 查状态。
上层应在 `on_task_id` 回调里立即持久化它；Worker 崩溃后凭 `get_status(batch_id)` 即可判定
是否已受理（`waiting-file` 表示尚未收到原件），无需盲重投。
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Literal

import httpx

from app.config import Settings
from app.documents.parsing.dto import (
    BlockKind,
    NormRect,
    PageInfo,
    ParsedBlock,
    ParsedTable,
    ParsedTableCell,
    ParsedTableRow,
    ParseOutcome,
    ParserUnavailable,
    QualityFlag,
    SegmentKind,
    SourceAnchorDraft,
    SourceSegmentDraft,
    TableCellRef,
)

MINERU_PROTOCOL_VERSION = "mineru_v4_batch/1"
# 实测（合成样件，A4 与横向页各一）：content_list 的 bbox 为左上原点、按页宽高归一化到 0–1000，
# 与 layout.json 中以 pt 计的坐标换算一致。版本变化若改变此约定，校准检查会把坐标降级为页级。
MINERU_BBOX_SCALE = 1000.0
# MinerU 对不存在或已过期的 batch 返回的业务码（实测）
MINERU_CODE_NOT_FOUND = -60012

MAX_READ_ATTEMPTS = 3
MIN_RETRY_INTERVAL_SECONDS = 2.0
# 结果包受控解包上限：只读取需要的 JSON/Markdown，防压缩炸弹
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_MEMBER_BYTES = 200 * 1024 * 1024
MAX_ZIP_DOWNLOAD_BYTES = 200 * 1024 * 1024

MinerUState = Literal["waiting-file", "pending", "running", "converting", "done", "failed", "not_found", "unknown"]
_KNOWN_STATES = {"waiting-file", "pending", "running", "converting", "done", "failed"}


class MinerUError(RuntimeError):
    """
    `kind` 决定上层怎么处理：
    - not_sent / rejected：可稍后重试；
    - acceptance_unknown：原件可能已送达，须先 `get_status` 核查，不得直接重传；
    - auth / permanent / protocol：不重试，显示禁用或失败原因。
    """

    def __init__(
        self,
        kind: Literal["not_sent", "rejected", "acceptance_unknown", "auth", "permanent", "protocol"],
        message: str,
        *,
        http_status: int | None = None,
        provider_code: int | str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.provider_code = provider_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class MinerUOptions:
    enable_formula: bool = False
    enable_table: bool = True
    language: str = "ch"
    is_ocr: bool = True
    model_version: Literal["vlm", "pipeline"] = "vlm"

    @classmethod
    def from_settings(cls, settings: Settings) -> MinerUOptions:
        return cls(model_version=settings.mineru_model_version, language=settings.mineru_language)

    def fingerprint(self) -> str:
        values = {"protocol": MINERU_PROTOCOL_VERSION, **asdict(self)}
        # 旧任务省略模型、传 auto；续查保留已封存的配置指纹。
        if self.model_version == "pipeline" and self.language == "auto":
            values.pop("model_version")
        payload = json.dumps(values, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class MinerUSubmission:
    batch_id: str
    # 签名地址本身就是上传凭据，不进日志与 repr
    upload_url: str = field(repr=False)


@dataclass(frozen=True)
class MinerUStatus:
    batch_id: str
    state: MinerUState
    error_message: str | None = None
    extracted_pages: int | None = None
    total_pages: int | None = None
    zip_url: str | None = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        """供应商是否已收到原件并进入处理。恢复时据此决定「等结果」还是「可重传」。"""
        return self.state in {"pending", "running", "converting", "done", "failed"}

    @property
    def terminal(self) -> bool:
        return self.state in {"done", "failed"}


@dataclass(frozen=True)
class MinerUResult:
    batch_id: str
    content_list: list[dict[str, Any]] = field(repr=False)
    layout: dict[str, Any] | None = field(repr=False)
    markdown: str | None = field(repr=False)
    raw_zip: bytes = field(repr=False)
    backend_version: str | None = None


class MinerUAdapter:
    parser_name = "mineru"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        options: MinerUOptions | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 60.0,
        upload_timeout_seconds: float = 300.0,
    ) -> None:
        if not settings.mineru_base_url or not settings.mineru_api_key:
            raise ParserUnavailable("MinerU 未配置")
        self._base = settings.mineru_base_url.rstrip("/")
        self._settings = settings
        self._client = client or httpx.Client()
        self._closed = False
        self._options = options or MinerUOptions.from_settings(settings)
        self._sleep = sleep
        self._timeout = timeout_seconds
        self._upload_timeout = upload_timeout_seconds

    @property
    def options(self) -> MinerUOptions:
        return self._options

    def close(self) -> None:
        """Worker 子进程退出时关闭自身 HTTP 客户端。重复调用安全。"""
        if not self._closed:
            self._client.close()
            self._closed = True

    @staticmethod
    def _retry_delay(value: str | None) -> float:
        if not value:
            return MIN_RETRY_INTERVAL_SECONDS
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                seconds = (when - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError):
                return MIN_RETRY_INTERVAL_SECONDS
        import math

        return max(MIN_RETRY_INTERVAL_SECONDS, seconds) if math.isfinite(seconds) else MIN_RETRY_INTERVAL_SECONDS

    def _request_signed(self, method: str, url: str, *, data: bytes | None = None, stream: bool = False) -> httpx.Response:
        # 直接构造请求，避免继承注入客户端的默认 Authorization/Content-Type/cookie。
        request = httpx.Request(method, url, content=data)
        request.extensions["timeout"] = httpx.Timeout(self._upload_timeout).as_dict()
        return self._client.send(request, auth=None, stream=stream)

    def _auth_headers(self) -> dict[str, str]:
        # Key 在每次请求时读取，不缓存为实例属性，避免随对象被序列化或打印
        key = self._settings.mineru_api_key
        if key is None or not key.get_secret_value().strip():
            raise ParserUnavailable("MinerU 未配置")
        token = key.get_secret_value().strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ParserUnavailable("MinerU Token 为空")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ---- 提交 ----

    def request_upload(self, file_name: str) -> MinerUSubmission:
        """
        申请 batch 与上传地址。此时还没有原件，不会产生解析计费；超时后重试最多留下一个
        永远 `waiting-file` 的空 batch，因此按可重试读调用处理。
        """
        body = {
            "model_version": self._options.model_version,
            "enable_formula": self._options.enable_formula,
            "enable_table": self._options.enable_table,
            "language": self._options.language,
            "files": [{"name": file_name, "is_ocr": self._options.is_ocr}],
        }
        payload = self._json_call("POST", f"{self._base}/file-urls/batch", json_body=body)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MinerUError("protocol", "MinerU 申请上传的回包结构异常")
        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not isinstance(batch_id, str) or not batch_id or not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            raise MinerUError("protocol", "MinerU 申请上传的回包缺少 batch_id 或上传地址")
        return MinerUSubmission(batch_id=batch_id, upload_url=urls[0])

    def upload(self, submission: MinerUSubmission, data: bytes) -> None:
        """
        PUT 原件字节。只有「连接未建立」才直接重试；已开始传输后失败无法确定对象是否落地，
        抛 acceptance_unknown，由上层先 `get_status` 核查再决定是否重传。
        """
        for attempt in range(MAX_READ_ATTEMPTS):
            try:
                # 签名地址不接受额外请求头（D0 实测带 Authorization 会失败），这里一个头都不加
                resp = self._request_signed("PUT", submission.upload_url, data=data)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                if attempt < MAX_READ_ATTEMPTS - 1:
                    self._sleep(MIN_RETRY_INTERVAL_SECONDS)
                    continue
                raise MinerUError("not_sent", "无法连接 MinerU 上传地址") from None
            except (httpx.UnsupportedProtocol, httpx.InvalidURL):
                raise MinerUError("permanent", "MinerU 上传地址格式无效") from None
            except httpx.TransportError:
                raise MinerUError("acceptance_unknown", "原件上传中断，需先核查 MinerU 是否已收到") from None
            if 200 <= resp.status_code < 300:
                return
            if resp.status_code in {401, 403}:
                raise MinerUError("auth", "MinerU 上传地址拒绝访问或已过期", http_status=resp.status_code)
            if 400 <= resp.status_code < 500:
                raise MinerUError("permanent", "MinerU 拒绝了上传请求", http_status=resp.status_code)
            raise MinerUError("acceptance_unknown", "MinerU 上传返回服务端错误，需先核查", http_status=resp.status_code)
        raise MinerUError("not_sent", "无法连接 MinerU 上传地址")

    def submit(self, data: bytes, file_name: str, *, on_task_id: Callable[[str], None]) -> str:
        """
        申请 → 回调持久化 batch_id → 上传。回调抛错时不上传，保证「先落账再提交」：
        没有记下 batch_id 的原件绝不会送出。
        """
        submission = self.request_upload(file_name)
        on_task_id(submission.batch_id)
        self.upload(submission, data)
        return submission.batch_id

    # ---- 查询与取回 ----

    def get_status(self, batch_id: str) -> MinerUStatus:
        payload = self._json_call(
            "GET", f"{self._base}/extract-results/batch/{batch_id}", allow_codes={MINERU_CODE_NOT_FOUND}
        )
        if payload.get("code") == MINERU_CODE_NOT_FOUND:
            return MinerUStatus(batch_id=batch_id, state="not_found")
        data = payload.get("data")
        results = data.get("extract_result") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return MinerUStatus(batch_id=batch_id, state="unknown")
        item = results[0]
        raw_state = item.get("state")
        # 未见过的状态不猜语义，按 unknown 交给上层核查
        state: MinerUState = raw_state if isinstance(raw_state, str) and raw_state in _KNOWN_STATES else "unknown"
        progress = item.get("extract_progress") or {}
        err = item.get("err_msg") or None
        return MinerUStatus(
            batch_id=batch_id,
            state=state,
            error_message="MinerU 解析失败，需检查原件或服务配置" if err else None,
            extracted_pages=progress.get("extracted_pages") if isinstance(progress, dict) else None,
            total_pages=progress.get("total_pages") if isinstance(progress, dict) else None,
            zip_url=item.get("full_zip_url") if state == "done" and isinstance(item.get("full_zip_url"), str) else None,
        )

    def fetch_result(self, status: MinerUStatus) -> MinerUResult:
        if status.state != "done" or not status.zip_url:
            raise MinerUError("protocol", "只有 done 状态的任务才能取回结果")
        raw = self._read_bytes(status.zip_url)
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise MinerUError("protocol", "MinerU 结果包不是有效的 zip") from None
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise MinerUError("protocol", "MinerU 结果包条目数异常")

            def read_member(predicate: Callable[[str], bool]) -> bytes | None:
                for info in infos:
                    if predicate(info.filename):
                        if info.file_size > MAX_ZIP_MEMBER_BYTES:
                            raise MinerUError("protocol", "MinerU 结果包成员过大")
                        return archive.read(info)
                return None

            # 注意区分 *_content_list.json 与 *_content_list_v2.json，领域归一化只按前者实测字段实现
            content_raw = read_member(lambda n: n.endswith("_content_list.json"))
            if content_raw is None:
                raise MinerUError("protocol", "MinerU 结果包缺少 content_list")
            try:
                content_list = json.loads(content_raw)
            except ValueError:
                raise MinerUError("protocol", "MinerU content_list 不是合法 JSON") from None
            if not isinstance(content_list, list):
                raise MinerUError("protocol", "MinerU content_list 结构异常")

            layout: dict[str, Any] | None = None
            layout_raw = read_member(lambda n: n.rsplit("/", 1)[-1] == "layout.json")
            if layout_raw is not None:
                try:
                    parsed = json.loads(layout_raw)
                    layout = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    layout = None
            md_raw = read_member(lambda n: n.rsplit("/", 1)[-1] == "full.md")
            backend = None
            if layout is not None:
                backend = "/".join(str(layout[k]) for k in ("_backend", "_version_name") if layout.get(k))
            return MinerUResult(
                batch_id=status.batch_id,
                content_list=content_list,
                layout=layout,
                markdown=md_raw.decode("utf-8", errors="replace") if md_raw else None,
                raw_zip=raw,
                backend_version=backend or None,
            )

    def normalize(
        self,
        result: MinerUResult,
        *,
        page_map: Sequence[int] | None = None,
        page_rotations: Mapping[int, int] | None = None,
        key_prefix: str = "o",
    ) -> ParseOutcome:
        return normalize_content_list(
            result.content_list,
            layout=result.layout,
            markdown=result.markdown,
            raw_payload=result.raw_zip,
            provider_task_id=result.batch_id,
            backend_version=result.backend_version,
            config_fingerprint=self._options.fingerprint(),
            page_map=page_map,
            page_rotations=page_rotations,
            key_prefix=key_prefix,
        )

    # ---- HTTP ----

    def _json_call(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        """
        只用于不产生解析计费的调用（申请上传地址、查状态），因此超时与服务端错误均可重试：
        首次加两次，间隔不少于 2 秒，限流时尊重 Retry-After。
        """
        last_error: MinerUError | None = None
        for attempt in range(MAX_READ_ATTEMPTS):
            delay = MIN_RETRY_INTERVAL_SECONDS
            try:
                resp = self._client.request(
                    method, url, json=json_body, headers=self._auth_headers(), timeout=self._timeout
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                last_error = MinerUError("not_sent", "无法连接 MinerU")
            except (httpx.UnsupportedProtocol, httpx.InvalidURL):
                raise MinerUError("permanent", "MinerU 服务地址格式无效") from None
            except httpx.TransportError:
                last_error = MinerUError("rejected", "MinerU 响应超时或中断")
            else:
                if resp.status_code in {401, 403}:
                    raise MinerUError("auth", "MinerU 拒绝了凭据", http_status=resp.status_code)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = MinerUError("rejected", "MinerU 暂时不可用", http_status=resp.status_code)
                    delay = self._retry_delay(resp.headers.get("Retry-After"))
                elif resp.status_code >= 400:
                    raise MinerUError("permanent", "MinerU 判定请求无效", http_status=resp.status_code)
                else:
                    try:
                        payload = resp.json()
                    except ValueError:
                        raise MinerUError("protocol", "MinerU 回包不是 JSON", http_status=resp.status_code) from None
                    if not isinstance(payload, dict):
                        raise MinerUError("protocol", "MinerU 回包结构异常")
                    code = payload.get("code")
                    if code == 0 or (allow_codes and isinstance(code, int) and code in allow_codes):
                        return payload
                    raise MinerUError(
                        "permanent", "MinerU 返回业务错误", http_status=resp.status_code, provider_code=code
                    )
            if attempt < MAX_READ_ATTEMPTS - 1:
                if delay>self._timeout:
                    raise MinerUError("rejected","MinerU只读限流，延迟后继续读取",retry_after=delay)
                self._sleep(delay)
        assert last_error is not None
        raise last_error

    def _read_bytes(self, url: str) -> bytes:
        # 结果包地址是 MinerU 提供的签名下载地址，不带我们的 Key
        last_error: MinerUError | None = None
        for attempt in range(MAX_READ_ATTEMPTS):
            resp = None
            delay = MIN_RETRY_INTERVAL_SECONDS
            try:
                resp = self._request_signed("GET", url, stream=True)
                if resp.status_code == 200:
                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > MAX_ZIP_DOWNLOAD_BYTES:
                            raise MinerUError("protocol", "MinerU 结果包下载体积超限")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                last_error = MinerUError("not_sent", "无法连接 MinerU 结果地址")
            except (httpx.UnsupportedProtocol, httpx.InvalidURL):
                raise MinerUError("permanent", "MinerU 结果地址格式无效") from None
            except httpx.TransportError:
                last_error = MinerUError("rejected", "下载 MinerU 结果中断")
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = MinerUError("rejected", "MinerU 结果地址暂时不可用", http_status=resp.status_code)
                    delay = self._retry_delay(resp.headers.get("Retry-After"))
                else:
                    raise MinerUError("permanent", "MinerU 结果地址不可用或已过期", http_status=resp.status_code)
            finally:
                if resp is not None:
                    resp.close()
            if attempt < MAX_READ_ATTEMPTS - 1:
                if delay>self._timeout:
                    raise MinerUError("rejected","MinerU结果读取限流，延迟后继续读取",retry_after=delay)
                self._sleep(delay)
        assert last_error is not None
        raise last_error


# ---- 归一化 -------------------------------------------------------------------

_TEXT_TYPE_KIND: dict[str, tuple[BlockKind, SegmentKind]] = {
    "header": ("header", "header"),
    "footer": ("footer", "footer"),
    "page_number": ("page_number", "other"),
    "page_footnote": ("footnote", "footnote"),
    "aside_text": ("other", "other"),
}


class _TableHtmlParser(HTMLParser):
    """只认 tr/td/th 与 rowspan/colspan；其余标签只取文字，脚本与属性一律不执行、不保留。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            if not self.rows:
                self.rows.append([])
            attr = dict(attrs)

            def span(name: str) -> int:
                try:
                    return max(1, min(int(attr.get(name) or 1), 1000))
                except ValueError:
                    return 1

            self._cell = []
            self._span = (span("rowspan"), span("colspan"))
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in {"td", "th"} and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self.rows[-1].append((text, *self._span))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._skip_depth:
            self._cell.append(data)


def parse_table_html(html: str) -> list[ParsedTableCell]:
    """按 rowspan/colspan 占位图计算真实网格坐标，合并单元格不会把后续单元格挤错列。"""
    parser = _TableHtmlParser()
    parser.feed(html)
    parser.close()
    occupied: set[tuple[int, int]] = set()
    cells: list[ParsedTableCell] = []
    for r, row in enumerate(parser.rows):
        c = 0
        for text, row_span, col_span in row:
            while (r, c) in occupied:
                c += 1
            for dr in range(row_span):
                for dc in range(col_span):
                    occupied.add((r + dr, c + dc))
            cells.append(ParsedTableCell(row_index=r, col_index=c, row_span=row_span, col_span=col_span, text=text))
            c += col_span
    return cells


def _calibrated_rect(bbox: Any) -> NormRect | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox):
        return None
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if min(x0, y0, x1, y1) < 0 or max(x0, y0, x1, y1) > MINERU_BBOX_SCALE:
        return None
    if not (x0 < x1 and y0 < y1):
        return None
    return NormRect(
        x0=x0 / MINERU_BBOX_SCALE, y0=y0 / MINERU_BBOX_SCALE, x1=x1 / MINERU_BBOX_SCALE, y1=y1 / MINERU_BBOX_SCALE
    )


def normalize_content_list(
    content_list: Sequence[dict[str, Any]],
    *,
    layout: dict[str, Any] | None,
    markdown: str | None,
    raw_payload: bytes | None,
    provider_task_id: str | None,
    backend_version: str | None,
    config_fingerprint: str,
    page_map: Sequence[int] | None = None,
    page_rotations: Mapping[int, int] | None = None,
    key_prefix: str = "o",
) -> ParseOutcome:
    """
    `page_map[i]` 是 MinerU 第 i 页（0 起）对应的原件页码（1 起）。混合 PDF 只把扫描页抽成子文件
    送 OCR 时靠它映射回原页；缺省为恒等映射。`page_rotations` 以原件页码为键，由调用方从原件读取：
    旋转页的归一化坐标是否相对显示方向尚未实测，因此这些页一律降级为页级定位。
    """
    rotations = dict(page_rotations or {})
    locator_version = f"{MINERU_PROTOCOL_VERSION}+{backend_version or 'unknown'}"
    flags: list[QualityFlag] = []
    flagged: set[tuple[str, int | None]] = set()

    def flag(code: Any, page_no: int | None, detail: str = "") -> None:
        if (code, page_no) in flagged:
            return
        flagged.add((code, page_no))
        flags.append(QualityFlag(code=code, page_no=page_no, detail=detail))

    layout_pages = (layout or {}).get("pdf_info") if isinstance(layout, dict) else None
    layout_pages = layout_pages if isinstance(layout_pages, list) else []
    source_page_count = max(
        len(layout_pages),
        max((b.get("page_idx", -1) for b in content_list if isinstance(b.get("page_idx"), int)), default=-1) + 1,
        len(page_map or ()),
    )
    mapping = list(page_map) if page_map is not None else list(range(1, source_page_count + 1))

    def to_page_no(page_idx: Any) -> int | None:
        if isinstance(page_idx, int) and 0 <= page_idx < len(mapping):
            return mapping[page_idx]
        return None

    pages: list[PageInfo] = []
    for i, page_no in enumerate(mapping):
        size = layout_pages[i].get("page_size") if i < len(layout_pages) and isinstance(layout_pages[i], dict) else None
        width = float(size[0]) if isinstance(size, list) and len(size) == 2 else None
        height = float(size[1]) if isinstance(size, list) and len(size) == 2 else None
        pages.append(
            PageInfo(
                page_no=page_no, width_pt=width, height_pt=height, rotation=rotations.get(page_no, 0), route="ocr", needs_ocr=True
            )
        )

    def anchor_for(bbox: Any, page_no: int | None) -> SourceAnchorDraft:
        if page_no is None:
            flag("bbox_uncalibrated", None, "内容块页码无法映射到原件")
            return SourceAnchorDraft(precision="none", locator_version=locator_version)
        rotation = rotations.get(page_no, 0)
        if rotation:
            flag("rotation_uncalibrated", page_no, "旋转页坐标未校准，降为页级定位")
            return SourceAnchorDraft(
                precision="page_only", page_no=page_no, page_rotation=rotation, locator_version=locator_version
            )
        if bbox is None:
            flag("bbox_missing", page_no, "内容块缺少坐标，降为页级定位")
            return SourceAnchorDraft(precision="page_only", page_no=page_no, locator_version=locator_version)
        rect = _calibrated_rect(bbox)
        if rect is None:
            flag("bbox_uncalibrated", page_no, "坐标超出约定范围，降为页级定位")
            return SourceAnchorDraft(precision="page_only", page_no=page_no, locator_version=locator_version)
        return SourceAnchorDraft(
            precision="rect",
            page_no=page_no,
            rect=rect,
            coordinate_space="page_normalized_top_left",
            locator_version=locator_version,
        )

    def page_anchor(page_no: int | None) -> SourceAnchorDraft:
        if page_no is None:
            return SourceAnchorDraft(precision="none", locator_version=locator_version)
        return SourceAnchorDraft(
            precision="page_only", page_no=page_no, page_rotation=rotations.get(page_no, 0), locator_version=locator_version
        )

    blocks: list[ParsedBlock] = []
    tables: list[ParsedTable] = []
    segments: list[SourceSegmentDraft] = []
    order = 0

    def add_segment(kind: SegmentKind, text: str, anchor: SourceAnchorDraft, block_key: str,
                    cell: TableCellRef | None = None) -> str | None:
        nonlocal order
        text = text.strip()
        if not text:
            return None
        key = f"{key_prefix}s{len(segments)}"
        segments.append(
            SourceSegmentDraft(
                key=key, kind=kind, text=text, reading_order=order, block_key=block_key, table_cell=cell, anchor=anchor
            )
        )
        order += 1
        return key

    for idx, item in enumerate(content_list):
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        page_no = to_page_no(item.get("page_idx"))
        block_key = f"{key_prefix}b{idx}"
        anchor = anchor_for(item.get("bbox"), page_no)
        seg_keys: list[str] = []
        table_key: str | None = None

        if btype == "text":
            level = item.get("text_level") if isinstance(item.get("text_level"), int) else None
            is_title = bool(level and level >= 1)
            text = str(item.get("text") or "")
            kind: BlockKind = "title" if is_title else "text"
            if (k := add_segment("title" if is_title else "paragraph", text, anchor, block_key)) is not None:
                seg_keys.append(k)
        elif btype in _TEXT_TYPE_KIND:
            kind, seg_kind = _TEXT_TYPE_KIND[btype]
            level = None
            text = str(item.get("text") or "")
            if (k := add_segment(seg_kind, text, anchor, block_key)) is not None:
                seg_keys.append(k)
        elif btype == "list":
            kind, level = "list", None
            items = item.get("list_items")
            text = "\n".join(str(x) for x in items) if isinstance(items, list) else str(item.get("text") or "")
            # 列表项没有各自的坐标，整块作为一个片段，避免给单项配上整块矩形
            if (k := add_segment("list_item", text, anchor, block_key)) is not None:
                seg_keys.append(k)
        elif btype == "table":
            kind, level = "table", None
            table_key = f"{key_prefix}t{len(tables)}"
            body = item.get("table_body")
            cells = parse_table_html(body) if isinstance(body, str) and body.strip() else []
            if not cells:
                flag("table_structure_unparsed", page_no, "表格结构未取得，需人工核对该区域")
            rows: dict[int, list[ParsedTableCell]] = {}
            for cell in cells:
                ref = TableCellRef(table_key=table_key, row_index=cell.row_index, col_index=cell.col_index)
                # MinerU 不给单元格坐标：定位到「第几页哪张表的哪个格」，不把整表矩形冒充单元格框
                cell_anchor = SourceAnchorDraft(
                    precision="cell", page_no=page_no, table_cell=ref, locator_version=locator_version
                )
                seg_key = add_segment("table_cell", cell.text, cell_anchor, block_key, ref)
                if seg_key is not None:
                    seg_keys.append(seg_key)
                rows.setdefault(cell.row_index, []).append(cell.model_copy(update={"segment_key": seg_key}))
            caption_parts = [str(x) for x in (item.get("table_caption") or []) if str(x).strip()]
            for part in caption_parts:
                if (k := add_segment("caption", part, page_anchor(page_no), block_key)) is not None:
                    seg_keys.append(k)
            for part in (str(x) for x in (item.get("table_footnote") or [])):
                if (k := add_segment("footnote", part, page_anchor(page_no), block_key)) is not None:
                    seg_keys.append(k)
            tables.append(
                ParsedTable(
                    key=table_key,
                    page_nos=(page_no,) if page_no else (),
                    rows=tuple(ParsedTableRow(row_index=r, cells=tuple(cs)) for r, cs in sorted(rows.items())),
                    caption=" ".join(caption_parts) or None,
                    anchor=anchor,
                    structure_source="mineru_html",
                )
            )
            text = ""
        elif btype == "image":
            kind, level = "image", None
            text = ""
            for part in (str(x) for x in (item.get("image_caption") or [])):
                if (k := add_segment("caption", part, page_anchor(page_no), block_key)) is not None:
                    seg_keys.append(k)
            for part in (str(x) for x in (item.get("image_footnote") or [])):
                if (k := add_segment("footnote", part, page_anchor(page_no), block_key)) is not None:
                    seg_keys.append(k)
            flag("image_region_not_transcribed", page_no, "图片区域内的文字未转写")
        elif btype == "equation":
            kind, level = "equation", None
            text = str(item.get("text") or "")
            if (k := add_segment("equation", text, anchor, block_key)) is not None:
                seg_keys.append(k)
        elif btype == "code":
            kind, level = "code", None
            text = str(item.get("code_body") or item.get("text") or "")
            if (k := add_segment("other", text, anchor, block_key)) is not None:
                seg_keys.append(k)
        else:
            kind, level = "other", None
            text = str(item.get("text") or "")
            flag("unknown_block_type", page_no, "存在未识别的内容块类型")
            if (k := add_segment("other", text, anchor, block_key)) is not None:
                seg_keys.append(k)

        blocks.append(
            ParsedBlock(
                key=block_key,
                kind=kind,
                page_no=page_no,
                text=text.strip(),
                text_level=level,
                reading_order=idx,
                anchor=anchor,
                table_key=table_key,
                segment_keys=tuple(seg_keys),
            )
        )

    return ParseOutcome(
        parser="mineru",
        parser_version=locator_version,
        config_fingerprint=config_fingerprint,
        provider_task_id=provider_task_id,
        page_count=max(mapping) if mapping else 0,
        pages=tuple(pages),
        blocks=tuple(blocks),
        tables=tuple(tables),
        segments=tuple(segments),
        quality_flags=tuple(flags),
        markdown=markdown,
        raw_payload=raw_payload,
    )
