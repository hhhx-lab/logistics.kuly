"""
MinerU 真实协议验证（付费，默认跳过）。

运行：cd src/backend && .venv/bin/python -m pytest tests/test_mineru_external.py -m external -q
只发送脚本内合成的样件，不含任何真实单证内容；一次运行约消耗 1 页解析额度。
"""

from __future__ import annotations

import io
import time

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.config import AdapterKind, get_settings
from app.documents.parsing import MinerUAdapter, ParserUnavailable

pytestmark = pytest.mark.external


def _synthetic_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(100, 700, "COMMERCIAL INVOICE INV-2026-0001")
    c.drawString(100, 660, "Shipper: NINGBO TEXTILE CO LTD")
    c.save()
    return buf.getvalue()


def test_mineru_real_protocol_roundtrip() -> None:
    settings = get_settings()
    if settings.parser_adapter is not AdapterKind.MINERU:
        pytest.skip("PARSER_ADAPTER 不是 mineru")
    try:
        adapter = MinerUAdapter(settings)
    except ParserUnavailable:
        pytest.skip("MinerU 未配置")

    persisted: list[str] = []
    batch_id = adapter.submit(_synthetic_pdf(), "external_probe.pdf", on_task_id=persisted.append)
    assert persisted == [batch_id]

    deadline = time.monotonic() + 300
    status = adapter.get_status(batch_id)
    while not status.terminal and time.monotonic() < deadline:
        time.sleep(5)
        status = adapter.get_status(batch_id)
    assert status.state == "done", status.error_message

    outcome = adapter.normalize(adapter.fetch_result(status))
    text = " ".join(s.text for s in outcome.segments)
    assert "INV-2026-0001" in text
    rects = [s for s in outcome.segments if s.anchor.precision == "rect"]
    assert rects, "实测应返回可校准 bbox"
    # 与原生坐标约定一致：x=100pt 的文字左边界约在 0.17 附近
    assert any(abs(s.anchor.rect.x0 - 100 / A4[0]) < 0.03 for s in rects)
