"""专业审核输入注册；只发布有来源、时间与作用域的窄风险候选。"""
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewedRulePublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern="^hs_description_candidate$")
    reviewed_by: str = Field(min_length=1, max_length=160)
    review_reference: str = Field(min_length=1, max_length=500)
    source_url: str = Field(max_length=600)
    source_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    valid_from: date
    valid_before: date
    destination_country: str = Field(pattern="^[A-Z]{2}$")
    commodity: str = Field(min_length=1, max_length=100)
    # 完整货描词组白名单，避免管理员提供任意正则或仅凭宽泛单词猜归类。
    descriptions: list[str] = Field(min_length=1, max_length=100)
    allowed_six_digit_codes: list[str] = Field(min_length=1, max_length=100)
    publication_status: str = Field(default="pending", pattern="^(pending|approved|withdrawn)$")
    source_artifact: str | None = None
    review_artifact: str | None = None
    review_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    evidence_verified: bool = False

    @model_validator(mode="after")
    def valid(self):
        parsed = urlsplit(self.source_url)
        if self.valid_before <= self.valid_from or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("审核来源必须HTTPS且日期窗口有效")
        if not self.reviewed_by.strip() or not self.review_reference.strip():
            raise ValueError("审核人及审核记录引用不能为空白")
        if any(not re.fullmatch(r"\d{6}", item) for item in self.allowed_six_digit_codes):
            raise ValueError("仅接受审核范围内的HS六位码")
        if any(not item.strip() or len(item) > 200 for item in self.descriptions):
            raise ValueError("货描白名单不能为空或超过200字")
        return self


def freeze_publications(settings):
    frozen = []
    for item in settings.reviewed_rule_publications:
        pack = ReviewedRulePublication.model_validate(item)
        if pack.publication_status != "approved" or pack.valid_before <= datetime.now(UTC).date():
            continue
        host = urlsplit(pack.source_url).hostname
        allowed_hosts = {str(domain).strip().lower() for domain in settings.rule_official_domains}
        if not settings.rule_evidence_directory or host not in allowed_hosts:
            continue
        try:
            root = Path(settings.rule_evidence_directory).resolve()
            paths = [(root / name).resolve() for name in (pack.source_artifact, pack.review_artifact)]
            if any(not p.is_relative_to(root) or not p.is_file() or not 0 < p.stat().st_size <= 20_000_000 for p in paths):
                continue
            contents = []
            for path in paths:
                with path.open("rb") as artifact:
                    contents.append(artifact.read(20_000_001))
            source, record = contents
            if any(not data or len(data) > 20_000_000 for data in contents):
                continue
            if hashlib.sha256(source).hexdigest() != pack.source_sha256 or hashlib.sha256(record).hexdigest() != pack.review_sha256:
                continue
            review = json.loads(record)
            if not isinstance(review, dict):
                continue
            required = {"version": pack.version, "reviewed_by": pack.reviewed_by, "source_sha256": pack.source_sha256,
                "source_url": pack.source_url, "destination_country": pack.destination_country, "commodity": pack.commodity,
                "valid_from": pack.valid_from.isoformat(), "valid_before": pack.valid_before.isoformat(), "decision": "approved",
                "review_reference": pack.review_reference, "kind": pack.kind, "descriptions": pack.descriptions,
                "allowed_six_digit_codes": pack.allowed_six_digit_codes}
            qualification = review.get("reviewer_qualification_reference")
            if any(review.get(k) != value for k, value in required.items()) or not isinstance(qualification, str) or not qualification.strip():
                continue
        except (OSError, ValueError, TypeError, RuntimeError):
            continue
        # 不把服务器路径放入工作流、模型上下文或接口返回；仅保留可复核的版本与哈希。
        frozen.append(pack.model_copy(update={"source_artifact": None, "review_artifact": None,
            "evidence_verified": True}).model_dump(mode="json"))
    return frozen


def description_candidates(lines, sources, publications, jurisdiction_inputs):
    from app.review.core_v2 import direct_sources
    from app.review.rules import RuleResult
    source_by_id = {row["id"]: row for row in sources}
    facts = jurisdiction_inputs or {}
    outputs = []
    normalize = lambda value: " ".join(str(value).split()).casefold()
    for raw in publications or []:
        pack = ReviewedRulePublication.model_validate(raw)
        if pack.publication_status != "approved" or not pack.evidence_verified or pack.valid_before <= datetime.now(UTC).date():
            continue
        # 没有用户确认范围时不套用规则；不从港口/文件名/模型猜目的国或货类。
        if facts.get("destination_country") != pack.destination_country or facts.get("commodity") != pack.commodity:
            continue
        effective = facts.get("import_date")
        try:
            applicable_date = date.fromisoformat(effective) if isinstance(effective, str) else effective
        except ValueError:
            applicable_date = None
        if not applicable_date or not pack.valid_from <= applicable_date < pack.valid_before:
            continue
        descriptions = {normalize(item) for item in pack.descriptions}
        for line in lines:
            description = str(line.get("description") or "").strip()
            code = str(line.get("hs_code") or "").strip()
            if normalize(description) not in descriptions or not re.fullmatch(r"\d{6}(?:\d{2})?(?:\d{2})?", code):
                continue
            if not direct_sources(line, source_by_id, description) or not direct_sources(line, source_by_id, code):
                continue
            mismatch = code[:6] not in pack.allowed_six_digit_codes
            outputs.append(RuleResult("hs.reviewed_description_candidate", "failed" if mismatch else "undetermined",
                "原件明确货描及HS与已审核窄规则包不一致候选；请人工复核，不自动改码或给最终归类" if mismatch else "已审核词组范围未见编码冲突候选；不证明最终归类正确",
                tuple(line.get("source_segment_ids", [])), calculation={"publication": raw, "line_id": line.get("id"), "description": description,
                    "source_code": code, "candidate_only": True, "final_classification": "not_assessed"}, severity="yellow", blocks_export=False))
    return outputs


def publication_export_state(publications, settings):
    """冻结依据保留历史，已明确撤回的版本不能用于新的交付。"""
    declared = {item["version"]: ReviewedRulePublication.model_validate(item)
        for item in settings.reviewed_rule_publications}
    result = []
    for raw in publications or []:
        frozen = ReviewedRulePublication.model_validate(raw)
        current = declared.get(frozen.version)
        status = "withdrawn" if current and current.publication_status == "withdrawn" else (
            "expired" if frozen.valid_before <= datetime.now(UTC).date() else "frozen")
        result.append({"version": frozen.version, "source_sha256": frozen.source_sha256, "status": status})
    return result
