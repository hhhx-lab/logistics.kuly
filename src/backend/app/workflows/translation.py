"""同步翻译节点：固定版本、来源多对多映射、确定性 QA 与逐源译文草稿。"""
from __future__ import annotations
from app.languages import language_name

import html
import re
from collections import Counter
from html.parser import HTMLParser
from uuid import UUID
from decimal import Decimal,InvalidOperation

from sqlalchemy import func, select

from app.config import ModelRole
from app.documents import evidence_models as em
from app.documents.preprocessing import assess_language,vision_candidate
from app.documents.models import Case,DocumentVersion,StoredObject
from app.documents.parsing.dto import NormRect
from app.infra.model_gateway import CallBudget, Failed, NeedsReconcile, Succeeded, StepExecutionCallLedger, build_model_gateway, model_call_timeout
from app.prompts.registry import PromptBudgetExceeded
from app.prompts.schemas import SemanticQaResult, GuidedSemanticQaResult, CategorizedSemanticQaResult, IssueAssessmentResult, SegmentRevisionResult, TranslationResult
from app.review import models as rm
from app.review.rules import evaluate_rules
from app.workflows.contracts import ModelBinding
from app.workflows.graphs import build_translate_review_graph
from app.workflows.models import InputRequest, InputRequestStatus, RunStatus, StepExecution
from app.prompts.registry import estimate_tokens

_TOKENS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\b[A-Za-z][A-Za-z0-9._:/-]*\d[A-Za-z0-9._:/-]*\b|\b[A-Za-z]*\d[A-Za-z0-9./:_-]*\b|\b(?:USD|EUR|CNY|RMB|JPY|GBP|HKD)\b")
_HOLDER = re.compile(r"⟦(P\d+)⟧")
_TRANSLATABLE_LABELS = {"commercial", "invoice", "packing", "list", "bill", "lading", "seller", "buyer", "shipper", "consignee", "notify", "port", "loading", "discharge", "quantity", "description", "goods", "total", "amount", "weight", "date", "cartons", "packages", "price", "origin", "receipt", "destination", "vessel", "voyage", "container", "seal"}


def translation_language_defects(source_text: str, translated_text: str, target_language: str) -> list[str]:
    """Independent of model declarations; identifiers and proper-name-only rows may remain.

    This is a conservative blocker for obvious untranslated prose, not a claim of
    semantic correctness. A model or human edit cannot bypass it by changing state.
    """
    source = _TOKENS.sub(" ", source_text)
    target = _TOKENS.sub(" ", translated_text)
    source_chinese = re.findall(r"[\u4e00-\u9fff]", source)
    target_chinese = re.findall(r"[\u4e00-\u9fff]", target)
    words = re.findall(r"[^\W\d_]{2,}", source, re.UNICODE)
    neutral_words = {"kg","g","mg","lb","lbs","mt","cbm","cm","mm","m","pcs","eur","usd","cny", "de","des","du","la","le","les","del","el","da","van","von"}
    words = [word for word in words if word.casefold() not in neutral_words]
    assessment = assess_language(source_text)
    copied = re.sub(r"\W+", "", source).casefold() == re.sub(r"\W+", "", target).casefold()
    if assessment.language in {"fr", "es", "ru"} and assessment.language != target_language and not assessment.uncertain and copied and len(re.findall(r"[^\W\d_]{2,}", source, re.UNICODE)) >= 4:
        return ["source_copied"]
    if target_language in {"fr", "es", "ru"}:
        # 同语材料可保留；短标签或专名不能仅因字符形态被硬判为翻译失败。
        if assessment.language == target_language and not assessment.uncertain:return []
        normalize = lambda value: re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()
        prose = len(re.findall(r"[^\W\d_]{2,}", source, re.UNICODE)) >= 4
        defects = ["source_copied"] if prose and assessment.language and not assessment.uncertain and normalize(source) == normalize(target) else []
        if target_language == "ru" and prose and assessment.language != "ru" and not re.search(r"[\u0400-\u04ff]", target):
            defects.append("target_language_missing")
        return defects
    if target_language == "zh-CN":
        translatable = not source_chinese and bool(words) and (
            any(word.lower() in _TRANSLATABLE_LABELS or word.islower() for word in words)
            or len([word for word in words if word.isupper() and len(word) >= 5]) >= 2
        )
        missing_language = translatable and not target_chinese
    else:
        translatable = len(source_chinese) >= 2
        missing_language = translatable and not re.search(r"[A-Za-z]{2,}", target)
    if not translatable:
        return []
    normalize = lambda value: re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()
    defects = ["target_language_missing"] if missing_language else []
    if normalize(source) == normalize(target):
        defects.append("source_copied")
    if target_language == "zh-CN":
        meaningful = Counter(word.lower() for word in words if word.lower() in _TRANSLATABLE_LABELS or word.islower())
        retained = Counter(word.lower() for word in re.findall(r"[A-Za-z]{2,}", target))
        total = sum(meaningful.values())
        if total >= 2 and sum((meaningful & retained).values()) / total >= .8:
            defects.append("source_copy_rate")
    if target_language == "en" and len(target_chinese) >= 2 and "".join(source_chinese) in "".join(target_chinese):
        if "source_copied" not in defects:
            defects.append("source_copied")
    return defects


def adopted_value_translation_reason(original:str,adopted:str,translated:str,*,numeric_value=None)->str|None:
    """Human business assertions remain separate from immutable source translation."""
    if original.strip()==adopted.strip():return None
    def numbers(text):
        values=[]
        for token in re.findall(r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9.])",text):
            try:values.append(Decimal(token))
            except InvalidOperation:pass
        return Counter(values)
    required=numbers(adopted);previous=numbers(original);actual=numbers(translated)
    if numeric_value is not None:
        required=Counter([Decimal(str(numeric_value))])
    if required and (numeric_value is not None or required!=previous):
        if any(actual[value]<count for value,count in required.items()):return "adopted_numeric_value_absent"
        return None
    # Literal identifiers need exact token boundaries, not substring evidence.
    protected=_TOKENS.findall(adopted)
    if protected and set(protected)!=set(_TOKENS.findall(original)):
        if any(not re.search(r"(?<![A-Za-z0-9])"+re.escape(value)+r"(?![A-Za-z0-9])",translated) for value in protected):return "adopted_protected_value_absent"
        return None
    return "changed_business_text_requires_review"


def bounded_batches(rows, text, group=lambda row: None, *, max_items=24, max_tokens=7000):
    current=[];size=0;previous=None
    for row in rows:
        estimate=estimate_tokens(text(row))+128
        if estimate>max_tokens:
            raise PromptBudgetExceeded("单段内容超过有界批次预算，须先按结构拆分")
        marker=group(row)
        if current and (marker!=previous or len(current)>=max_items or size+estimate>max_tokens):
            yield current;current=[];size=0
        current.append(row);size+=estimate;previous=marker
    if current:yield current


def sanitize_html(contents: str) -> str:
    """仅保留静态阅读元素及本地 DOM ID；脚本、外链、事件属性、样式全部丢弃。"""
    class Sanitizer(HTMLParser):
        allowed = {"html","head","body","title","section","article","div","p","span","strong","em","br","h1","h2","h3","table","thead","tbody","tr","td","th","ul","ol","li","hr"}
        blocked = {"script","style","iframe","object","svg","math"}
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.output=[];self.depth=0
        def handle_starttag(self, tag, attrs):
            if tag in self.blocked:
                self.depth+=1
            elif not self.depth and tag in self.allowed:
                identifier=next((v for k,v in attrs if k=="id" and v and re.fullmatch(r"[A-Za-z0-9_-]+",v)),None)
                safe = f' id="{identifier}"' if identifier else ''
                for name,value in attrs:
                    if name in {"data-source-segment-ids", "data-translation-segment-ids"} and value:
                        ids = value.split()
                        try:
                            ids = [str(UUID(identifier)) for identifier in ids]
                        except ValueError:
                            ids = []
                        if ids:
                            safe += f' {name}="{" ".join(ids)}"'
                    if name in {"rowspan","colspan"} and tag in {"td","th"} and value and value.isdigit() and 1<=int(value)<=1000:safe+=f' {name}="{value}"'
                    if name=="class" and value in {"document-page","translation-only","bilingual"}:safe+=f' class="{value}"'
                self.output.append('<'+tag+safe+'>')
        def handle_endtag(self, tag):
            if tag in self.blocked and self.depth:
                self.depth-=1
            elif not self.depth and tag in self.allowed:
                self.output.append('</'+tag+'>')
        def handle_data(self,data):
            if not self.depth:
                self.output.append(html.escape(data))
    parser=Sanitizer();parser.feed(contents);parser.close()
    return ''.join(parser.output)


def protect_text(text: str) -> tuple[str, dict[str, str]]:
    tokens = {}
    def replace(match):
        key = f"P{len(tokens) + 1}"
        tokens[key] = match.group(0)
        return f"⟦{key}⟧"
    return _TOKENS.sub(replace, text), tokens


def restore_translation(text: str, tokens: dict[str, str]) -> tuple[str, list[str]]:
    found = Counter(_HOLDER.findall(text))
    defects = [key for key in tokens if found[key] != 1]
    defects.extend(key for key in found if key not in tokens)
    return _HOLDER.sub(lambda m: tokens.get(m.group(1), m.group(0)), text), defects


def reading_html(source_rows: list[dict], translated_rows: list[dict], *, synthetic: bool) -> str:
    by_source = {}
    for row in translated_rows:
        for source_id in row["source_segment_ids"]:
            by_source.setdefault(source_id, []).append(row)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>双语复核草稿</title><body><p>待确认双语草稿，仅供复核</p>']
    if synthetic:
        parts.append('<strong>假模型输出，非真实翻译</strong>')
    for source in source_rows:
        parts.append(f'<section id="source-{html.escape(source["id"], quote=True)}"><p>{html.escape(source["text"])}</p>')
        matches = by_source.get(source["id"], [])
        for target in matches:
            parts.append(f'<p id="translation-{html.escape(target["id"], quote=True)}">{html.escape(target["text"])}</p>')
        if not matches:
            parts.append('<p>原文保留／未翻译（不属于已确认范围）</p>')
        parts.append('</section>')
    return ''.join(parts) + '</body></html>'


