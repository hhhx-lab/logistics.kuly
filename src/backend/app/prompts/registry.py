"""
Prompt Registry：读取包内 manifest 与模板，校验后按 `(key, version)` 返回不可变 PromptSpec。

设计取舍：
- 只做严格占位符替换（`{{name}}`），不引入可执行模板引擎：模板里不能有条件、循环或表达式，
  这样「模板 + 变量 → 最终文本」可以被哈希与回放，也不会被资料内容触发任何求值。
- 校验失败不抛到进程级（不 `sys.exit`）：问题逐条记入 `RegistryCheck`，只让依赖该 Prompt 的
  能力拒绝接单，其余能力照常工作；readiness 与 prepare 阶段据此给出禁用原因。
- 已发布版本文件不可原位改字。改动必须新增版本文件并在 manifest 追加条目；
  sha256 校验就是这条纪律的执行点——原位改字会让旧版本变为不可用，而不是悄悄换内容。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from app.config import ModelRole
from app.prompts.schemas import NO_OUTPUT, OUTPUT_SCHEMAS

PROMPTS_ROOT = Path(__file__).resolve().parent

REQUIRED_PROMPT_KEYS: tuple[str, ...] = (
    "chat.system",
    "chat.intent",
    "chat.answer",
    "chat.session_summary",
    "document.extract",
    "document.translate",
    "document.vision_region",
    "document.semantic_qa",
)

# 能力 → 所需 Prompt。任一 Prompt 不可用，该能力整体不接单，不做「缺一个也先跑」的降级。
CAPABILITY_PROMPTS: dict[str, tuple[str, ...]] = {
    "chat": ("chat.system", "chat.intent", "chat.answer", "chat.session_summary"),
    "document_extract": ("document.extract",),
    "document_translate": ("document.translate",),
    "vision_region": ("document.vision_region",),
    "semantic_qa": ("document.semantic_qa",),
    "issue_assessment": ("document.issue_assess",),
}


class TrustLevel(StrEnum):
    """输入信任级别，自上而下递减（Agent 方案第 8 节）。"""

    SYSTEM_POLICY = "system_policy"
    USER_TASK = "user_task"
    VERIFIED_FACT = "verified_fact"
    USER_PREFERENCE = "user_preference"
    SESSION_SUMMARY = "session_summary"
    REFERENCE_MATERIAL = "reference_material"


# 低于「用户确认偏好」的输入在渲染时加显式围栏：模型看得到边界，
# 资料里伪造的收尾标签也会被中和，无法「跳出」资料段冒充指令。
_FENCED_TRUST = {TrustLevel.SESSION_SUMMARY, TrustLevel.REFERENCE_MATERIAL}

VarType = Literal["text", "integer", "json"]
_VAR_TYPES: frozenset[str] = frozenset({"text", "integer", "json"})
PromptStatus = Literal["active", "unverified", "retired"]
_STATUSES: frozenset[str] = frozenset({"active", "unverified", "retired"})

_KEY_RE = re.compile(r"^[a-z]+(\.[a-z_]+)+$")
_VAR_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class PromptUnavailable(RuntimeError):
    """请求的 Prompt 未通过校验或未启用。调用方应把对应能力视为不可受理。"""


class PromptRenderError(ValueError):
    pass


class PromptBudgetExceeded(PromptRenderError):
    pass


@dataclass(frozen=True)
class VariableSpec:
    name: str
    type: VarType
    trust: TrustLevel


@dataclass(frozen=True)
class RenderedPrompt:
    """
    一次渲染的结果。`text` 可能含原件片段，只能在内存中传给网关，
    不得写入日志或账本；账本只记 key/version/sha256 与 `input_fingerprint`。
    """

    key: str
    version: int
    sha256: str
    role: ModelRole
    message_role: Literal["system", "user"]
    output_schema: str
    max_output_tokens: int
    estimated_input_tokens: int
    input_fingerprint: str
    text: str = field(repr=False)
    # 仅供确定性假适配器按输入构造带标记的结果；真实适配器不读取
    variables: Mapping[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.version}"


@dataclass(frozen=True)
class PromptSpec:
    key: str
    version: int
    sha256: str
    role: ModelRole
    message_role: Literal["system", "user"]
    output_schema: str
    allowed_input_trust: frozenset[TrustLevel]
    max_input_tokens: int
    max_output_tokens: int
    status: PromptStatus
    change_note: str
    variables: tuple[VariableSpec, ...]
    template: str = field(repr=False)

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.version}"

    def render(self, **values: Any) -> RenderedPrompt:
        declared = {v.name: v for v in self.variables}
        missing = sorted(set(declared) - set(values))
        extra = sorted(set(values) - set(declared))
        if missing or extra:
            raise PromptRenderError(f"{self.ref} 变量不匹配：缺少 {missing}，多余 {extra}")

        rendered_values = {name: _render_value(spec, values[name], self.ref) for name, spec in declared.items()}
        # 单次替换：变量值里即便含 `{{x}}` 也不会被二次展开
        text = _PLACEHOLDER_RE.sub(lambda m: rendered_values[m.group(1)], self.template)

        estimate = estimate_tokens(text)
        if estimate > self.max_input_tokens:
            raise PromptBudgetExceeded(
                f"{self.ref} 输入估计 {estimate} tokens，超过上限 {self.max_input_tokens}；须缩小片段范围后再调用"
            )
        return RenderedPrompt(
            key=self.key,
            version=self.version,
            sha256=self.sha256,
            role=self.role,
            message_role=self.message_role,
            output_schema=self.output_schema,
            max_output_tokens=self.max_output_tokens,
            estimated_input_tokens=estimate,
            input_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text,
            variables=MappingProxyType(dict(values)),
        )


def estimate_tokens(text: str) -> int:
    """
    保守估计：非 ASCII 字符按 1 token/字、ASCII 按 1 token/3 字符。
    没有供应商分词器时宁可高估，防止真实请求超出上下文；它不是计费依据。
    """
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_count = len(text) - non_ascii
    return non_ascii + math.ceil(ascii_count / 3)


def _render_value(spec: VariableSpec, value: Any, ref: str) -> str:
    if spec.type == "text":
        if not isinstance(value, str):
            raise PromptRenderError(f"{ref} 变量 {spec.name} 应为文本")
        rendered = value
    elif spec.type == "integer":
        # bool 是 int 的子类，必须单独排除
        if not isinstance(value, int) or isinstance(value, bool):
            raise PromptRenderError(f"{ref} 变量 {spec.name} 应为整数")
        rendered = str(value)
    else:
        try:
            # sort_keys 让同一输入得到同一文本，输入指纹才可复现
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PromptRenderError(f"{ref} 变量 {spec.name} 不是可序列化的 JSON 值") from exc

    if spec.trust in _FENCED_TRUST:
        tag = spec.trust.value
        neutralized = re.sub(rf"</\s*{tag}", f"<\\/{tag}", rendered, flags=re.IGNORECASE)
        return f'<{tag} name="{spec.name}">\n{neutralized}\n</{tag}>'
    return rendered


# ---- 校验 ------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryIssue:
    code: str
    detail: str
    key: str | None = None
    version: int | None = None


@dataclass(frozen=True)
class RegistryCheck:
    """启动校验结果。readiness、prepare 与网关调用前都查询它，而不是各自重复校验。"""

    issues: tuple[RegistryIssue, ...]
    valid: frozenset[tuple[str, int]]
    statuses: Mapping[tuple[str, int], str]
    defaults: Mapping[str, int]

    @property
    def ok(self) -> bool:
        return not self.issues

    def is_ready(self, key: str, version: int | None = None) -> bool:
        if version is None:
            version = self.defaults.get(key)
            if version is None:
                return False
            # 默认版本只能是 active；retired 仅供已冻结的 Run 按显式版本取用
            return (key, version) in self.valid and self.statuses.get((key, version)) == "active"
        return (key, version) in self.valid and self.statuses.get((key, version)) in {"active", "retired"}

    def reasons(self, key: str) -> list[str]:
        found = [f"{i.code}: {i.detail}" for i in self.issues if i.key == key]
        version = self.defaults.get(key)
        if version is not None and self.statuses.get((key, version)) == "unverified":
            found.append(f"unverified: {key}@{version} 尚未通过验证，暂不接单")
        if version is None:
            found.append(f"no_default: {key} 没有可用默认版本")
        return found

    def capability_ready(self, capability: str) -> bool:
        keys = CAPABILITY_PROMPTS.get(capability)
        return bool(keys) and all(self.is_ready(k) for k in keys)

    def capability_report(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for capability, keys in CAPABILITY_PROMPTS.items():
            blocked = {k: self.reasons(k) for k in keys if not self.is_ready(k)}
            report[capability] = {"ready": not blocked, "blocked_by": blocked}
        return report


class PromptRegistry:
    def __init__(self, specs: Mapping[tuple[str, int], PromptSpec], check: RegistryCheck) -> None:
        self._specs = dict(specs)
        self.check = check

    @classmethod
    def load(cls, root: Path | None = None) -> PromptRegistry:
        root = root or PROMPTS_ROOT
        specs, issues, statuses, defaults = _load(root)
        valid = frozenset(k for k in specs)
        check = RegistryCheck(
            issues=tuple(issues),
            valid=valid,
            statuses=MappingProxyType(statuses),
            defaults=MappingProxyType(defaults),
        )
        return cls(specs, check)

    def default_version(self, key: str) -> int:
        if not self.check.is_ready(key):
            raise PromptUnavailable(f"{key} 不可用：{'；'.join(self.check.reasons(key)) or '未登记'}")
        return self.check.defaults[key]

    def get(self, key: str, version: int | None = None) -> PromptSpec:
        if version is None:
            version = self.default_version(key)
        if not self.check.is_ready(key, version):
            raise PromptUnavailable(f"{key}@{version} 不可用：{'；'.join(self.check.reasons(key)) or '未登记或未启用'}")
        return self._specs[(key, version)]

    def freeze(self, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        """
        回合/首个 ExecutionEpoch 冻结用的版本清单。后续发布新版不影响已冻结的 Run，
        恢复与重试按这里的 version+sha256 取回同一份模板。
        """
        frozen: dict[str, dict[str, Any]] = {}
        for key in keys:
            spec = self.get(key)
            frozen[key] = {"version": spec.version, "sha256": spec.sha256}
        return frozen

    def get_frozen(self, key: str, version: int, sha256: str) -> PromptSpec:
        spec = self.get(key, version)
        if spec.sha256 != sha256:
            # 冻结后模板内容变了，说明违反了「已发布版本不可原位改字」
            raise PromptUnavailable(f"{key}@{version} 与冻结哈希不一致")
        return spec


def _load(
    root: Path,
) -> tuple[dict[tuple[str, int], PromptSpec], list[RegistryIssue], dict[tuple[str, int], str], dict[str, int]]:
    issues: list[RegistryIssue] = []
    specs: dict[tuple[str, int], PromptSpec] = {}
    statuses: dict[tuple[str, int], str] = {}

    manifest_path = root / "manifest.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(RegistryIssue("manifest_missing", "找不到 manifest.toml"))
        return specs, issues, statuses, {}
    except tomllib.TOMLDecodeError as exc:
        issues.append(RegistryIssue("manifest_invalid", f"manifest.toml 无法解析：{exc}"))
        return specs, issues, statuses, {}

    entries = manifest.get("prompt")
    if not isinstance(entries, list):
        issues.append(RegistryIssue("manifest_invalid", "manifest.toml 缺少 [[prompt]] 条目"))
        entries = []

    seen: dict[tuple[str, int], int] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and isinstance(entry.get("version"), int):
            ident = (entry["key"], entry["version"])
            seen[ident] = seen.get(ident, 0) + 1
    duplicates = {ident for ident, count in seen.items() if count > 1}
    for key, version in sorted(duplicates):
        # 重复键无法判断哪条才是发布意图，两条都不采信
        issues.append(RegistryIssue("duplicate_key", f"{key}@{version} 重复登记", key, version))

    for entry in entries:
        spec, entry_issues = _parse_entry(root, entry)
        issues.extend(entry_issues)
        if spec is None:
            continue
        ident = (spec.key, spec.version)
        statuses[ident] = spec.status
        if ident in duplicates or entry_issues:
            continue
        specs[ident] = spec

    defaults: dict[str, int] = {}
    raw_defaults = manifest.get("defaults", {})
    if not isinstance(raw_defaults, dict):
        issues.append(RegistryIssue("manifest_invalid", "[defaults] 必须是表"))
        raw_defaults = {}
    for key, version in raw_defaults.items():
        if not isinstance(version, int) or isinstance(version, bool):
            issues.append(RegistryIssue("default_invalid", "默认版本必须是整数", key))
            continue
        if (key, version) not in statuses:
            issues.append(RegistryIssue("default_invalid", f"默认版本 {key}@{version} 未登记", key, version))
            continue
        if statuses[(key, version)] == "retired":
            issues.append(RegistryIssue("default_invalid", f"默认版本 {key}@{version} 已退役", key, version))
            continue
        defaults[key] = version

    for key in REQUIRED_PROMPT_KEYS:
        if not any(k == key for k, _ in statuses):
            issues.append(RegistryIssue("missing_prompt", f"必需的 {key} 未登记", key))
        elif key not in raw_defaults:
            issues.append(RegistryIssue("default_invalid", f"{key} 缺少默认版本", key))

    return specs, issues, statuses, defaults


def _parse_entry(root: Path, entry: Any) -> tuple[PromptSpec | None, list[RegistryIssue]]:
    if not isinstance(entry, dict):
        return None, [RegistryIssue("entry_invalid", "[[prompt]] 条目不是表")]
    key = entry.get("key")
    version = entry.get("version")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        return None, [RegistryIssue("entry_invalid", f"key 不合法：{key!r}")]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None, [RegistryIssue("entry_invalid", "version 必须是正整数", key)]

    issues: list[RegistryIssue] = []

    def fail(code: str, detail: str) -> None:
        issues.append(RegistryIssue(code, detail, key, version))

    try:
        role = ModelRole(entry.get("role"))
    except ValueError:
        fail("entry_invalid", f"role 不合法：{entry.get('role')!r}")
        role = ModelRole.CHAT

    message_role = entry.get("message_role")
    if message_role not in {"system", "user"}:
        fail("entry_invalid", "message_role 必须是 system 或 user")
        message_role = "user"

    status = entry.get("status")
    if status not in _STATUSES:
        fail("entry_invalid", f"status 不合法：{status!r}")
        status = "unverified"

    output_schema = entry.get("output_schema")
    if not isinstance(output_schema, str) or not output_schema:
        fail("missing_output_schema", "未声明输出 schema")
        output_schema = ""
    elif output_schema not in OUTPUT_SCHEMAS:
        fail("missing_output_schema", f"输出 schema {output_schema} 未在 schemas.py 登记")
    elif message_role == "system" and output_schema != NO_OUTPUT:
        fail("entry_invalid", "system 段不产生输出，output_schema 必须为 none@1")
    elif message_role == "user" and output_schema == NO_OUTPUT:
        fail("missing_output_schema", "user 段必须声明实际输出 schema")

    allowed_raw = entry.get("allowed_input_trust")
    allowed: set[TrustLevel] = set()
    if not isinstance(allowed_raw, list) or not allowed_raw:
        fail("entry_invalid", "allowed_input_trust 必须是非空列表")
    else:
        for item in allowed_raw:
            try:
                allowed.add(TrustLevel(item))
            except ValueError:
                fail("entry_invalid", f"未知信任级别 {item!r}")
    if message_role == "system" and allowed - {TrustLevel.SYSTEM_POLICY}:
        fail("trust_violation", "system 段只允许 system_policy 输入")

    max_in = entry.get("max_input_tokens")
    max_out = entry.get("max_output_tokens")
    if not isinstance(max_in, int) or isinstance(max_in, bool) or max_in <= 0:
        fail("entry_invalid", "max_input_tokens 必须是正整数")
        max_in = 0
    if not isinstance(max_out, int) or isinstance(max_out, bool) or max_out < 0 or (max_out == 0 and output_schema != NO_OUTPUT):
        fail("entry_invalid", "max_output_tokens 必须是正整数（仅 none@1 可为 0）")
        max_out = 0

    change_note = entry.get("change_note")
    if not isinstance(change_note, str) or not change_note.strip():
        fail("entry_invalid", "缺少变更说明")
        change_note = ""

    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        fail("entry_invalid", "sha256 必须是 64 位小写十六进制")
        sha256 = ""

    variables: list[VariableSpec] = []
    raw_vars = entry.get("variables", [])
    if not isinstance(raw_vars, list):
        fail("entry_invalid", "variables 必须是列表")
        raw_vars = []
    for raw in raw_vars:
        if not isinstance(raw, dict):
            fail("entry_invalid", "变量声明必须是表")
            continue
        name, vtype, trust_raw = raw.get("name"), raw.get("type"), raw.get("trust")
        if not isinstance(name, str) or not _VAR_NAME_RE.match(name):
            fail("entry_invalid", f"变量名不合法：{name!r}")
            continue
        if vtype not in _VAR_TYPES:
            fail("entry_invalid", f"变量 {name} 类型不合法：{vtype!r}")
            continue
        try:
            trust = TrustLevel(trust_raw)
        except ValueError:
            fail("entry_invalid", f"变量 {name} 信任级别不合法：{trust_raw!r}")
            continue
        if trust not in allowed:
            fail("trust_violation", f"变量 {name} 的信任级别 {trust.value} 不在允许范围内")
        variables.append(VariableSpec(name=name, type=vtype, trust=trust))
    names = [v.name for v in variables]
    if len(set(names)) != len(names):
        fail("entry_invalid", "变量名重复")

    template_path = root / "templates" / key / f"{version}.md"
    try:
        raw_bytes = template_path.read_bytes()
    except FileNotFoundError:
        fail("missing_file", f"缺少模板文件 templates/{key}/{version}.md")
        raw_bytes = None

    template = ""
    if raw_bytes is not None:
        actual = hashlib.sha256(raw_bytes).hexdigest()
        if sha256 and actual != sha256:
            fail("hash_mismatch", "模板文件哈希与 manifest 不符（已发布版本不得原位改字）")
        try:
            template = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            fail("entry_invalid", "模板文件不是 UTF-8")
        used = set(_PLACEHOLDER_RE.findall(template))
        # 去掉合法占位符后仍有花括号对，说明写了无法识别的占位符，宁可拒绝也不原样发给模型
        leftover = _PLACEHOLDER_RE.sub("", template)
        if "{{" in leftover or "}}" in leftover:
            fail("malformed_placeholder", "模板含无法识别的占位符")
        undeclared = sorted(used - set(names))
        unused = sorted(set(names) - used)
        if undeclared:
            fail("undeclared_variable", f"模板使用了未声明变量：{undeclared}")
        if unused:
            fail("unused_variable", f"声明的变量未在模板中使用：{unused}")

    spec = PromptSpec(
        key=key,
        version=version,
        sha256=sha256,
        role=role,
        message_role=message_role,
        output_schema=output_schema,
        allowed_input_trust=frozenset(allowed),
        max_input_tokens=max_in,
        max_output_tokens=max_out,
        status=status,
        change_note=change_note,
        variables=tuple(variables),
        template=template,
    )
    return spec, issues


@lru_cache(maxsize=1)
def get_prompt_registry() -> PromptRegistry:
    return PromptRegistry.load()
