"""JSON字符串增量与授权元数据分离；只发布用户可读的待核验正文。"""
import asyncio
from contextlib import suppress
import json
import re
import time

from app.conversation.safety import contains_internal_information, protected_values
from app.conversation.evidence import fact_suffix_position, render_fact_value
from app.prompts.schemas import NATURAL_CLAIM_KINDS, NATURAL_STATUSES, normalize_natural_metadata


class JsonPrefix:
    """跨分块处理JSON转义/Unicode/嵌套，不依赖字段固定位置。"""

    def __init__(self):
        self.stack, self.done = [], {}
        self.token, self.raw, self.escape, self.path = None, "", False, ()
        self.value = None

    def _path(self):
        if not self.stack: return ()
        node, path, key, state = self.stack[-1]
        return path + (len(node) if isinstance(node, list) else key,)

    def _put(self, value, path, complete=True):
        if not path: self.value = value
        elif self.stack:
            parent = self.stack[-1][0]
            if isinstance(parent, list):
                if len(parent) == path[-1]: parent.append(value)
                else: parent[path[-1]] = value
            else: parent[path[-1]] = value
        if complete:
            self.done[path] = value
            if self.stack: self.stack[-1][3] = "comma"

    def feed(self, text):
        updates = []
        for char in text:
            again = True
            while again:
                again = False
                if self.token == "string":
                    if char == '"' and not self.escape:
                        value = json.loads('"' + self.raw + '"')
                        if self.stack and self.stack[-1][3] == "key":
                            if value in self.stack[-1][0]: raise ValueError("JSON重复字段")
                            self.stack[-1][2], self.stack[-1][3] = value, "colon"
                        else:
                            self._put(value, self.path)
                            updates.append((self.path, value, True))
                        self.token, self.raw = None, ""
                    else:
                        self.raw += char
                        self.escape = char == "\\" and not self.escape
                    continue
                if self.token == "atom":
                    if char not in ",]} \t\r\n":
                        self.raw += char
                        continue
                    self._put(json.loads(self.raw), self.path)
                    self.token, self.raw, again = None, "", True
                    continue
                if char.isspace(): continue
                if char in "{[":
                    if len(self.stack) >= 32: raise ValueError("JSON嵌套超限")
                    path = self._path()
                    value = {} if char == "{" else []
                    self._put(value, path, False)
                    self.stack.append([value, path, None, "key" if char == "{" else "value"])
                elif char in "}]":
                    value, path, _, _ = self.stack.pop()
                    self._put(value, path)
                    updates.append((path, value, True))
                elif char == ":": self.stack[-1][3] = "value"
                elif char == ",": self.stack[-1][3] = "key" if isinstance(self.stack[-1][0], dict) else "value"
                elif char == '"':
                    self.token, self.raw, self.escape, self.path = "string", "", False, self._path()
                else:
                    self.token, self.raw, self.path = "atom", char, self._path()
        if self.token == "string" and self.stack[-1][3] != "key":
            # JSON decoder负责代理对合并；未闭合转义只保留在缓冲区。
            raw = self.raw
            while raw:
                try:
                    value = json.loads('"' + raw + '"')
                    if value and 0xD800 <= ord(value[-1]) <= 0xDBFF: value = value[:-1]
                    updates.append((self.path, value, False))
                    break
                except ValueError:
                    raw = raw[:-1]
                    if len(self.raw) - len(raw) > 12: break
        return updates


def safe_stream_prefix(text, protected):
    """仅暂留可能成为敏感内容的末尾；跨块配置值和敏感语法不先行显示。"""
    cut = len(text)
    # 完整命中由contains_internal_information拒绝；这里保留未完成的前缀。
    markers = ("sk-", "Bearer", "postgres://", "postgresql://", "redis://", "/Users/", "/home/", "/opt/",
        "CHAT_MODEL_", "EXTRACT_MODEL_", "TRANSLATE_MODEL_", "VISION_MODEL_",
        "系统提示词", "开发者提示词", "内部配置", "项目内部设计", "数据库连接串")
    for value in (*protected, *markers):
        for size in range(min(len(text), len(value)), 0, -1):
            if text[-size:].casefold() == value[:size].casefold():
                cut = min(cut, len(text) - size)
                break
    for marker in markers:
        pos = text.casefold().rfind(marker.casefold())
        if pos >= 0 and not re.search(r"[\s。！？\n]", text[pos + len(marker):]):
            cut = min(cut, pos)
        elif pos >= 0 and marker in {"系统提示词", "开发者提示词", "内部配置", "项目内部设计", "数据库连接串", "Bearer", "CHAT_MODEL_", "EXTRACT_MODEL_", "TRANSLATE_MODEL_", "VISION_MODEL_"}:
            tail = text[pos + len(marker):]
            if not re.search(r"[。！？\n]", tail):
                cut = min(cut, pos)
    return text[:cut]