class TranslationNodes:
    def __init__(self, worker):
        self.worker = worker

    def __getattr__(self, key):
        return getattr(self.worker, key)

    def _call(self, step_id, key, variables, model, role):
        from app.workflows.worker import ModelFailure
        ref = self.prompt_versions.get(key)
        if ref is None:
            raise ModelFailure("unavailable", "任务未冻结所需提示词")
        prompt = self.registry.get_frozen(key, ref["version"], ref["sha256"]).render(**variables)
        binding = next((ModelBinding.model_validate(b) for b in self.bindings if b["role"] == role.value), None)
        if binding is None:
            raise ModelFailure("unavailable", "任务未冻结所需模型")
        budget=self.worker.model_budget(binding)
        self.ensure_time(model_call_timeout(self.settings, binding))
        self.submit_intent(step_id)
        from app.workflows.costs import PricedLedger
        gateway = build_model_gateway(self.settings, ledger=PricedLedger(StepExecutionCallLedger(self.factory, tenant_id=self.tenant, step_execution_id=step_id,
            lease_token=self.lease, execution_fence=self.fence), binding, budget))
        try:
            result = self.runner.run(gateway.structured_generate(binding, [prompt], model, self.request_key(step_id), budget=budget))
            if isinstance(result,Succeeded):self.worker.record_model_response(step_id,result)
        finally:
            self.runner.run(gateway.aclose())
        if isinstance(result, NeedsReconcile):
            raise ModelFailure("acceptance_unknown", "模型受理状态待核查", unknown=True)
        if isinstance(result, Failed):
            raise ModelFailure(result.kind, result.message)
        return result

    def load_review(self,state):
        from app.workflows.models import ExecutionEpoch
        result=self.worker.load_spec(state)
        with self.transaction() as db:
            run=self.locked(db);epoch=db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==self.epoch))
            self.repair_request=epoch.repair_request
            self.business_id=run.current_business_revision_id;self.translation_id=run.current_translation_revision_id
            revision=db.get(rm.TranslationRevision,self.translation_id) if self.translation_id else None
            self.parse_ids=[UUID(i) for i in revision.parse_artifact_ids] if revision else [UUID(i) for i in epoch.input_parse_artifact_ids]
            self.scope_ids=[UUID(i) for i in revision.resolved_scope_ids] if revision else []
        return result

    def _comparison_review_revision(self, db, run, revision):
        """比较重算只迁移复核基线，固定源译片段及历史版本保持不变。"""
        from app.consignment import models as cm
        from app.consignment.service import binding_incompatibility
        from app.workflows.worker import ModelFailure, digest
        request = getattr(self, "repair_request", {}) or {}
        if not request.get("comparison_batch_id"):
            return revision
        batch = db.get(cm.ComparisonBatch, UUID(request["comparison_batch_id"]))
        case = db.get(Case, self.case_id)
        if (getattr(self, "purpose", None) != "quality_preview" or run.workflow_version < 2
                or batch is None or batch.tenant_id != self.tenant or batch.case_id != self.case_id
                or batch.business_revision_id != self.business_id or self.input_business_id != self.business_id):
            raise ModelFailure("revision_stale", "核对批次与本次复核业务基线不一致")
        reason = binding_incompatibility(db, case, run, batch)
        if reason:
            raise ModelFailure("revision_stale", "核对依赖已变化，请创建新翻译任务：" + reason)
        if revision is None or revision.tenant_id != self.tenant or revision.case_id != self.case_id or revision.run_id != run.id:
            raise ModelFailure("translation_missing", "没有可沿用的译文，须先完成翻译任务")
        frozen_parses = {UUID(i) for i in self.input_parse_ids}
        scopes = [db.get(rm.ResolvedTranslationScope, UUID(i)) for i in revision.resolved_scope_ids]
        if (not scopes or len(scopes) != len(self.documents)
                or set(revision.parse_artifact_ids) != {str(i) for i in frozen_parses}
                or revision.target_language != self.spec["target_language"]
                or any(scope is None or scope.tenant_id != self.tenant or scope.case_id != self.case_id
                    or scope.run_id != run.id or scope.business_revision_id != revision.business_revision_id
                    or scope.parse_artifact_id not in frozen_parses for scope in scopes)
                or {(scope.document_id, scope.document_version) for scope in scopes} != set(self.documents)):
            raise ModelFailure("revision_stale", "沿用译文范围与冻结交付解析不一致")
        for scope in scopes:
            parse = db.get(em.ParseArtifact, scope.parse_artifact_id)
            sources = list(db.scalars(select(em.SourceSegment).where(
                em.SourceSegment.id.in_([UUID(i) for i in scope.source_segment_ids]))))
            if (parse is None or (parse.document_id, parse.document_version) != (scope.document_id, scope.document_version)
                    or {str(source.id) for source in sources} != set(scope.source_segment_ids)
                    or any(source.parse_artifact_id != scope.parse_artifact_id or source.case_id != self.case_id
                        or str(source.section_id) not in scope.source_section_ids for source in sources)):
                raise ModelFailure("invalid_source_mapping", "沿用译文范围含失效来源，未发布新复核版")
        source_ids = {i for scope in scopes for i in scope.source_segment_ids}
        segments = list(db.scalars(select(rm.TranslationSegment).where(
            rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]))))
        if ({str(segment.id) for segment in segments} != set(revision.segment_ids)
                or any(segment.tenant_id != self.tenant or segment.case_id != self.case_id or segment.run_id != run.id
                    or segment.parse_artifact_id not in frozen_parses or segment.target_language != revision.target_language
                    or not set(segment.source_segment_ids).issubset(source_ids) for segment in segments)):
            raise ModelFailure("invalid_source_mapping", "沿用译文片段与冻结来源不一致，未发布新复核版")
        if revision.business_revision_id == self.business_id:
            self.scope_ids = [scope.id for scope in scopes]
            return revision
        new_scopes = []
        for scope in scopes:
            current = rm.ResolvedTranslationScope(tenant_id=self.tenant, case_id=self.case_id, run_id=run.id,
                document_id=scope.document_id, document_version=scope.document_version, parse_artifact_id=scope.parse_artifact_id,
                business_revision_id=self.business_id, selector_version=scope.selector_version,
                source_segment_ids=list(scope.source_segment_ids), source_section_ids=list(scope.source_section_ids),
                fingerprint=digest({"parent_scope":str(scope.id), "source_fingerprint":scope.fingerprint,
                    "business_revision_id":str(self.business_id), "comparison_input":batch.input_fingerprint}),
                confirmed_by=scope.confirmed_by)
            db.add(current);db.flush();new_scopes.append(current)
        number = (db.scalar(select(func.max(rm.TranslationRevision.revision_no)).where(
            rm.TranslationRevision.run_id == run.id)) or 0) + 1
        current = rm.TranslationRevision(tenant_id=self.tenant, case_id=self.case_id, run_id=run.id,
            business_revision_id=self.business_id, target_language=revision.target_language, revision_no=number,
            parent_revision_id=revision.id, parse_artifact_ids=list(revision.parse_artifact_ids),
            resolved_scope_ids=[str(scope.id) for scope in new_scopes], segment_ids=list(revision.segment_ids),
            prompt_versions=dict(revision.prompt_versions), model_bindings=list(revision.model_bindings),
            glossary_version=revision.glossary_version, qa_status="not_run", qa_summary={},
            dependency_fingerprint=digest({"parent_revision":str(revision.id), "translation_input":revision.dependency_fingerprint,
                "business_revision_id":str(self.business_id), "scopes":[scope.fingerprint for scope in new_scopes]}),
            is_synthetic=revision.is_synthetic, created_by=revision.created_by)
        db.add(current);db.flush()
        run.current_translation_revision_id=current.id
        self.translation_id=current.id;self.scope_ids=[scope.id for scope in new_scopes]
        return current

    def assess_issue(self, state):
        from app.workflows.worker import ModelFailure, append_event
        from app.review.issue_skill import guidance_for, source_targets, skill_instructions
        from app.conversation.safety import contains_internal_information
        request = self.repair_request
        self.prompt_versions = {**self.prompt_versions, "document.issue_assess": request["prompt"]}
        with self.transaction() as db:
            run = self.locked(db)
            issue = db.get(rm.Issue, UUID(request["issue_id"]))
            allowed = []
            for target in source_targets(issue):
                ids = [target.get("source_segment_id")]
                if target.get("field_id"):
                    field = db.get(em.FieldAssertion, UUID(target["field_id"]))
                    if field and field.case_id == self.case_id and field.parse_artifact_id in self.parse_ids:
                        ids.extend(field.source_segment_ids)
                for source_id in ids:
                    if not source_id: continue
                    source = db.get(em.SourceSegment, UUID(str(source_id)))
                    if source and source.case_id == self.case_id and source.parse_artifact_id in self.parse_ids and str(source.id) not in allowed:
                        allowed.append(str(source.id))
            # 没有具体来源时只读取冻结解析中的有限正文，不能借其他票或新版补证。
            if not allowed:
                allowed = [str(value) for value in db.scalars(select(em.SourceSegment.id).where(
                    em.SourceSegment.case_id == self.case_id, em.SourceSegment.parse_artifact_id.in_(self.parse_ids)).order_by(em.SourceSegment.ordinal).limit(20))]
            from app.documents.extraction import field_schema
            materials = []
            for value in allowed[:40]:
                segment = db.get(em.SourceSegment, UUID(value))
                logical = db.get(em.LogicalDocument, segment.logical_document_id)
                profile = self.confirmed_type(db, logical) if logical else "unknown"
                cargos = self.confirmed_cargo_profiles(db, logical) if logical else []
                materials.append({"source_segment_id": value, "text": segment.text[:800], "document_profile": profile,
                    "cargo_profile_ids": cargos, "review_focus": field_schema(profile, cargos)["review_focus"]})
            guidance = guidance_for(issue, {key: request[key] for key in
                ("recommendation", "replacement_text", "recommended_action") if key in request})
            content_guidance = {key: guidance[key] for key in ("category_label", "impact", "recommendation", "recommended_action")}
            variables = {"issue_rules": skill_instructions(), "issue": {"category": issue.category, "severity": issue.severity,
                "description": issue.description, "guidance": content_guidance, "source_segment_ids": allowed[:40]},
                "materials": materials, "instruction": request["instruction"]}
        def assess(step_id):
            result = self._call(step_id, "document.issue_assess", variables, IssueAssessmentResult, ModelRole.EXTRACT)
            answer = result.value
            if not set(answer.source_segment_ids).issubset(set(allowed[:40])):
                raise ModelFailure("invalid_source_mapping", "Agent 建议引用了未授权资料，未发布")
            if contains_internal_information(answer.explanation + answer.recommendation):
                raise ModelFailure("sensitive_output", "Agent 建议包含内部信息，未发布")
            with self.transaction() as db:
                run = self.locked(db); issue = db.get(rm.Issue, UUID(request["issue_id"]))
                if not issue.comparison_batch_id and int((issue.guidance or {}).get("agent_request_no", 0)) != request["agent_request_no"]:
                    raise ModelFailure("revision_stale", "疑点处理请求已变化，未覆盖新结果")
                assessment = {"agent_status": "succeeded",
                    "agent_response": answer.explanation, "recommendation": answer.recommendation,
                    "replacement_text": None,
                    "agent_source_segment_ids": answer.source_segment_ids, "recommended_action": answer.recommended_action}
                if issue.comparison_batch_id:
                    from app.workflows.models import ExecutionEpoch
                    epoch = db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id, ExecutionEpoch.epoch == self.epoch))
                    epoch.repair_request = {**epoch.repair_request, **assessment}
                else:
                    issue.guidance = {**(issue.guidance or {}), **assessment}
                case = db.get(Case, self.case_id); case.review_state_version += 1
                self.complete_step(db, step_id, [])
            return []
        self.step("疑点材料分析:" + request["issue_id"], request, assess)
        with self.transaction() as db:
            run = self.locked(db)
            self.require_settled_steps(db)
            run.status = RunStatus(request["return_status"])
            run.current_stage = "疑点分析完成，等待人工处理"
            append_event(db, run, "run_status", "Agent 已提供处理建议；没有自动修改原件、字段或关闭疑点", {"issue_id": request["issue_id"]})
        return {}

    def repair_local(self,state):
        from datetime import UTC,datetime
        from app.workflows.worker import digest,ModelFailure
        request=self.repair_request
        self.prompt_versions={**self.prompt_versions,"document.revise":request["prompt"]}
        with self.transaction() as db:
            self.locked(db);segment=db.get(rm.TranslationSegment,UUID(request["segment_id"]))
            issue=db.get(rm.Issue,UUID(request["issue_id"]))
            source="\n".join(db.get(em.SourceSegment,UUID(i)).text for i in segment.source_segment_ids)
            variables={"target_language":segment.target_language,"source_text":source,"translated_text":segment.translated_text,
                "issue":{"description":issue.description,"guidance":issue.guidance,"sources":segment.source_segment_ids},"instruction":request["instruction"],"protected_values":list(segment.protected_tokens.values())}
            if request["prompt"]["version"]>=2:
                from app.review.issue_skill import skill_instructions
                variables["issue_rules"]=skill_instructions()
        def apply(step_id):
            result=self._call(step_id,"document.revise",variables,SegmentRevisionResult,ModelRole.TRANSLATE)
            from app.conversation.safety import contains_internal_information
            text=result.value.translated_text
            if contains_internal_information(text + result.value.explanation):raise ModelFailure("sensitive_output","修改内容包含内部信息，未应用")
            if not text.strip():raise ModelFailure("empty_translation", "修改结果为空，未覆盖原译文")
            if any(value not in text for value in variables["protected_values"]):
                raise ModelFailure("protected_value_changed", "修改结果丢失单号、金额等保护值，未覆盖原译文")
            with self.transaction() as db:
                run=self.locked(db);old=db.get(rm.TranslationRevision,UUID(request["translation_revision_id"]))
                issue=db.get(rm.Issue,UUID(request["issue_id"]))
                if request.get("agent_request_no") and int((issue.guidance or {}).get("agent_request_no", 0)) != request["agent_request_no"]:
                    raise ModelFailure("revision_stale", "疑点请求已更新，未覆盖新结果")
                if run.current_translation_revision_id!=old.id:raise ModelFailure("revision_stale","原译文已变化，未覆盖新版")
                previous=db.get(rm.TranslationSegment,UUID(request["segment_id"]))
                if text.strip() == previous.translated_text.strip():
                    raise ModelFailure("revision_unchanged", "Agent 未产生译文修改，疑点保持待处理。" + result.value.explanation)
                edited=rm.TranslationSegment(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,document_id=previous.document_id,document_version=previous.document_version,
                    parse_artifact_id=previous.parse_artifact_id,section_id=previous.section_id,target_language=previous.target_language,ordinal=previous.ordinal,source_segment_ids=previous.source_segment_ids,
                    translated_text=text,protected_tokens=previous.protected_tokens,state="human_revised",supersedes_segment_id=previous.id,created_by=UUID(request["actor_id"]),reason="用户意见："+request["instruction"],dependency_fingerprint=digest({"old":str(previous.id),"text":text}))
                db.add(edited);db.flush()
                new=rm.TranslationRevision(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=old.business_revision_id,target_language=old.target_language,
                    revision_no=old.revision_no+1,parent_revision_id=old.id,parse_artifact_ids=old.parse_artifact_ids,resolved_scope_ids=old.resolved_scope_ids,
                    segment_ids=[str(edited.id) if i==str(previous.id) else i for i in old.segment_ids],prompt_versions={**old.prompt_versions,"document.revise":request["prompt"]},model_bindings=old.model_bindings,
                    glossary_version=old.glossary_version,qa_status="not_run",qa_summary={},dependency_fingerprint=digest({"old":str(old.id),"edited":str(edited.id)}),is_synthetic=old.is_synthetic,created_by=UUID(request["actor_id"]))
                db.add(new);db.flush()
                for sid in edited.source_segment_ids:db.add(rm.SourceTranslationLink(tenant_id=self.tenant,source_segment_id=UUID(sid),translation_segment_id=edited.id))
                for preview in db.scalars(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,rm.PreviewArtifact.status=="valid")):preview.status="stale"
                run.current_translation_revision_id=new.id
                case=db.get(Case,self.case_id);case.review_state_version+=1
                issue.guidance = {**(issue.guidance or {}), "agent_status": "succeeded", "agent_response": result.value.explanation}
                db.add(rm.IssueDisposition(tenant_id=self.tenant,case_id=self.case_id,issue_id=issue.id,business_revision_id=self.business_id,action="revise",reason=f"译文v{old.revision_no}修订为v{new.revision_no}：{request['instruction']}",review_sequence=case.review_state_version,decided_by=UUID(request["actor_id"])))
                self.complete_step(db,step_id,[new.id]);return [new.id]
        ids=self.step("疑点局部修订:"+request["issue_id"],request,apply)
        self.translation_id=UUID(str(ids[0]))
        return {}

    def _input(self, db, run, parse, kind, question, options, source_refs):
        from app.workflows.worker import append_event
        active = db.scalar(select(InputRequest).where(InputRequest.run_id == run.id, InputRequest.status == InputRequestStatus.ACTIVE))
        if active is None:
            db.add(InputRequest(tenant_id=self.tenant, run_id=run.id, kind=kind, document_id=parse.document_id,
                document_version=parse.document_version, expected_parse_artifact_id=parse.id,
                expected_business_revision_id=run.current_business_revision_id, question=question, options=options,
                source_refs=source_refs, status=InputRequestStatus.ACTIVE))
        run.status = RunStatus.AWAITING_INPUT
        run.current_stage = "等待范围确认"
        append_event(db, run, "run_status", question)

    def _source_refs(self, db, source_ids, translation_ids=()):
        refs=[]
        for value in source_ids:
            source=db.get(em.SourceSegment,UUID(str(value)))
            if source is None:
                continue
            parse=db.get(em.ParseArtifact,source.parse_artifact_id)
            anchor=db.scalar(select(em.SourceAnchor).where(em.SourceAnchor.source_segment_id==source.id))
            target={"case_id":str(self.case_id),"run_id":str(self.run_id),"document_id":str(parse.document_id),"document_version":parse.document_version,
                "parse_artifact_id":str(parse.id),"logical_document_id":str(source.logical_document_id),"section_id":str(source.section_id),"source_segment_id":str(source.id)}
            if anchor:
                target["source_anchor_id"]=str(anchor.id)
            refs.append({**target,"label":"原件来源","value":source.text,"target":target})
        for value in translation_ids:
            translation=db.get(rm.TranslationSegment,UUID(str(value)))
            if translation is not None:
                target={"case_id":str(self.case_id),"run_id":str(self.run_id),"document_id":str(translation.document_id),"document_version":translation.document_version,
                    "parse_artifact_id":str(translation.parse_artifact_id),"section_id":str(translation.section_id),"translation_segment_id":str(translation.id),"translation_revision_id":str(self.translation_id)}
                refs.append({**target,"label":"目标译文","value":translation.translated_text,"target":target})
        return refs

    def validate_documents(self, state):
        from app.workflows.worker import digest
        from app.documents.type_catalog import document_type_choices
        with self.transaction() as db:
            run = self.locked(db)
            self.business_id = run.current_business_revision_id
            parses = list(db.scalars(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(self.parse_ids))))
            self.scope_ids = []
            for parse in parses:
                sources = list(db.scalars(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id == parse.id).order_by(em.SourceSegment.ordinal)))
                sections = {x.id: x for x in db.scalars(select(em.DocumentSection).where(em.DocumentSection.parse_artifact_id == parse.id))}
                logicals=list(db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id==parse.id)))
                for logical in logicals:
                    if self.worker.confirmed_type(db,logical)==em.LogicalDocumentType.UNKNOWN.value:
                        section_ids=[str(section.id) for section in sections.values() if section.logical_document_id==logical.id]
                        self._input(db,run,parse,"document_boundary","该原件单据类型或边界无法可靠确认，请确认当前单据用途后继续翻译",document_type_choices(section_ids),
                            [{"section_id":value} for value in section_ids])
                        return {"branch_decision":"awaiting_input"}
                mode = self.spec["translation_scope"]
                refs = self.spec.get("selected_section_refs", [])
                selected = sources
                if mode == "selected_sections":
                    target_refs = [r for r in refs if str(parse.document_id) == r["document_id"]]
                    if any(str(parse.id) != r["parse_artifact_id"] for r in target_refs):
                        self._input(db, run, parse, "document_boundary", "所选区段的解析基线变化，需重新确认同一语义范围", [], [])
                        return {"branch_decision":"awaiting_input"}
                    wanted = {r["section_id"] for r in target_refs}
                    selected = [s for s in sources if str(s.section_id) in wanted]
                if mode == "goods_description":
                    fields = list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField, em.BusinessRevisionField.field_assertion_id == em.FieldAssertion.id).where(
                        em.BusinessRevisionField.business_revision_id == self.business_id, em.FieldAssertion.parse_artifact_id == parse.id,
                        em.FieldAssertion.field_key.in_(["goods_description", "description_of_goods", "commodity_description"]))))
                    candidates = [f for f in fields if f.source_segment_ids]
                    resolved = db.scalar(select(InputRequest).where(InputRequest.run_id == run.id, InputRequest.expected_parse_artifact_id == parse.id,
                        InputRequest.kind == "translation_scope", InputRequest.status == InputRequestStatus.RESOLVED).order_by(InputRequest.resolved_at.desc()).limit(1))
                    wanted = {i for f in candidates for i in f.source_segment_ids}
                    if resolved and resolved.answer:
                        wanted = {i for o in resolved.answer["selected_options"] for i in o.get("source_segment_ids", [])}
                    elif len(candidates) != 1:
                        options = [{"option_id":str(f.id), "label":f.source_text or f.candidate_value, "source_segment_ids":f.source_segment_ids} for f in candidates]
                        self._input(db, run, parse, "translation_scope", "请确认需要翻译的货描片段；零命中时请补充或重识别", options, [])
                        return {"branch_decision":"awaiting_input"}
                    selected = [s for s in sources if str(s.id) in wanted]
                for section in sections.values():
                    if not any(s.section_id == section.id for s in selected):
                        continue
                    decisions = list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.section_id == section.id).order_by(em.SectionAssignmentDecision.created_at.desc())))
                    confirmed_language = next((d.confirmed_language for d in decisions if d.confirmed_language), None)
                    assessment=assess_language("\n".join(s.text for s in selected if s.section_id==section.id))
                    if (section.language_uncertain or assessment.uncertain) and not confirmed_language:
                        self._input(db, run, parse, "source_language", "请确认该区段源语言与原文保留范围", [
                            {"option_id":language,"label":language_name(language),"confirmed_language":language,"section_ids":[str(section.id)]} for language in ("zh-CN","en","fr","es","ru")], [{"section_id":str(section.id)}])
                        return {"branch_decision":"awaiting_input"}
                ids = [str(s.id) for s in selected]
                scope = rm.ResolvedTranslationScope(tenant_id=self.tenant,case_id=self.case_id,run_id=self.run_id,document_id=parse.document_id,
                    document_version=parse.document_version,parse_artifact_id=parse.id,business_revision_id=self.business_id,
                    selector_version=self.spec.get("scope_selector_version") or mode+"@1",source_segment_ids=ids,
                    source_section_ids=list(dict.fromkeys(str(s.section_id) for s in selected)),fingerprint=digest({"parse":str(parse.id),"source_ids":ids,"mode":mode}))
                db.add(scope);db.flush();self.scope_ids.append(scope.id)
            self._rules(db, run, parses)
        self._vision_candidates()
        return {"branch_decision":"continue"}

    def _vision_candidates(self):
        from app.workflows.worker import ModelFailure,digest
        ref=self.prompt_versions.get("document.vision_region")
        binding=next((ModelBinding.model_validate(value) for value in self.bindings if value["role"]==ModelRole.VISION.value),None)
        if ref is None or binding is None:return
        with self.transaction() as db:
            parses=list(db.scalars(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(self.parse_ids),em.ParseArtifact.quality==em.ParseQuality.DEGRADED)))
            candidates=[]
            for parse in parses:
                source_rows=list(db.scalars(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id==parse.id).order_by(em.SourceSegment.ordinal)))
                for source in source_rows:
                    anchor=db.scalar(select(em.SourceAnchor).where(em.SourceAnchor.source_segment_id==source.id,em.SourceAnchor.precision==em.AnchorPrecision.RECT))
                    if anchor is None or not anchor.bbox or not anchor.page_no or len(source.text)>2000:continue
                    if anchor.coord_system not in {"page_normalized_top_left","page_normalized"}:continue
                    version=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==parse.document_id,DocumentVersion.version==parse.document_version))
                    if version is None or version.mime not in {"application/pdf","image/jpeg","image/png"}:continue
                    if version.mime!="application/pdf" and anchor.page_no!=1:continue
                    obj=db.get(StoredObject,version.object_id)
                    candidates.append((source,anchor,obj.object_key,parse.id,version.mime,version.sha256,version.size_bytes))
                    if len(candidates)>=2:break
                if len(candidates)>=2:break
        for source,anchor,object_key,parse_id,source_mime,source_sha256,source_size in candidates:
            def reread(step_id):
                raw=self.store.read_all(object_key)
                import hashlib
                from app.workflows.worker import ModelFailure
                if len(raw)!=source_size or hashlib.sha256(raw).hexdigest()!=source_sha256:raise ModelFailure("source_hash_mismatch","视觉原件与封存字节不一致")
                self.ensure_time(model_call_timeout(self.settings, binding))
                self.submit_intent(step_id)
                from app.workflows.costs import PricedLedger
                budget=self.worker.model_budget(binding)
                gateway=build_model_gateway(self.settings,ledger=PricedLedger(StepExecutionCallLedger(self.factory,tenant_id=self.tenant,step_execution_id=step_id,
                    lease_token=self.lease,execution_fence=self.fence),binding,budget))
                try:
                    result=self.runner.run(vision_candidate(gateway,self.registry,binding,raw,page_no=anchor.page_no,
                        rect=NormRect(x0=anchor.bbox[0],y0=anchor.bbox[1],x1=anchor.bbox[2],y1=anchor.bbox[3]),ocr_text=source.text,
                        region_context={"parse_artifact_id":str(parse_id),"source_segment_id":str(source.id),"page_no":anchor.page_no,"purpose":"仅二次转写候选，不覆盖原文"},
                        provider_request_key=self.request_key(step_id),budget=budget,prompt_ref=ref,source_mime=source_mime))
                    if isinstance(result,Succeeded):self.worker.record_model_response(step_id,result)
                finally:self.runner.run(gateway.aclose())
                if isinstance(result,NeedsReconcile):raise ModelFailure("acceptance_unknown","局部视觉复读受理状态待核查",unknown=True)
                if isinstance(result,Failed):raise ModelFailure(result.kind,result.message)
                with self.transaction() as db:
                    self.locked(db)
                    candidate=em.FieldAssertion(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=parse_id,logical_document_id=source.logical_document_id,
                        section_id=source.section_id,field_key="vision_transcription_candidate",role="source",group="quality",source_text=source.text,
                        candidate_value=result.value.transcription,normalized_value=None,source_segment_ids=[str(source.id)],state="candidate",
                        ambiguity=("视觉二次转写候选；原文未改变，必须人工复核。可辨认" if result.value.legible else "视觉二次转写不可可靠辨认；仅候选")[:200])
                    db.add(candidate);db.flush()
                    db.add(em.ArtifactDependency(tenant_id=self.tenant,case_id=self.case_id,artifact_kind="vision_candidate",artifact_id=candidate.id,
                        depends_on={"source_segment_ids":[str(source.id)],"source_anchor_id":str(anchor.id),"parse_artifact_id":str(parse_id),"prompt":ref,"binding":binding.public_config_hash},
                        fingerprint=digest({"source":str(source.id),"anchor":str(anchor.id),"prompt":ref,"binding":binding.public_config_hash})))
                    self.complete_step(db,step_id,[candidate.id]);return [candidate.id]
            candidate_ids=self.step("视觉复读:"+str(source.id),{"source":str(source.id),"anchor":str(anchor.id)},reread)
            with self.transaction() as db:
                run=self.locked(db)
                for candidate_id in candidate_ids:
                    candidate=db.get(em.FieldAssertion,UUID(str(candidate_id)))
                    self._publish_vision_candidate(db,run,candidate)

    def _publish_vision_candidate(self,db,run,candidate):
        from app.workflows.worker import digest
        if run.id!=self.run_id or candidate is None or candidate.tenant_id!=self.tenant or candidate.case_id!=self.case_id or candidate.parse_artifact_id not in self.parse_ids or candidate.field_key!="vision_transcription_candidate" or candidate.is_human:
            raise ValueError("视觉候选不属于本次冻结解析")
        for source_id in candidate.source_segment_ids:
            source=db.get(em.SourceSegment,UUID(str(source_id)))
            if source is None or source.case_id!=self.case_id or source.parse_artifact_id!=candidate.parse_artifact_id or source.logical_document_id!=candidate.logical_document_id:
                raise ValueError("视觉候选来源与冻结解析不一致")
        refs=self._source_refs(db,candidate.source_segment_ids)
        if not refs:raise ValueError("视觉候选没有可核验来源")
        fingerprint=digest({"rule":"material.vision_candidate@1","candidate_id":str(candidate.id),"business_revision_id":str(self.business_id)})
        check=db.scalar(select(rm.RuleCheck).where(rm.RuleCheck.run_id==run.id,rm.RuleCheck.business_revision_id==self.business_id,rm.RuleCheck.input_fingerprint==fingerprint))
        if check is not None:return
        description="视觉复读结果仅为未核验质量候选，不能作为单证事实；请对照原区域查看。接受提示只记录已查看，不采用候选值；事实纠正仍须修订对应业务字段并填写依据。"
        check=rm.RuleCheck(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,
            rule_id="material.vision_candidate",rule_pack_version=self.spec["rule_pack_version"],status="undetermined",explanation=description,
            input_field_ids=[str(candidate.id)],source_segment_ids=candidate.source_segment_ids,
            calculation={"candidate_only":True,"candidate_id":str(candidate.id),"adopted":False},input_fingerprint=fingerprint)
        db.add(check);db.flush()
        original={**refs[0],"label":"原解析转写（请对照原件）","value":candidate.source_text or refs[0]["value"]}
        visual={**refs[0],"label":"视觉复读候选（未经核验）","value":candidate.candidate_value or "不可可靠辨认"}
        db.add(rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,
            rule_check_id=check.id,category="quality",severity="yellow",blocks_export=False,title="视觉复读候选待核验",
            description=description,sources=[original,visual]))
        db.get(Case,self.case_id).review_state_version+=1

    def _rules(self, db, run, parses):
        from app.workflows.worker import digest
        logicals = {l.id:l for l in db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id.in_(self.parse_ids)))}
        logical_types={value.id:self.confirmed_type(db,value) for value in logicals.values()}
        from app.documents.type_catalog import base_type
        core_types={key:base_type(value) for key,value in logical_types.items()}
        all_sources = list(db.scalars(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id.in_(self.parse_ids))))
        anchors = list(db.scalars(select(em.SourceAnchor).where(em.SourceAnchor.parse_artifact_id.in_(self.parse_ids))))
        documents = [{"id":str(p.id),"page_count":p.page_count,"seen_pages":[a.page_no for a in anchors if a.parse_artifact_id==p.id and a.page_no],
            "segment_count":sum(s.parse_artifact_id==p.id for s in all_sources),"source_segment_ids":[str(s.id) for s in all_sources if s.parse_artifact_id==p.id],
            "quality_flags":p.quality_flags,"is_synthetic":p.is_synthetic,
            "document_type":next((core_types[l.id] for l in logicals.values() if l.parse_artifact_id==p.id),"unknown"),
            "logical_documents":[{"logical_document_id":str(l.id),"document_type":core_types[l.id],"document_profile":logical_types[l.id],"source_segment_ids":[str(s.id) for s in all_sources if s.logical_document_id==l.id]} for l in logicals.values() if l.parse_artifact_id==p.id]} for p in parses]
        fields = list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(em.BusinessRevisionField.business_revision_id==self.business_id)))
        field_rows = [{"id":str(f.id),"logical_document_id":str(f.logical_document_id),"field_key":f.field_key,"role":f.role,"value":f.normalized_value if f.normalized_value is not None else f.candidate_value,"numeric_value":f.numeric_value,"source_text":f.source_text,"unit":f.unit,"source_segment_ids":f.source_segment_ids} for f in fields]
        lines = list(db.scalars(select(em.LineItem).where(em.LineItem.parse_artifact_id.in_(self.parse_ids))))
        line_rows = [{**{k:getattr(l,k) for k in ("quantity","quantity_unit","package_count","package_unit","unit_price","amount","currency","net_weight","gross_weight","weight_unit","source_segment_ids","description","hs_code")},"id":str(l.id),"logical_document_id":str(l.logical_document_id),"document_type":core_types[l.logical_document_id]} for l in lines]
        line_ids=[line.id for line in lines]
        matches=list(db.scalars(select(em.LineMatchEdge).where(em.LineMatchEdge.case_id==self.case_id,em.LineMatchEdge.left_line_id.in_(line_ids),em.LineMatchEdge.right_line_id.in_(line_ids))))
        match_rows=[{**{key:getattr(edge,key) for key in ('relation','state','basis')},'id':str(edge.id),'left_line_id':str(edge.left_line_id),'right_line_id':str(edge.right_line_id),'decided_by':str(edge.decided_by) if edge.decided_by else None} for edge in matches]
        source_rows=[{"id":str(source.id),"text":source.text,"logical_document_id":str(source.logical_document_id),"section_id":str(source.section_id),
                      "structure":source.structure,"table_row":source.table_row,"table_col":source.table_col,"document_id":str(next(p.document_id for p in parses if p.id==source.parse_artifact_id)),
                      "document_version":next(p.document_version for p in parses if p.id==source.parse_artifact_id)} for source in all_sources]
        from app.documents.catalog_checks import evaluate_catalog_checks
        from app.consignment.service import comparison_owned_rule
        from app.review.rule_inputs import decision_view
        decisions=[decision_view(decision) for decision in db.scalars(select(rm.RuleInputDecision).where(rm.RuleInputDecision.business_revision_id==self.business_id))]
        for result in [*evaluate_catalog_checks(logical_documents=[{"logical_document_id":str(l.id), "document_type":logical_types[l.id],
            "cargo_profile_ids":self.confirmed_cargo_profiles(db,l)} for l in logicals.values()],fields=field_rows,source_rows=source_rows),
            *evaluate_rules(documents=documents,fields=field_rows,lines=line_rows,rule_pack=self.spec["rule_pack_version"],line_matches=match_rows,
                                     jurisdiction_inputs=self.spec.get("jurisdiction_inputs"),source_rows=source_rows,rule_inputs=decisions,
                                     reviewed_publications=self.spec.get("reviewed_rule_publications"))]:
            if comparison_owned_rule(run,result.rule_id):continue
            prior = db.scalar(select(rm.RuleCheck).where(rm.RuleCheck.run_id==run.id,rm.RuleCheck.business_revision_id==self.business_id,rm.RuleCheck.input_fingerprint==result.fingerprint))
            if prior:
                continue
            check = rm.RuleCheck(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,rule_id=result.rule_id,
                rule_pack_version=self.spec["rule_pack_version"],status=result.status,explanation=result.explanation,input_field_ids=list(result.input_field_ids),
                source_segment_ids=list(result.source_segment_ids),calculation=result.calculation,input_fingerprint=result.fingerprint)
            db.add(check);db.flush()
            if result.status in {"failed","undetermined","not_covered","not_run"}:
                from app.review.issue_skill import classify_rule, SKILL_ID
                category, severity, blocks_export = classify_rule(result)
                db.add(rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,rule_check_id=check.id,
                    category=category,severity=severity,blocks_export=blocks_export,guidance={"skill_version":SKILL_ID,"classification_basis":"deterministic_rule","rule_id":result.rule_id},
                    title=result.rule_id,description=result.explanation,sources=self._source_refs(db,result.source_segment_ids)))

    def translate_segments(self, state):
        if state.get("branch_decision") == "awaiting_input":
            return {}
        from app.workflows.worker import digest, ModelFailure
        translated_ids = []
        synthetic = False
        with self.transaction() as db:
            run = self.locked(db)
            previous = db.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
            existing_segments=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in previous.segment_ids])))) if previous else []
            existing={value:s for s in existing_segments for value in s.source_segment_ids}
            scopes = list(db.scalars(select(rm.ResolvedTranslationScope).where(rm.ResolvedTranslationScope.id.in_(self.scope_ids))))
        source_order=[]
        for scope in scopes:
            with self.transaction() as db:
                sources=list(db.scalars(select(em.SourceSegment).join(em.LogicalDocument,em.LogicalDocument.id==em.SourceSegment.logical_document_id)
                    .join(em.DocumentSection,em.DocumentSection.id==em.SourceSegment.section_id).where(em.SourceSegment.id.in_([UUID(i) for i in scope.source_segment_ids]))
                    .order_by(em.LogicalDocument.ordinal,em.DocumentSection.ordinal,em.SourceSegment.ordinal)))
                sections={x.id:x for x in db.scalars(select(em.DocumentSection).where(em.DocumentSection.parse_artifact_id==scope.parse_artifact_id))}
            pending=[]
            for source in sources:
                source_id=str(source.id);source_order.append(source_id)
                fingerprint=digest({"source":source_id,"text":source.text,"parse":str(scope.parse_artifact_id),"target":self.spec["target_language"],"glossary":self.spec.get("glossary_version")})
                old=existing.get(source_id)
                if old and set(old.source_segment_ids)<=set(scope.source_segment_ids) and old.parse_artifact_id==scope.parse_artifact_id and old.target_language==self.spec["target_language"] and (old.created_by is not None or old.dependency_fingerprint==fingerprint or self.purpose in ("recompute","resume_input") and previous.business_revision_id==self.business_id):
                    if str(old.id) not in translated_ids:translated_ids.append(str(old.id))
                    continue
                section=sections[source.section_id]
                with self.transaction() as db:
                    decision=db.scalar(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.section_id==section.id,
                        em.SectionAssignmentDecision.confirmed_language.is_not(None)).order_by(em.SectionAssignmentDecision.created_at.desc()).limit(1))
                language=decision.confirmed_language if decision else section.detected_language
                retained=language in {self.spec["target_language"],"already_target"} and (decision or not section.language_uncertain)
                retained=bool(retained and not translation_language_defects(source.text,source.text,self.spec["target_language"]))
                pending.append({"source":source,"fingerprint":fingerprint,"retained":retained})
            for batch in bounded_batches(pending,lambda row: row["source"].text,lambda row: str(row["source"].logical_document_id)):
                batch_hash=digest([row["fingerprint"] for row in batch])
                def translate(step_id):
                    nonlocal synthetic
                    material=[];token_map={};source_tokens={};source_map={str(r["source"].id):r for r in batch}
                    for row in batch:
                        if row["retained"]:continue
                        protected,tokens=protect_text(row["source"].text)
                        renamed={key:f"P{len(token_map)+index+1}" for index,key in enumerate(tokens)}
                        protected=_HOLDER.sub(lambda m:'⟦'+renamed[m.group(1)]+'⟧',protected)
                        token_map.update({renamed[k]:v for k,v in tokens.items()})
                        source_tokens[str(row["source"].id)]={renamed[k]:v for k,v in tokens.items()}
                        material.append({"id":str(row["source"].id),"text":protected})
                    outputs=[]
                    if material:
                        result=self._call(step_id,"document.translate",{"target_language":self.spec["target_language"],"segments":material,"glossary":{}},TranslationResult,ModelRole.TRANSLATE)
                        synthetic=synthetic or result.is_fake
                        expected={row["id"] for row in material};coverage=set()
                        if result.value.target_language!=self.spec["target_language"]:raise ModelFailure("invalid_source_mapping","目标语种不一致")
                        for target in result.value.segments:
                            ids=target.source_segment_ids
                            if not ids or len(ids)!=len(set(ids)) or not set(ids)<=expected:raise ModelFailure("invalid_source_mapping","译文来源集合不属于输入批次")
                            if self.prompt_versions["document.translate"]["version"] >= 3 and len(ids)!=1:raise ModelFailure("invalid_source_mapping","译段跨越源片段或单元格边界，未发布")
                            if len({source_map[i]["source"].section_id for i in ids})!=1:raise ModelFailure("invalid_source_mapping","合译不得跨区段")
                            coverage.update(ids)
                            found_keys=_HOLDER.findall(target.translated_text)
                            actual_tokens={key:token_map[key] for key in found_keys if key in token_map}
                            text,defects=restore_translation(target.translated_text,actual_tokens)
                            if any(key not in token_map for key in found_keys):defects.append("unknown_protected_token")
                            if set(target.protected_token_ids)!=set(actual_tokens):defects.append("declared_tokens")
                            required_tokens={key:value for sid in ids for key,value in source_tokens[sid].items()}
                            expected_values=Counter(required_tokens.values())
                            if any(text.count(value)<count for value,count in expected_values.items()):defects.append("missing_protected_value")
                            defects.extend(translation_language_defects("\n".join(source_map[i]["source"].text for i in ids),text,self.spec["target_language"]))
                            from app.conversation.safety import contains_internal_information
                            if contains_internal_information(text):raise ModelFailure("sensitive_output","译文包含项目内部信息，未发布")
                            outputs.append((ids,text,required_tokens,"warning" if defects or result.is_fake else "passed"))
                        if coverage!=expected:raise ModelFailure("invalid_source_mapping","模型遗漏输入源段，未产生完整批次")
                    for row in batch:
                        if row["retained"]:outputs.append(([str(row["source"].id)],row["source"].text,{},"retained_original"))
                    with self.transaction() as db:
                        self.locked(db);ids=[]
                        for source_ids,text,tokens,state_value in outputs:
                            source=source_map[source_ids[0]]["source"]
                            segment=rm.TranslationSegment(tenant_id=self.tenant,case_id=self.case_id,run_id=self.run_id,document_id=scope.document_id,
                                document_version=scope.document_version,parse_artifact_id=scope.parse_artifact_id,section_id=source.section_id,target_language=self.spec["target_language"],
                                ordinal=source.ordinal,source_segment_ids=source_ids,translated_text=text,protected_tokens=tokens,state=state_value,
                                dependency_fingerprint=source_map[source_ids[0]]["fingerprint"] if len(source_ids)==1 else digest([source_map[i]["fingerprint"] for i in source_ids]))
                            db.add(segment);db.flush();ids.append(segment.id)
                            for source_id in source_ids:db.add(rm.SourceTranslationLink(tenant_id=self.tenant,source_segment_id=UUID(source_id),translation_segment_id=segment.id))
                            db.add(em.ArtifactDependency(tenant_id=self.tenant,case_id=self.case_id,artifact_kind="translation_segment",artifact_id=segment.id,
                                depends_on={"source_segment_ids":source_ids,"parse_artifact_id":str(scope.parse_artifact_id),"source_text_hash":digest([source_map[i]["source"].text for i in source_ids]),
                                    "prompt":self.prompt_versions["document.translate"],"model_bindings":self.bindings,"glossary":self.spec.get("glossary_version")},fingerprint=segment.dependency_fingerprint))
                        self.complete_step(db, step_id, ids)
                        return ids
                translated_ids.extend(self.step("翻译批次:"+batch_hash[:20],{"fingerprint":batch_hash},translate))
        with self.transaction() as db:
            translated=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in translated_ids]))))
        order={value:index for index,value in enumerate(source_order)}
        translated.sort(key=lambda value:min(order[i] for i in value.source_segment_ids))
        translated_ids=[str(value.id) for value in translated]
        with self.transaction() as db:
            run=self.locked(db)
            current=db.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
            if current and current.business_revision_id==self.business_id and current.target_language==self.spec["target_language"] and current.segment_ids==translated_ids and set(current.parse_artifact_ids)=={str(i) for i in self.parse_ids}:
                previous_scopes=list(db.scalars(select(rm.ResolvedTranslationScope).where(rm.ResolvedTranslationScope.id.in_([UUID(i) for i in current.resolved_scope_ids]))))
                if {scope.fingerprint for scope in previous_scopes}=={scope.fingerprint for scope in scopes}:
                    self.translation_id=current.id;self.scope_ids=[scope.id for scope in previous_scopes]
                    return {}
            number=(db.scalar(select(func.max(rm.TranslationRevision.revision_no)).where(rm.TranslationRevision.run_id==run.id)) or 0)+1
            revision=rm.TranslationRevision(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,target_language=self.spec["target_language"],
                revision_no=number,parent_revision_id=run.current_translation_revision_id,parse_artifact_ids=[str(i) for i in self.parse_ids],resolved_scope_ids=[str(i) for i in self.scope_ids],
                segment_ids=translated_ids,prompt_versions=self.prompt_versions,model_bindings=self.bindings,glossary_version=self.spec.get("glossary_version"),qa_status="not_run",
                dependency_fingerprint=digest(translated_ids),is_synthetic=synthetic)
            db.add(revision);db.flush();run.current_translation_revision_id=revision.id;self.translation_id=revision.id
        return {}

    def quality_and_preview(self, state):
        if state.get("branch_decision") == "awaiting_input":
            return {}
        from app.workflows.worker import digest, ModelFailure
        preview_ids=[]
        with self.transaction() as db:
            run=self.locked(db);revision=db.get(rm.TranslationRevision,self.translation_id)
            revision=self._comparison_review_revision(db,run,revision)
            if revision is None or revision.business_revision_id!=self.business_id:
                raise ModelFailure("revision_stale","译文与当前业务基线不一致，未发布复核稿")
            segments=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]))))
            scopes=list(db.scalars(select(rm.ResolvedTranslationScope).where(rm.ResolvedTranslationScope.id.in_(self.scope_ids))))
            if any(scope.business_revision_id!=self.business_id for scope in scopes):
                raise ModelFailure("revision_stale","译文范围与当前业务基线不一致，未发布复核稿")
            if (getattr(self,"repair_request",{}) or {}).get("comparison_batch_id"):
                parses=list(db.scalars(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(self.parse_ids))))
                self._rules(db,run,parses)
            expected={i for s in scopes for i in s.source_segment_ids}; actual={i for s in segments for i in s.source_segment_ids}
            invalid=[];language_defects={};invalid_details={}
            synthetic = revision.is_synthetic or any("【假模型输出" in s.translated_text for s in segments)
            revision.is_synthetic=synthetic
            for segment in segments:
                source_text="\n".join(db.get(em.SourceSegment,UUID(i)).text for i in segment.source_segment_ids)
                protected_values=list(segment.protected_tokens.values())
                missing_value=any(segment.translated_text.count(v)<protected_values.count(v) for v in protected_values)
                source_numbers=Counter(re.findall(r"\d+(?:[.,:/-]\d+)*",source_text))
                translated_numbers=Counter(re.findall(r"\d+(?:[.,:/-]\d+)*",segment.translated_text))
                defects=translation_language_defects(source_text,segment.translated_text,revision.target_language)
                if defects:language_defects[str(segment.id)]=defects
                reasons=[]
                if not segment.translated_text.strip():reasons.append("对应译段为空")
                if missing_value or any(translated_numbers[k]<v for k,v in source_numbers.items()):reasons.append("原文编号、数字或受保护值未完整保留")
                if defects:reasons.append("目标语言正文不足或疑似保留了应翻译的原文")
                if _HOLDER.search(segment.translated_text):reasons.append("译文仍含未恢复的保护占位符")
                if reasons:
                    invalid.append(segment)
                    invalid_details[str(segment.id)]=reasons
            revision.qa_status="warning" if expected!=actual or invalid or revision.is_synthetic else "passed"
            revision.qa_summary={"expected_source_ids":sorted(expected),"actual_source_ids":sorted(actual),"invalid_segment_ids":[str(s.id) for s in invalid],"target_language_defects":language_defects,"deterministic_qa_version":"translation-deterministic@2","semantic_status":"not_run"}
            adopted_conflicts=[]
            human_fields=list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(
                em.BusinessRevisionField.business_revision_id==self.business_id,em.FieldAssertion.is_human.is_(True))))
            for field in human_fields:
                if not field.supersedes_assertion_id:continue
                original=db.get(em.FieldAssertion,field.supersedes_assertion_id)
                if original is None:continue
                targets=[segment for segment in segments if set(segment.source_segment_ids)&set(field.source_segment_ids)]
                reason=adopted_value_translation_reason(original.normalized_value or original.candidate_value or original.source_text or "",field.normalized_value or field.candidate_value or "",
                    "\n".join(segment.translated_text for segment in targets),numeric_value=field.numeric_value)
                if reason is None:continue
                adopted_conflicts.append({"field_assertion_id":str(field.id),"reason":reason})
                prior=db.scalar(select(rm.Issue).where(rm.Issue.run_id==run.id,rm.Issue.translation_revision_id==revision.id,rm.Issue.business_revision_id==self.business_id,
                    rm.Issue.category=="adopted_value_translation").limit(1))
                if prior:continue
                parse=db.get(em.ParseArtifact,field.parse_artifact_id)
                target={"case_id":str(self.case_id),"run_id":str(self.run_id),"document_id":str(parse.document_id),"document_version":parse.document_version,"parse_artifact_id":str(parse.id),
                    "business_revision_id":str(self.business_id),"translation_revision_id":str(revision.id),"field_id":str(field.id)}
                db.add(rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,translation_revision_id=revision.id,
                    category="adopted_value_translation",severity="red",blocks_export=True,title="人工采用值与源译关系待裁定",
                    description="人工采用的业务值与原文不同，当前译文未证明已表达采用值或仍需文字语义核验；原文及原译保留。请依据原件裁定是否保持原译或作明确修订，报告不得声称译文已自动改为采用值。",
                    sources=[{"label":"人工采用字段","value":field.normalized_value or field.candidate_value or "","target":target},
                        {"label":"原字段候选","value":original.normalized_value or original.candidate_value or original.source_text or "","target":{**target,"field_id":str(original.id)}},
                        *self._source_refs(db,field.source_segment_ids,[segment.id for segment in targets])]))
            revision.qa_summary={**revision.qa_summary,"adopted_value_conflicts":adopted_conflicts}
            from app.review.issue_skill import source_targets
            prior_quality=list(db.scalars(select(rm.Issue).where(rm.Issue.run_id==run.id,
                rm.Issue.translation_revision_id==revision.id,rm.Issue.category=="translation_quality")))
            for segment in invalid:
                description = "；".join(invalid_details[str(segment.id)])+"。可手动修订对应译文或提交Agent处理意见，再核验本段。"
                existing_quality = [issue for issue in prior_quality if any(target.get("translation_segment_id") == str(segment.id)
                    for target in source_targets(issue))]
                if existing_quality:
                    if len(existing_quality) == 1 and existing_quality[0].description == description:
                        self._carry_quality_disposition(db, run, revision, segment, existing_quality[0])
                    continue
                issue = rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,translation_revision_id=revision.id,
                    category="translation_quality",severity="red",blocks_export=True,title="对应译段需修订",
                    description=description,
                    sources=self._source_refs(db,segment.source_segment_ids,[segment.id]))
                db.add(issue); db.flush()
                self._carry_quality_disposition(db, run, revision, segment, issue)
            if (expected!=actual or revision.is_synthetic) and not prior_quality:
                db.add(rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,translation_revision_id=revision.id,
                    category="translation_quality",severity="red",blocks_export=True,title="译文范围不完整",
                    description="已确认范围存在未生成译段或测试模型输出，请重新处理；不能以修改其他译段代替补全。",
                    sources=self._source_refs(db,sorted(expected-actual))))
            # 必需 HTML 不依赖可选草稿 PDF；每份物理原件有明确映射。
            for scope in scopes:
                existing=db.scalar(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,
                    rm.PreviewArtifact.document_id==scope.document_id,rm.PreviewArtifact.parse_artifact_id==scope.parse_artifact_id,
                    rm.PreviewArtifact.business_revision_id==self.business_id,rm.PreviewArtifact.translation_revision_id==revision.id,
                    rm.PreviewArtifact.resolved_scope_id==scope.id,rm.PreviewArtifact.status=="valid")
                    .order_by(rm.PreviewArtifact.created_at.desc()).limit(1))
                if existing:
                    preview_ids.append(existing.id);continue
                source_rows=[{"id":str(s.id),"text":s.text} for s in db.scalars(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id==scope.parse_artifact_id).order_by(em.SourceSegment.ordinal))]
                rows=[{"id":str(s.id),"text":s.translated_text,"source_segment_ids":s.source_segment_ids} for s in segments if s.document_id==scope.document_id]
                number=(db.scalar(select(func.max(rm.PreviewArtifact.preview_version)).where(rm.PreviewArtifact.run_id==run.id,rm.PreviewArtifact.document_id==scope.document_id)) or 0)+1
                preview=rm.PreviewArtifact(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,document_id=scope.document_id,document_version=scope.document_version,
                    parse_artifact_id=scope.parse_artifact_id,business_revision_id=self.business_id,translation_revision_id=revision.id,resolved_scope_id=scope.id,target_language=self.spec["target_language"],
                    preview_version=number,render_config_version="source-layout@1",status="valid",reading_html=self._layout_html(db,scope,segments),
                    segment_mappings=[{"source_segment_ids":r["source_segment_ids"],"translation_segment_id":r["id"]} for r in rows],draft_pdf_disabled_reason="草稿PDF正在生成")
                db.add(preview);db.flush();preview_ids.append(preview.id)
        self._draft_previews(preview_ids)
        if getattr(self,"repair_request",{}).get("layout_only"):
            return {}
        self._semantic_quality()
        return {}

    def _carry_quality_disposition(self, db, run, revision, segment, issue):
        """同一不可变译段和确定性结论沿用父版处置；当前人工决定优先。"""
        from app.review.issue_skill import source_targets
        if db.scalar(select(rm.IssueDisposition.id).where(rm.IssueDisposition.issue_id == issue.id).limit(1)):
            return
        parent = db.get(rm.TranslationRevision, revision.parent_revision_id) if revision.parent_revision_id else None
        if (issue.category != "translation_quality" or not revision.qa_summary.get("deterministic_qa_version")
                or parent is None or parent.run_id != run.id or parent.business_revision_id != revision.business_revision_id
                or parent.target_language != revision.target_language or str(segment.id) not in parent.segment_ids
                or parent.qa_summary.get("deterministic_qa_version") != revision.qa_summary.get("deterministic_qa_version")
                or parent.qa_summary.get("target_language_defects", {}).get(str(segment.id)) != revision.qa_summary.get("target_language_defects", {}).get(str(segment.id))):
            return
        def evidence_key(candidate):
            keys = ("document_id", "document_version", "parse_artifact_id", "logical_document_id", "section_id",
                "source_anchor_id", "source_segment_id", "translation_segment_id")
            return sorted((tuple(str(target.get(key)) for key in keys) for target in source_targets(candidate)))
        candidates = list(db.scalars(select(rm.Issue).where(rm.Issue.run_id == run.id,
            rm.Issue.translation_revision_id == parent.id, rm.Issue.business_revision_id == revision.business_revision_id,
            rm.Issue.category == issue.category, rm.Issue.severity == issue.severity, rm.Issue.blocks_export == issue.blocks_export,
            rm.Issue.title == issue.title, rm.Issue.description == issue.description)))
        candidates = [old for old in candidates if old.rule_check_id == issue.rule_check_id and evidence_key(old) == evidence_key(issue)
            and {target.get("translation_segment_id") for target in source_targets(old) if target.get("translation_segment_id")} == {str(segment.id)}]
        if len(candidates) != 1:
            return
        previous = candidates[0]
        decision = db.scalar(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id == previous.id)
            .order_by(rm.IssueDisposition.review_sequence.desc()).limit(1))
        if decision is None:
            return
        case = db.get(Case, self.case_id); case.review_state_version += 1
        issue.supersedes_issue_id = previous.id
        db.add(rm.IssueDisposition(tenant_id=self.tenant, case_id=self.case_id, issue_id=issue.id,
            business_revision_id=revision.business_revision_id, action=decision.action,
            reason=f"沿用未变化译段的上一版处置（v{parent.revision_no}→v{revision.revision_no}）：{decision.reason}",
            basis=decision.basis, review_sequence=case.review_state_version, decided_by=decision.decided_by, created_at=decision.created_at))
        from app.workflows.worker import append_event
        append_event(db, run, "run_status", "已沿用未变化译段、相同质量结论的上一版人工处置", {"issue_id": str(issue.id)})

    def _layout_html(self, db, scope, segments):
        from app.review.layout import document_layout, render_html
        from app.documents.models import DocumentVersion, StoredObject
        parse=db.get(em.ParseArtifact,scope.parse_artifact_id)
        version=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==scope.document_id,DocumentVersion.version==scope.document_version))
        raw=ocr_raw=None
        if not parse.layout_manifest:
            raw=self.store.read_all(db.get(StoredObject,version.object_id).object_key)
            ocr=db.get(StoredObject,parse.raw_response_object_id) if parse.raw_response_object_id else None
            if ocr and ocr.is_visible and ocr.mime=="application/zip":ocr_raw=self.store.read_all(ocr.object_key)
        layout,sources=document_layout(db,parse,raw,version.mime,ocr_raw)
        return render_html(layout,{str(s.id):s.text for s in sources},[s for s in segments if s.document_id==scope.document_id])

    def _draft_previews(self,preview_ids):
        """可选草稿在短事务外渲染，失败不影响必要 HTML，永不登记成功快照。"""
        import hashlib
        from app.exports import render
        from app.documents.models import DocumentVersion,StoredObject,StoredObjectCategory
        from app.workflows.worker import StaleExecution,YieldExecution
        for preview_id in preview_ids:
            try:
                with self.transaction() as db:
                    run=self.locked(db);preview=db.get(rm.PreviewArtifact,preview_id)
                    if preview is None or preview.status!="valid" or preview.translation_revision_id!=run.current_translation_revision_id or preview.business_revision_id!=run.current_business_revision_id:continue
                    existing=db.get(StoredObject,preview.draft_pdf_object_id) if preview.draft_pdf_object_id else None
                    if existing and existing.is_visible:
                        head=self.store.head(existing.object_key)
                        if head and head.size_bytes==existing.size_bytes:continue
                    revision=db.get(rm.TranslationRevision,preview.translation_revision_id)
                    source=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==preview.document_id,DocumentVersion.version==preview.document_version))
                    segments=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]),rm.TranslationSegment.document_id==preview.document_id).order_by(rm.TranslationSegment.ordinal,rm.TranslationSegment.id)))
                    translated=render.translations_by_source(segments)
                    originals=list(db.scalars(select(em.SourceSegment).join(em.DocumentSection,em.DocumentSection.id==em.SourceSegment.section_id)
                        .join(em.LogicalDocument,em.LogicalDocument.id==em.SourceSegment.logical_document_id)
                        .where(em.SourceSegment.parse_artifact_id==preview.parse_artifact_id)
                        .order_by(em.LogicalDocument.ordinal,em.DocumentSection.ordinal,em.SourceSegment.ordinal,em.SourceSegment.id)))
                    rows=[{"section":db.get(em.DocumentSection,s.section_id).title or "单据内容","source":s.text,"translation":translated.get(str(s.id))} for s in originals]
                    language=preview.target_language
                    from app.review.layout import document_layout
                    parse=db.get(em.ParseArtifact,preview.parse_artifact_id)
                    raw=self.store.read_all(db.get(StoredObject,source.object_id).object_key) if not parse.layout_manifest else None
                    ocr=db.get(StoredObject,parse.raw_response_object_id) if parse.raw_response_object_id else None
                    ocr_raw=self.store.read_all(ocr.object_key) if ocr and ocr.is_visible and ocr.mime=="application/zip" and not parse.layout_manifest else None
                    layout,originals=document_layout(db,parse,raw,source.mime,ocr_raw)
                    source_text={str(s.id):s.text for s in originals}
                self.ensure_time(150)
                docx,expected=render.build_layout_translation(layout,source_text,segments,target_language=language,draft=True)
                data=render.to_pdf(docx);render.verify(data,"pdf",["草稿",*expected],allow_draft=True)
                key=self.store.build_key(self.tenant,self.case_id,StoredObjectCategory.TEMP)
                self.store._internal.put_object(Bucket=self.store._bucket,Key=key,Body=data,ContentType="application/pdf")
                with self.transaction() as db:
                    run=self.locked(db);preview=db.get(rm.PreviewArtifact,preview_id)
                    if preview.status!="valid" or preview.translation_revision_id!=run.current_translation_revision_id or preview.business_revision_id!=run.current_business_revision_id:continue
                    obj=StoredObject(tenant_id=self.tenant,case_id=self.case_id,object_key=key,category=StoredObjectCategory.TEMP,
                        mime="application/pdf",size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),is_visible=True)
                    db.add(obj);db.flush();preview.draft_pdf_object_id=obj.id;preview.draft_pdf_disabled_reason=None
                    preview.render_config_version="source-layout@1+"+render.DRAFT_TEMPLATE_VERSION
            except (StaleExecution,YieldExecution):raise
            except Exception as exc:
                with self.transaction() as db:
                    self.locked(db);preview=db.get(rm.PreviewArtifact,preview_id)
                    if preview and preview.status=="valid":preview.draft_pdf_disabled_reason=f"草稿PDF生成失败（{type(exc).__name__}），请使用双语阅读视图"

    def _semantic_quality(self):
        from app.workflows.worker import digest, ModelFailure
        if not self.registry.check.capability_ready("semantic_qa"):
            return
        with self.transaction() as db:
            revision=db.get(rm.TranslationRevision,self.translation_id)
            targets=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]))))
        if getattr(self,"purpose",None) in {"quality_preview","issue_repair"}:
            # Only changed segments need a new model assessment. Preserve all
            # unchanged candidates and their human decisions in the new version.
            with self.transaction() as db:
                run=self.locked(db);revision=db.get(rm.TranslationRevision,self.translation_id)
                parent=db.get(rm.TranslationRevision,revision.parent_revision_id) if revision.parent_revision_id else None
                if parent and parent.business_revision_id==revision.business_revision_id and parent.qa_summary.get("semantic_status") in {"passed","evaluated_candidates_only"}:
                    unchanged=set(parent.segment_ids)&set(revision.segment_ids)
                    from app.review.issue_skill import TRANSLATION_CATEGORIES
                    replaced = {str(segment.supersedes_segment_id): str(segment.id) for segment in db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]))) if segment.supersedes_segment_id}
                    for previous in db.scalars(select(rm.Issue).where(rm.Issue.translation_revision_id==parent.id,rm.Issue.business_revision_id==self.business_id,rm.Issue.rule_check_id.is_(None))):
                        refs=[source.get("target",source).get("translation_segment_id") for source in previous.sources]
                        if previous.category=="translation_quality" or not refs:continue
                        if previous.category in TRANSLATION_CATEGORIES and not all(ref is None or ref in unchanged for ref in refs):continue
                        exists=db.scalar(select(rm.Issue.id).where(rm.Issue.translation_revision_id==revision.id,rm.Issue.supersedes_issue_id==previous.id))
                        if exists:continue
                        sources=[]
                        for source in previous.sources:
                            updated={**source}
                            if "target" in source:
                                updated["target"]={**source["target"],"translation_revision_id":str(revision.id)}
                                if source["target"].get("translation_segment_id") in replaced:
                                    updated["target"]["translation_segment_id"] = replaced[source["target"]["translation_segment_id"]]
                            else:
                                updated["translation_revision_id"]=str(revision.id)
                                if source.get("translation_segment_id") in replaced:
                                    updated["translation_segment_id"] = replaced[source["translation_segment_id"]]
                            sources.append(updated)
                        copy=rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=run.id,business_revision_id=self.business_id,translation_revision_id=revision.id,category=previous.category,severity=previous.severity,blocks_export=previous.blocks_export,title=previous.title,description=previous.description,guidance=previous.guidance,sources=sources,supersedes_issue_id=previous.id)
                        db.add(copy);db.flush()
                        decision=db.scalar(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==previous.id).order_by(rm.IssueDisposition.review_sequence.desc()).limit(1))
                        if decision:
                            case=db.get(Case,self.case_id);case.review_state_version+=1
                            db.add(rm.IssueDisposition(tenant_id=self.tenant,case_id=self.case_id,issue_id=copy.id,business_revision_id=self.business_id,action=decision.action,reason=decision.reason,basis=decision.basis,review_sequence=case.review_state_version,decided_by=decision.decided_by))
                    targets=[target for target in targets if str(target.id) not in unchanged]
        pairs=[]
        for target in targets:
            with self.transaction() as db:
                sources=list(db.scalars(select(em.SourceSegment).where(em.SourceSegment.id.in_([UUID(i) for i in target.source_segment_ids]))))
                logical=db.get(em.LogicalDocument,sources[0].logical_document_id) if sources else None
                profile=self.confirmed_type(db,logical) if logical else "unknown"
                cargos=self.confirmed_cargo_profiles(db,logical) if logical else []
                from app.documents.extraction import field_schema
                focus=field_schema(profile,cargos)["review_focus"]
            pairs.append({"source_segment_ids":target.source_segment_ids,"translation_segment_ids":[str(target.id)],"source_text":"\n".join(s.text for s in sources),"translated_text":target.translated_text,"section_id":str(target.section_id),
                "document_profile":profile,"cargo_profile_ids":cargos,"review_focus":focus})
        for batch in bounded_batches(pairs,lambda row:row["source_text"]+row["translated_text"]+str(row["review_focus"])+str(row["cargo_profile_ids"])+row["document_profile"],lambda row:row["section_id"],max_tokens=10000):
            def check(step_id):
                from app.review.issue_skill import skill_instructions
                variables={"target_language":self.spec["target_language"],"pairs":[{k:v for k,v in p.items() if k!="section_id"} for p in batch]}
                if self.prompt_versions["document.semantic_qa"]["version"]>=4:
                    variables["issue_rules"]=skill_instructions()
                result=self._call(step_id,"document.semantic_qa",variables,CategorizedSemanticQaResult if self.prompt_versions["document.semantic_qa"]["version"]>=4 else GuidedSemanticQaResult if self.prompt_versions["document.semantic_qa"]["version"]>=3 else SemanticQaResult,ModelRole.EXTRACT)
                allowed_sources={i for pair in batch for i in pair["source_segment_ids"]}
                allowed_targets={i for pair in batch for i in pair["translation_segment_ids"]}
                with self.transaction() as db:
                    self.locked(db)
                    for finding in result.value.findings:
                        if not finding.source_segment_ids or not set(finding.source_segment_ids)<=allowed_sources or not finding.translation_segment_ids or not set(finding.translation_segment_ids)<=allowed_targets:
                            raise ModelFailure("invalid_source_mapping","语义疑点来源不属于本次输入")
                        mapped = {source_id for pair in batch if set(finding.translation_segment_ids).intersection(pair["translation_segment_ids"]) for source_id in pair["source_segment_ids"]}
                        if not set(finding.source_segment_ids).issubset(mapped):
                            raise ModelFailure("invalid_source_mapping", "语义疑点的原文与译段没有对应关系")
                        from app.review.issue_skill import CATEGORIES, SKILL_ID
                        severity = "undetermined" if finding.category in {"source_uncertain", "source_omission", "field_missing"} else "yellow"
                        title = CATEGORIES.get(finding.category, ("单证问题待核对", ""))[0]
                        db.add(rm.Issue(tenant_id=self.tenant,case_id=self.case_id,run_id=self.run_id,business_revision_id=self.business_id,translation_revision_id=self.translation_id,
                            category=finding.category,severity=severity,blocks_export=False,title=title,description=finding.explanation,
                            sources=self._source_refs(db,finding.source_segment_ids,finding.translation_segment_ids),
                            guidance={**{k:getattr(finding,k,None) for k in ("recommendation","impact","replacement_text")}, "skill_version": SKILL_ID, "classification_basis": "model_candidate"}))
                    self.complete_step(db, step_id, [])
                return []
            fingerprint=digest(batch)
            self.step("语义QA批次:"+fingerprint[:20],{"pairs":fingerprint,"business_revision_id":str(self.business_id)},check)
        with self.transaction() as db:
            self.locked(db); revision=db.get(rm.TranslationRevision,self.translation_id)
            revision.qa_summary={**revision.qa_summary,"semantic_status":"evaluated_candidates_only"}

    def publish_review(self, state):
        if state.get("branch_decision") == "awaiting_input":
            return {}
        from app.workflows.worker import append_event, ModelFailure
        with self.transaction() as db:
            run=self.locked(db)
            self.require_settled_steps(db)
            previews=list(db.scalars(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,rm.PreviewArtifact.translation_revision_id==self.translation_id,rm.PreviewArtifact.status=="valid")))
            if {(p.document_id,p.document_version) for p in previews} != set(self.documents):
                raise ModelFailure("preview_incomplete","缺少同版逐源HTML复核稿")
            from app.management.index import refresh_run_summaries
            run.status=RunStatus.AWAITING_REVIEW;run.current_stage="等待人工复核";run.failure_reason=None
            case=db.get(Case,self.case_id);case.review_state_version+=1
            if run.workflow_type == "translate_review" and run.workflow_version >= 2:
                from app.consignment.service import try_rebind_current_for_run
                try_rebind_current_for_run(db, case, run)
            append_event(db,run,"run_status","单证规则与译文草稿已完成，请复核疑点",{"translation_revision_id":str(self.translation_id)})
            refresh_run_summaries(db, run)
        return {}


def build_translation_worker_graph(worker, *, checkpointer=None):
    return build_translate_review_graph(TranslationNodes(worker)).compile(checkpointer=checkpointer)
