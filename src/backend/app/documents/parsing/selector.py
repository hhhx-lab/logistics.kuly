"""
按 MIME 与配置选择解析路线（单证工作流方案 5.1）。

- DOCX：本地受控解析，不依赖外部服务。
- PDF：逐页检查文字层；可靠页走 pdfplumber，扫描页/文字层不足页走 OCR。
  混合 PDF 只把需要 OCR 的页抽成子文件外发，结果按 page_map 回到原页码。
- 图片：整张走 OCR。
- OCR 引擎由 `PARSER_ADAPTER` 决定：mineru 未配置时相关页明确不可用，
  不降级成假结果；只有显式配置 deterministic_fake 时才用假解析器，并带显著标记。

MinerU 是异步任务，提交与轮询由持久化 StepExecution 的工作流步骤驱动；
这里只给出计划、外发输入与合并规则，不在一个函数里阻塞轮询。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.config import AdapterKind, Settings
from app.documents.parsing.docx_adapter import DocxAdapter
from app.documents.parsing.dto import PageInfo, ParseOutcome, ParserUnavailable, QualityFlag
from app.documents.parsing.fake import DeterministicFakeParser
from app.documents.parsing.native_pdf import NativePdfAdapter, extract_pages_pdf, page_rotations
from app.documents.parsing.dto import PREPROCESSING_VERSION

MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
_IMAGE_SUFFIX = {MIME_PNG: ".png", MIME_JPEG: ".jpg"}


@dataclass(frozen=True)
class ParsePlan:
    mime: str
    local_route: Literal["native_pdf", "docx"] | None
    native_pages: tuple[int, ...]
    ocr_pages: tuple[int, ...]
    ocr_engine: Literal["mineru", "fake"] | None
    page_count: int | None
    page_rotations: Mapping[int, int] = field(default_factory=dict)
    page_decisions: tuple[PageInfo, ...] = ()
    ready: bool = True
    unavailable_reason: str | None = None
    preprocessing_version: str | None = None

    @property
    def needs_external_ocr(self) -> bool:
        return bool(self.ocr_pages) and self.ocr_engine == "mineru"

    @property
    def uses_fake(self) -> bool:
        return bool(self.ocr_pages) and self.ocr_engine == "fake"


def _ocr_engine(settings: Settings) -> tuple[Literal["mineru", "fake"] | None, str | None]:
    if settings.parser_adapter is AdapterKind.DETERMINISTIC_FAKE:
        return "fake", None
    if settings.parser_adapter is AdapterKind.MINERU:
        if settings.parser_ready():
            return "mineru", None
        return None, "MinerU 未配置，扫描页与图片暂不能识别"
    return None, f"解析适配器 {settings.parser_adapter.value} 不是可用的 OCR 引擎"


def select_parser(mime: str, settings: Settings, *, data: bytes | None = None) -> ParsePlan:
    engine, engine_problem = _ocr_engine(settings)

    if mime == MIME_DOCX:
        return ParsePlan(mime=mime, local_route="docx", native_pages=(), ocr_pages=(), ocr_engine=None, page_count=None)

    if mime in _IMAGE_SUFFIX:
        return ParsePlan(
            mime=mime,
            local_route=None,
            native_pages=(),
            ocr_pages=(1,),
            ocr_engine=engine,
            page_count=1,
            ready=engine is not None,
            unavailable_reason=engine_problem,
            preprocessing_version=PREPROCESSING_VERSION,
        )

    if mime == MIME_PDF:
        if data is None:
            raise ValueError("PDF 需要原件字节才能逐页判定路线")
        inspections = NativePdfAdapter().inspect_pages(data)
        native = tuple(p.page_no for p in inspections if not p.needs_ocr)
        ocr = tuple(p.page_no for p in inspections if p.needs_ocr)
        needs_engine = bool(ocr)
        return ParsePlan(
            mime=mime,
            local_route="native_pdf" if native else None,
            native_pages=native,
            ocr_pages=ocr,
            ocr_engine=engine if needs_engine else None,
            page_count=len(inspections),
            page_rotations=page_rotations(data),
            page_decisions=tuple(p.to_page_info("ocr" if p.needs_ocr else "native_pdf") for p in inspections),
            ready=(engine is not None) or not needs_engine,
            unavailable_reason=engine_problem if needs_engine else None,
            preprocessing_version=PREPROCESSING_VERSION,
        )

    return ParsePlan(
        mime=mime,
        local_route=None,
        native_pages=(),
        ocr_pages=(),
        ocr_engine=None,
        page_count=None,
        ready=False,
        unavailable_reason="不支持的文件格式",
    )


def prepared_ocr_input(plan: ParsePlan, data: bytes):
    from app.documents.preprocessing import prepare_image, prepare_pdf_ocr
    if not plan.ocr_pages:raise ValueError("该计划没有需要OCR的页")
    if plan.mime in _IMAGE_SUFFIX:return prepare_image(data,mime=plan.mime)
    return prepare_pdf_ocr(data,plan.ocr_pages)


def attach_preprocessing(outcome: ParseOutcome, manifest: dict, flags: Sequence[QualityFlag]) -> ParseOutcome:
    """派生输入已与显示层方向一致，保留精确恒等映射及原始像素反变换。"""
    def anchor(value):
        transform=manifest.get("pages",{}).get(str(value.page_no))
        if not transform:return value
        return value.model_copy(update={"preprocessing_transform":transform,"page_rotation":transform.get("source_rotation",0),
            "locator_version":value.locator_version+"/prep2"})
    return outcome.model_copy(update={
        "segments":tuple(s.model_copy(update={"anchor":anchor(s.anchor)}) for s in outcome.segments),
        "blocks":tuple(b.model_copy(update={"anchor":anchor(b.anchor)}) for b in outcome.blocks),
        "tables":tuple(t.model_copy(update={"anchor":anchor(t.anchor)}) for t in outcome.tables),
        "pages":tuple(p.model_copy(update={"rotation":manifest.get("pages",{}).get(str(p.page_no),{}).get("source_rotation",0)}) for p in outcome.pages),
        "quality_flags":tuple(outcome.quality_flags)+tuple(flags),"preprocessing_manifest":manifest,
        "config_fingerprint":hashlib.sha256((outcome.config_fingerprint+"|"+PREPROCESSING_VERSION).encode()).hexdigest(),
        "parser_version":outcome.parser_version+"/prep2"})


def ocr_input(plan: ParsePlan, data: bytes, *, base_name: str = "source") -> tuple[bytes, str, tuple[int, ...]]:
    """
    外发给 OCR 的最小输入：(字节, 文件名, page_map)。文件名只用随机/内部名，
    不把用户原始文件名发给供应商；后缀决定 MinerU 按图片还是 PDF 处理。
    """
    if not plan.ocr_pages:
        raise ValueError("该计划没有需要 OCR 的页")
    if plan.preprocessing_version:
        prepared=prepared_ocr_input(plan,data)
        suffix=".png" if prepared.mime==MIME_PNG else ".pdf"
        return prepared.data,f"{base_name}{suffix}",plan.ocr_pages
    if plan.mime in _IMAGE_SUFFIX:
        return data, f"{base_name}{_IMAGE_SUFFIX[plan.mime]}", (1,)
    if plan.page_count is not None and len(plan.ocr_pages) == plan.page_count:
        return data, f"{base_name}.pdf", plan.ocr_pages
    return extract_pages_pdf(data, list(plan.ocr_pages)), f"{base_name}.pdf", plan.ocr_pages


def parse_local_routes(plan: ParsePlan, data: bytes) -> ParseOutcome | None:
    """执行不依赖外部服务的部分：DOCX、PDF 原生页、以及显式配置的假 OCR。"""
    if plan.local_route == "docx":
        return DocxAdapter().parse(data)
    parts: list[ParseOutcome] = []
    if plan.local_route == "native_pdf" and plan.native_pages:
        parts.append(NativePdfAdapter().parse(data, pages=list(plan.native_pages)))
    if plan.ocr_pages and plan.ocr_engine == "fake":
        parts.append(DeterministicFakeParser().parse(data, page_nos=plan.ocr_pages, page_count=plan.page_count))
    if not parts:
        return None
    return merge_outcomes(parts, plan=plan)


def merge_outcomes(parts: Sequence[ParseOutcome], *, plan: ParsePlan) -> ParseOutcome:
    """
    合并同一原件不同路线的结果。各部分 key 前缀不同（n/o/f），不会冲突；
    阅读顺序按原页码重排，同页内保留各自解析器给出的顺序。
    计划要求 OCR 却没有 OCR 结果的页，显式记 `ocr_unavailable`，不让它看起来像空白页。
    """
    if not parts:
        raise ValueError("没有可合并的解析结果")
    if len(parts) == 1 and not plan.ocr_pages:
        return parts[0]

    ocr_covered = {p.page_no for part in parts if part.parser != "native_pdf" for p in part.pages}
    decisions = {p.page_no: p for p in plan.page_decisions}

    pages: dict[int, PageInfo] = {}
    for part in parts:
        for page in part.pages:
            if part.parser == "native_pdf" and page.page_no in ocr_covered:
                continue
            base = decisions.get(page.page_no)
            if base is not None and part.parser != "native_pdf":
                # 保留原生检查的判定依据，便于复核「为什么这页走了 OCR」
                page = page.model_copy(
                    update={
                        "char_count": base.char_count,
                        "garbled_ratio": base.garbled_ratio,
                        "image_area_ratio": base.image_area_ratio,
                        "needs_ocr": True,
                        "width_pt": page.width_pt or base.width_pt,
                        "height_pt": page.height_pt or base.height_pt,
                    }
                )
            pages[page.page_no] = page

    flags: list[QualityFlag] = []
    for part in parts:
        for f in part.quality_flags:
            if f.code == "page_needs_ocr" and f.page_no in ocr_covered:
                continue
            flags.append(f)
    for page_no in plan.ocr_pages:
        if page_no not in ocr_covered:
            flags.append(QualityFlag(code="ocr_unavailable", page_no=page_no, detail="该页需要 OCR 但未取得结果"))

    def page_key(page_no: int | None) -> int:
        return page_no if page_no is not None else 10**9

    seg_items = [(page_key(s.anchor.page_no), i, j, s) for i, part in enumerate(parts) for j, s in enumerate(part.segments)]
    # 假解析器的片段没有页码（锚点为 none），按其所属页的块顺序归位会编造位置，这里保持在原顺序末尾
    seg_items.sort(key=lambda t: (t[0], t[1], t[2]))
    segments = tuple(s.model_copy(update={"reading_order": n}) for n, (_, _, _, s) in enumerate(seg_items))
    block_items = [(page_key(b.page_no), i, j, b) for i, part in enumerate(parts) for j, b in enumerate(part.blocks)]
    block_items.sort(key=lambda t: (t[0], t[1], t[2]))
    blocks = tuple(b.model_copy(update={"reading_order": n}) for n, (_, _, _, b) in enumerate(block_items))

    ocr_parts = [p for p in parts if p.parser != "native_pdf"]
    fingerprint = hashlib.sha256("|".join(f"{p.parser}:{p.config_fingerprint}" for p in parts).encode()).hexdigest()
    return ParseOutcome(
        parser="+".join(p.parser for p in parts),
        parser_version="+".join(p.parser_version for p in parts),
        config_fingerprint=fingerprint,
        provider_task_id=next((p.provider_task_id for p in ocr_parts if p.provider_task_id), None),
        is_fake=any(p.is_fake for p in parts),
        page_count=plan.page_count,
        pages=tuple(pages[k] for k in sorted(pages)),
        blocks=blocks,
        tables=tuple(t for p in parts for t in p.tables),
        segments=segments,
        embedded_images=tuple(img for p in parts for img in p.embedded_images),
        quality_flags=tuple(flags),
        # 子集 OCR 的 Markdown 只覆盖部分页，拼接会产生缺页的阅读副本；混合结果不提供
        markdown=parts[0].markdown if len(parts) == 1 else None,
        raw_payload=next((p.raw_payload for p in ocr_parts if p.raw_payload), None),
        preprocessing_manifest=next((p.preprocessing_manifest for p in parts if p.preprocessing_manifest),None),
    )


def require_ready(plan: ParsePlan) -> None:
    if not plan.ready:
        raise ParserUnavailable(plan.unavailable_reason or "解析能力不可用")