class AnswerPreview:
    def __init__(self, emit, settings, *, sources=(), facts=()):
        self.emit, self.settings = emit, settings
        self.parser, self.texts, self.completed, self.sent = JsonPrefix(), {}, set(), {}
        self.facts = {f["evidence_id"]: f for f in facts}
        self.sources = {s["source_id"]: s for s in sources}
        self.natural = bool(facts)
        self.offset, self.started, self.queue, self.last = 0, False, "", time.monotonic()
        self.lock, self.ticker, self.error = asyncio.Lock(), None, None
        self.protected = protected_values()

    async def reset(self):
        async with self.lock:
            if self.error:
                raise self.error
            if self.started:
                await self.emit("answer_preview_reset", {"revision": 1, "reason": "Connection interrupted. Retrying the response."})
            self.parser, self.texts, self.completed, self.sent = JsonPrefix(), {}, set(), {}
            self.offset, self.started, self.queue, self.last = 0, False, "", time.monotonic()

    async def __aenter__(self):
        async def tick():
            try:
                while True:
                    await asyncio.sleep(.05)
                    async with self.lock:
                        if self.queue: await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = exc
        self.ticker = asyncio.create_task(tick())
        return self

    async def __aexit__(self, *args):
        self.ticker.cancel()
        with suppress(asyncio.CancelledError): await self.ticker

    def _render(self, text, complete, ids):
        if contains_internal_information(text): raise ValueError("内部信息候选被阻止")
        result, pos = "", 0
        for match in re.finditer(r"\[\[([^\[\]]+)\]\]", text):
            result += text[pos:match.start()]
            key = match[1]
            if self.natural:
                if key not in ids or key not in self.facts: raise ValueError("未知来源")
                fact = self.facts[key]
                value = render_fact_value(fact)
            else:
                from app.conversation.knowledge import knowledge_quote
                value = knowledge_quote(key, ids, self.sources.values())
            result += str(value)
            pos = fact_suffix_position(text, match.end(), fact, complete=complete) if self.natural else match.end()
        remainder = text[pos:]
        opening = remainder.find("[[")
        if opening >= 0:
            if complete or len(remainder) - opening > self.settings.chat_stream_pending_chars: raise ValueError("未闭合来源")
            remainder = remainder[:opening]
        elif not complete and remainder.endswith("["): remainder = remainder[:-1]
        result += remainder
        if any(0xD800 <= ord(c) <= 0xDFFF for c in result): raise ValueError("无效Unicode正文")
        if contains_internal_information(result): raise ValueError("内部信息候选被阻止")
        return result if complete else safe_stream_prefix(result, self.protected)

    async def feed(self, text):
        async with self.lock:
            if self.error: raise self.error
            await self._feed(text)

    async def _feed(self, text):
        for path, value, complete in self.parser.feed(text):
            if (len(path) == 2 and path[0] == "paragraphs" and isinstance(value, str)) or (len(path) == 3 and path[0] == "paragraphs" and path[-1] == "text"):
                self.texts[path[1]] = value
                if complete: self.completed.add(path[1])
        if sum(len(value) for value in self.texts.values()) > self.settings.chat_stream_max_chars:
            raise ValueError("正文缓冲超限")
        if self.parser.token and not (self.parser.token == "string" and self.parser.path and self.parser.path[0] == "paragraphs") and len(self.parser.raw) > self.settings.chat_stream_pending_chars:
            raise ValueError("协议字段缓冲超限")
        domain = self.parser.done.get(("domain",))
        if domain not in {"logistics", "related_material"}: return
        ids = self.parser.done.get(("source_ids",)) if not self.natural else None
        if not self.natural and ids is None: return
        if not self.natural and not isinstance(ids, list): raise ValueError("知识来源未授权")
        for index in sorted(self.texts):
            if index and index - 1 not in self.completed: break
            if self.natural:
                base = ("paragraphs", index)
                kind = self.parser.done.get(base + ("claim_kind",))
                ids = self.parser.done.get(base + ("evidence_ids",))
                status = self.parser.done.get(base + ("status",))
                if ids is None or status is None or kind is None: break
                metadata = normalize_natural_metadata({"claim_kind": kind, "status": status})
                kind, status = metadata["claim_kind"], metadata["status"]
                if (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
                    or not isinstance(kind, str) or kind not in NATURAL_CLAIM_KINDS
                    or not isinstance(status, str) or status not in NATURAL_STATUSES): raise ValueError("段落元数据无效")
                if any(i not in self.facts for i in ids): raise ValueError("段落来源不匹配")
            result = self._render(self.texts[index], index in self.completed, ids)
            previous = self.sent.get(index, "")
            if not result.startswith(previous): raise ValueError("正文前缀已改变")
            if result != previous:
                if index and not previous: self.queue += "\n\n"
                self.queue += result[len(previous):]
                self.sent[index] = result
            if index not in self.completed: break
        if self.offset + len(self.queue) > self.settings.chat_stream_max_chars: raise ValueError("正文超限")
        if len(self.queue) >= self.settings.chat_stream_event_chars or (self.queue and time.monotonic() - self.last >= .05):
            await self._flush()

    async def flush(self):
        async with self.lock:
            if self.error: raise self.error
            await self._flush()

    async def _flush(self):
        if self.queue and not self.started:
            await self.emit("answer_preview_started", {"revision": 1, "verification": "pending"})
            self.started = True
        while self.queue:
            chunk, self.queue = self.queue[:self.settings.chat_stream_event_chars], self.queue[self.settings.chat_stream_event_chars:]
            await self.emit("answer_preview_delta", {"revision": 1, "offset": self.offset, "text": chunk})
            self.offset += len(chunk)
        self.last = time.monotonic()
