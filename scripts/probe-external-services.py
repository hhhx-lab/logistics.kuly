#!/usr/bin/env python
"""
外部服务协议探测（方案 D0 要求）。

「OpenAI 兼容」不代表结构化输出、图片输入都可用，必须逐项实测并记录能力位。
本脚本只用合成样件，不发送任何真实单证内容。

用法：cd src/backend && .venv/bin/python ../../scripts/probe-external-services.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import zipfile
from dataclasses import dataclass, field

import httpx

from app.config import ModelRole, get_settings


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


def _synthetic_invoice_pdf() -> bytes:
    """合成一份含字段与表格行的发票，避免用真实客户材料探测。"""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(70, 760, "COMMERCIAL INVOICE")
    c.setFont("Helvetica", 11)
    rows = [
        ("Invoice No.", "INV-2026-0001"),
        ("Shipper", "NINGBO TEXTILE CO LTD"),
        ("Consignee", "HAMBURG IMPORT GMBH"),
        ("Gross Weight", "1250.5 KGS"),
    ]
    for i, (k, v) in enumerate(rows):
        c.drawString(70, 720 - i * 22, f"{k}:")
        c.drawString(210, 720 - i * 22, v)
    c.drawString(70, 600, "No.  Description        Qty   Unit Price   Amount")
    c.drawString(70, 580, "1    COTTON TOWEL       100   12.50        1250.00")
    c.save()
    return buf.getvalue()


def _png_with_text(text: str) -> bytes:
    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Helvetica", 36)
    c.drawString(80, 700, text)
    c.save()
    doc = pdfium.PdfDocument(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    doc[0].render(scale=2).to_pil().save(out, format="PNG")
    return out.getvalue()


def probe_model_role(role: ModelRole) -> ProbeResult:
    settings = get_settings()
    try:
        profile = settings.model_profile(role)
    except ValueError as exc:
        return ProbeResult(f"模型/{role.value}", False, str(exc))
    if profile is None:
        return ProbeResult(f"模型/{role.value}", False, "未配置")

    url = profile.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {profile.api_key.get_secret_value()}"}
    caps: dict[str, bool] = {}
    notes: list[str] = []

    def post(body: dict, timeout: int = 120) -> dict | None:
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    base = post({"model": profile.name, "messages": [{"role": "user", "content": "只回答两个字：收到"}], "max_tokens": 600})
    caps["chat"] = base is not None
    if base is None:
        return ProbeResult(f"模型/{role.value}", False, "基础对话不可用", caps)

    usage = base.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    if reasoning:
        notes.append(f"推理型模型：本次 reasoning_tokens={reasoning}，输出预算需放宽")

    d = post(
        {
            "model": profile.name,
            "messages": [{"role": "system", "content": "只输出 JSON"}, {"role": "user", "content": '返回 {"a":"1"}'}],
            "response_format": {"type": "json_object"},
            "max_tokens": 800,
        }
    )
    caps["json_object"] = False
    if d:
        try:
            json.loads(d["choices"][0]["message"]["content"])
            caps["json_object"] = True
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    d = post(
        {
            "model": profile.name,
            "messages": [{"role": "user", "content": "字段 a 值 1"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "f",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"a": {"type": "string"}},
                        "required": ["a"],
                        "additionalProperties": False,
                    },
                },
            },
            "max_tokens": 1500,
        }
    )
    caps["json_schema"] = bool(d)

    # 只有视觉角色需要图片输入；其余角色不因缺这一项判为不可用
    if role is ModelRole.VISION:
        marker = "INV-77881"
        b64 = base64.b64encode(_png_with_text(marker)).decode()
        d = post(
            {
                "model": profile.name,
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "图中的编号是什么？只输出编号本身。"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    }
                ],
            }
        )
        caps["image_input"] = bool(d and marker in (d["choices"][0]["message"].get("content") or ""))

    return ProbeResult(f"模型/{role.value}", True, "；".join(notes) or f"模型 {profile.name}", caps)


def probe_mineru() -> ProbeResult:
    settings = get_settings()
    if not (settings.mineru_base_url and settings.mineru_api_key):
        return ProbeResult("解析/MinerU", False, "未配置")
    base = settings.mineru_base_url.rstrip("/")
    token = settings.mineru_api_key.get_secret_value().strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    caps: dict[str, bool] = {}

    try:
        r = httpx.post(
            f"{base}/file-urls/batch",
            headers=headers,
            timeout=60,
            json={
                "model_version": settings.mineru_model_version,
                "enable_formula": False,
                "enable_table": True,
                "language": settings.mineru_language,
                "files": [{"name": "probe_invoice.pdf", "is_ocr": True}],
            },
        )
        body = r.json()
        if body.get("code") != 0:
            return ProbeResult("解析/MinerU", False, f"申请上传失败：{body.get('msg')}")
        batch_id = body["data"]["batch_id"]
        put_url = body["data"]["file_urls"][0]
        caps["auth"] = True

        # MinerU 的直传地址不接受额外请求头
        if httpx.put(put_url, content=_synthetic_invoice_pdf(), timeout=180).status_code != 200:
            return ProbeResult("解析/MinerU", False, "直传失败", caps)
        caps["presigned_put"] = True

        # 异步任务：必须能按 batch_id 查状态，否则恢复时无法核查是否已受理
        for _ in range(40):
            time.sleep(6)
            q = httpx.get(f"{base}/extract-results/batch/{batch_id}", headers=headers, timeout=60).json()
            results = (q.get("data") or {}).get("extract_result") or []
            if not results:
                continue
            state = results[0].get("state")
            if state == "done":
                caps["async_task_query"] = True
                zurl = results[0].get("full_zip_url")
                z = zipfile.ZipFile(io.BytesIO(httpx.get(zurl, timeout=300).content))
                names = z.namelist()
                target = next((n for n in names if n.endswith("_content_list.json")), None)
                if not target:
                    return ProbeResult("解析/MinerU", False, "结果缺少 content_list", caps)
                blocks = json.loads(z.read(target))
                with_bbox = sum(1 for b in blocks if b.get("bbox"))
                caps["bbox"] = with_bbox == len(blocks) and len(blocks) > 0
                caps["page_index"] = all("page_idx" in b for b in blocks)
                caps["markdown"] = "full.md" in names
                return ProbeResult(
                    "解析/MinerU",
                    True,
                    f"内容块 {len(blocks)}，带 bbox {with_bbox}；结果含 {', '.join(sorted(set(n.split('_')[-1] for n in names))[:4])}",
                    caps,
                )
            if state in ("failed", "error"):
                return ProbeResult("解析/MinerU", False, f"解析失败：{results[0]}", caps)
        return ProbeResult("解析/MinerU", False, "轮询超时", caps)
    except httpx.HTTPError as exc:
        return ProbeResult("解析/MinerU", False, f"{type(exc).__name__}: {exc}", caps)


def main() -> int:
    print("=" * 72)
    print("外部服务协议探测（仅用合成样件，不发送真实单证）")
    print("=" * 72)
    results = [probe_model_role(role) for role in ModelRole]
    results.append(probe_mineru())

    for r in results:
        mark = "可用" if r.ok else "不可用"
        print(f"\n[{mark}] {r.name}")
        if r.detail:
            print(f"  说明: {r.detail}")
        if r.capabilities:
            for k, v in r.capabilities.items():
                print(f"  - {k}: {'支持' if v else '不支持'}")

    failed = [r.name for r in results if not r.ok]
    print("\n" + "=" * 72)
    if failed:
        print(f"以下服务不可用：{', '.join(failed)}。相关工作流须显示禁用原因，不能用假成功充数。")
        return 1
    print("全部服务实测可用。可把 PARSER_ADAPTER 与 MODEL_ADAPTER 切为真实实现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
