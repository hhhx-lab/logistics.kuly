from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import math
import re
import secrets
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.api.errors import InvalidRequest, ServiceUnavailable
from app.api.idempotency import request_digest
from app.extensions.models import CredentialEnvelope


class McpFailure(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def decode_response_json(data):
    def reject_constant(value):
        raise ValueError("non-finite JSON constant")
    try:
        value = json.loads(data, parse_constant=reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        raise McpFailure("invalid_response") from exc
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32 or isinstance(item, float) and not math.isfinite(item):
            raise McpFailure("invalid_response")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def validate_call_result(result):
    if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
        or "isError" in result and type(result["isError"]) is not bool
        or "structuredContent" in result and not isinstance(result["structuredContent"], dict)):
        raise McpFailure("invalid_response")
    for block in result["content"]:
        if not isinstance(block, dict):
            raise McpFailure("invalid_response")
        kind = block.get("type")
        if not isinstance(kind, str):
            raise McpFailure("invalid_response")
        if kind == "text":
            required = ("text",)
        elif kind in {"image", "audio"}:
            required = ("data", "mimeType")
        elif kind == "resource_link":
            required = ("uri", "name")
        elif kind == "resource":
            resource = block.get("resource")
            if (not isinstance(resource, dict) or not isinstance(resource.get("uri"), str)
                or ("text" in resource) == ("blob" in resource)
                or not isinstance(resource.get("text", resource.get("blob")), str)
                or "mimeType" in resource and not isinstance(resource["mimeType"], str)):
                raise McpFailure("invalid_response")
            required = ()
        else:
            raise McpFailure("invalid_response")
        if (any(not isinstance(block.get(name), str) for name in required)
            or any(name in block and not isinstance(block[name], str) for name in ("title", "description", "mimeType"))
            or "annotations" in block and not isinstance(block["annotations"], dict)):
            raise McpFailure("invalid_response")
    if result.get("isError") is True:
        raise McpFailure("tool_failed")
    return result


def master_key(settings):
    if not settings.mcp_credential_master_key:
        return None
    try:
        key = base64.urlsafe_b64decode(settings.mcp_credential_master_key.get_secret_value())
    except Exception:
        return None
    return key if len(key) == 32 else None


def seal_credential(settings, principal, value):
    key = master_key(settings)
    if key is None:
        raise ServiceUnavailable("个人凭证安全存储未配置，不能接收密钥")
    data_key = AESGCM.generate_key(bit_length=256)
    key_nonce, data_nonce = secrets.token_bytes(12), secrets.token_bytes(12)
    aad = f"{principal.tenant_id}:{principal.user_id}".encode()
    encode = lambda data: base64.urlsafe_b64encode(data).decode()
    return CredentialEnvelope(tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        nonce=encode(data_nonce), encrypted_key=encode(key_nonce + AESGCM(key).encrypt(key_nonce, data_key, aad)),
        ciphertext=encode(AESGCM(data_key).encrypt(data_nonce, value.encode(), aad)),
        algorithm="AES256-GCM-envelope@1", key_fingerprint=hashlib.sha256(key).hexdigest())


def open_credential(settings, principal, envelope):
    key = master_key(settings)
    if key is None or hashlib.sha256(key).hexdigest() != envelope.key_fingerprint:
        raise McpFailure("credential_unavailable")
    decode = base64.urlsafe_b64decode
    aad = f"{principal.tenant_id}:{principal.user_id}".encode()
    try:
        wrapped = decode(envelope.encrypted_key)
        data_key = AESGCM(key).decrypt(wrapped[:12], wrapped[12:], aad)
        return AESGCM(data_key).decrypt(decode(envelope.nonce), decode(envelope.ciphertext), aad).decode()
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise McpFailure("credential_unavailable") from exc


async def endpoint_and_addresses(endpoint, settings):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.port not in {None, 443}:
        raise McpFailure("unsafe_endpoint")
    host = parsed.hostname.lower().rstrip(".")
    if host not in settings.mcp_allowed_host_list():
        raise McpFailure("host_not_allowed")
    try:
        entries = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise McpFailure("dns_failed") from exc
    addresses = {e[4][0] for e in entries}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise McpFailure("unsafe_ip")
    return urlunsplit(("https", host, parsed.path or "/", "", "")), sorted(addresses)


async def validate_endpoint(endpoint, settings):
    normalized, _ = await endpoint_and_addresses(endpoint, settings)
    return normalized


def validate_schema(schema, *, depth=0):
    if depth > 12 or not isinstance(schema, dict):
        raise McpFailure("invalid_schema")
    if depth == 0:
        try:
            json.dumps(schema, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise McpFailure("invalid_schema") from exc
    supported = {"type", "properties", "required", "additionalProperties", "items", "anyOf", "oneOf", "allOf", "enum", "const", "title", "description", "default", "examples", "$schema", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "pattern"}
    if any(name not in supported for name in schema):
        raise McpFailure("unsupported_schema")
    if "$ref" in schema or "$dynamicRef" in schema:
        raise McpFailure("unsupported_schema")
    kind = schema.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in {"object", "string", "boolean", "integer", "number", "array", "null"}):
        raise McpFailure("unsupported_schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(name, str) for name in properties):
        raise McpFailure("invalid_schema")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise McpFailure("invalid_schema")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise McpFailure("invalid_schema")
    for name in ("minLength", "maxLength", "minItems", "maxItems"):
        if name in schema and (type(schema[name]) is not int or schema[name] < 0):
            raise McpFailure("invalid_schema")
    for name in ("minimum", "maximum"):
        if name in schema and type(schema[name]) not in (int, float):
            raise McpFailure("invalid_schema")
    if "pattern" in schema:
        import re
        if not isinstance(schema["pattern"], str):
            raise McpFailure("invalid_schema")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise McpFailure("invalid_schema") from exc
    for child in properties.values():
        validate_schema(child, depth=depth+1)
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if isinstance(additional, dict):
            validate_schema(additional, depth=depth+1)
        elif type(additional) is not bool:
            raise McpFailure("invalid_schema")
    if "items" in schema:
        validate_schema(schema["items"], depth=depth+1)
    for name in ("anyOf", "oneOf", "allOf"):
        if name in schema and (not isinstance(schema[name], list) or not schema[name]):
            raise McpFailure("invalid_schema")
        for child in schema.get(name, []):
            validate_schema(child, depth=depth+1)


def validate_arguments(schema, value, *, depth=0):
    if depth > 12:
        raise McpFailure("invalid_arguments")
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        successes = 0
        for child in alternatives:
            try:
                validate_arguments(child, value, depth=depth+1)
                successes += 1
            except McpFailure:
                pass
        if successes == 0 or ("oneOf" in schema and successes != 1):
            raise McpFailure("invalid_arguments")
    for child in schema.get("allOf", []):
        validate_arguments(child, value, depth=depth+1)
    kind = schema.get("type")
    ok = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
        "boolean": isinstance(value, bool), "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool), "null": value is None}.get(kind, True)
    if not ok or ("enum" in schema and value not in schema["enum"]) or ("const" in schema and value != schema["const"]):
        raise McpFailure("invalid_arguments")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])) or any(k not in properties for k in value):
            raise McpFailure("invalid_arguments")
        for name, child in value.items():
            validate_arguments(properties[name], child, depth=depth+1)
    if isinstance(value, str):
        if len(value) > min(schema.get("maxLength", 4000), 4000) or len(value) < schema.get("minLength", 0):
            raise McpFailure("invalid_arguments")
        if schema.get("pattern"):
            import re
            if not re.fullmatch(schema["pattern"], value):
                raise McpFailure("invalid_arguments")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise McpFailure("invalid_arguments")
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise McpFailure("invalid_arguments")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > min(schema.get("maxItems", 50), 50):
            raise McpFailure("invalid_arguments")
        for child in value:
            validate_arguments(schema.get("items", {}), child, depth=depth+1)


DEEPWIKI_PUBLIC_KEY = ("https://mcp.deepwiki.com/mcp", "read_wiki_structure", "2df4430e88464f19b7e83c0917d7bee8fcdb18322d13210c201c1c480d5a39a5")


def public_tool_registry(settings):
    """Published finite public values; remote annotations never grant data scope."""
    registry = {DEEPWIKI_PUBLIC_KEY: {"repoName": ["langchain-ai/langgraph"]}}
    raw = settings.mcp_public_tool_registry_json
    if raw is None or raw == "":
        return registry
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError()
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"endpoint", "tool_id", "schema_hash", "parameters"}:
                raise ValueError()
            endpoint, tool_id, schema_hash = entry["endpoint"], entry["tool_id"], entry["schema_hash"]
            if not all(isinstance(v, str) for v in (endpoint, tool_id, schema_hash)):
                raise ValueError()
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in {None, 443} or endpoint != urlunsplit(("https", parsed.hostname.lower(), parsed.path or "/", "", ""))):
                raise ValueError()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", tool_id) or not re.fullmatch(r"[0-9a-f]{64}", schema_hash):
                raise ValueError()
            parameters = entry["parameters"]
            if not isinstance(parameters, dict) or len(parameters) > 20:
                raise ValueError()
            for name, allowed in parameters.items():
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,59}", name) or not isinstance(allowed, list) or not 1 <= len(allowed) <= 64:
                    raise ValueError()
                if not all(type(v) in {str, bool} for v in allowed) or len({type(v) for v in allowed}) != 1:
                    raise ValueError()
                if any(isinstance(v, str) and (not v or len(v) > 200) for v in allowed) or len(set(allowed)) != len(allowed):
                    raise ValueError()
            key = endpoint, tool_id, schema_hash
            if key in seen:
                raise ValueError()
            seen.add(key)
            registry[key] = parameters
    except (ValueError, TypeError, KeyError):
        raise McpFailure("public_registry_invalid") from None
    return registry


def public_tool_policy(settings, endpoint, tool_id, schema_hash, schema):
    policy = public_tool_registry(settings).get((endpoint, tool_id, schema_hash))
    if policy is None:
        return None
    if request_digest(schema) != schema_hash or schema.get("type") != "object" or set(schema.get("properties", {})) != set(policy):
        return None
    for name, allowed in policy.items():
        expected_type = "boolean" if type(allowed[0]) is bool else "string"
        if schema["properties"][name].get("type") != expected_type:
            return None
        for value in allowed:
            candidate = {key: values[0] for key, values in policy.items()}
            candidate[name] = value
            try:
                validate_arguments(schema, candidate)
            except McpFailure:
                return None
    return policy


def validate_public_arguments(settings, endpoint, tool_id, schema_hash, schema, arguments):
    policy = public_tool_policy(settings, endpoint, tool_id, schema_hash, schema)
    if policy is None or not isinstance(arguments, dict) or set(arguments) != set(policy):
        raise McpFailure("data_scope_not_supported")
    validate_arguments(schema, arguments)
    if any(not any(type(arguments[name]) is type(value) and arguments[name] == value for value in allowed) for name, allowed in policy.items()):
        raise McpFailure("data_scope_not_supported")
    return policy


def tool_category(tool, endpoint=None, settings=None):
    # Classification is separate from published public scope and grants.
    key = endpoint, tool.get("name"), request_digest(tool.get("inputSchema", {}))
    annotations = tool.get("annotations") or {}
    if annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False:
        return "write"
    if key == DEEPWIKI_PUBLIC_KEY:
        return "read_only"
    return "read_only" if annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True else "write"


class StreamableHttpClient:
    def __init__(self, endpoint, settings, credential=None, *, client=None):
        self.endpoint, self.settings, self.credential = endpoint, settings, credential
        self.client = client
        self.session_id = None
        self.protocol = "2025-03-26"
        self.counter = 0
        self.stages = []

    async def request(self, method, params=None, *, notification=False):
        endpoint, addresses = await endpoint_and_addresses(self.endpoint, self.settings)
        parsed_endpoint = urlsplit(endpoint)
        chosen = next((ip for ip in addresses if ":" not in ip), addresses[0])
        authority = "[" + chosen + "]" if ":" in chosen else chosen
        connect_url = urlunsplit(("https", authority, parsed_endpoint.path, "", ""))
        self.counter += 1
        request_id = None if notification else self.counter
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        headers = {"Host": parsed_endpoint.hostname, "Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.credential:
            headers["Authorization"] = "Bearer " + self.credential
        client = self.client or httpx.AsyncClient(timeout=self.settings.mcp_timeout_seconds, follow_redirects=False, trust_env=False)
        try:
            async with client.stream("POST", connect_url, json=body, headers=headers, extensions={"sni_hostname": parsed_endpoint.hostname}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    # 当前模板不跟随重定向，凭据不会转交第二主机。
                    raise McpFailure("redirect_rejected")
                if response.status_code >= 400:
                    raise McpFailure("http_" + str(response.status_code))
                if response.headers.get("Mcp-Session-Id"):
                    self.session_id = response.headers["Mcp-Session-Id"]
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.settings.mcp_max_response_bytes:
                        raise McpFailure("response_too_large")
                if notification and not data:
                    return None
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    candidates = []
                    for block in data.decode().replace("\r\n", "\n").split("\n\n")[:100]:
                        lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
                        if lines:
                            candidates.append(decode_response_json("\n".join(lines)))
                    payload = next((p for p in candidates if isinstance(p, dict) and p.get("id") == request_id), None)
                else:
                    payload = decode_response_json(data) if data else None
                if notification:
                    return None
                if (not isinstance(payload, dict) or type(payload.get("id")) is not int
                    or payload["id"] != request_id or payload.get("jsonrpc") != "2.0"
                    or ("result" in payload) == ("error" in payload)):
                    raise McpFailure("invalid_response")
                if "error" in payload:
                    error = payload["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                        raise McpFailure("invalid_response")
                    raise McpFailure("rpc_error")
                return payload["result"]
        except UnicodeDecodeError as exc:
            raise McpFailure("invalid_response") from exc
        except httpx.HTTPError as exc:
            raise McpFailure(type(exc).__name__) from exc
        finally:
            if self.client is None:
                await client.aclose()

    async def stage(self, name, call):
        start = datetime.now(UTC)
        error = None
        try:
            return await call()
        except McpFailure as exc:
            error = exc.kind
            raise
        finally:
            end = datetime.now(UTC)
            self.stages.append({"stage": name, "started_at": start.isoformat(), "ended_at": end.isoformat(),
                "duration_ms": round((end-start).total_seconds()*1000), "ok": error is None, "error_class": error})

    async def discover(self):
        async def initialize():
            result = await self.request("initialize", {"protocolVersion": self.protocol,
                "capabilities": {}, "clientInfo": {"name": "logistics-document-agent", "version": "1.0"}})
            if (not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str) or not result["protocolVersion"]
                or "capabilities" in result and not isinstance(result["capabilities"], dict)):
                raise McpFailure("invalid_initialize")
            self.protocol = result["protocolVersion"]
        await self.stage("initialize", initialize)
        await self.request("notifications/initialized", notification=True)
        async def list_tools():
            result = await self.request("tools/list")
            tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(tools, list) or len(tools) > 100:
                raise McpFailure("invalid_tools")
            return tools
        tools = await self.stage("tools/list", list_tools)
        async def schemas():
            ids = set()
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"] or tool["name"] in ids or len(tool["name"]) > 160:
                    raise McpFailure("invalid_tool_name")
                if not isinstance(tool.get("description", ""), str) or not isinstance(tool.get("annotations", {}), dict):
                    raise McpFailure("invalid_tools")
                ids.add(tool["name"])
                validate_schema(tool.get("inputSchema"))
            return tools
        return await self.stage("schema", schemas)

    async def call(self, tool_id, arguments):
        async def invoke():
            result = await self.request("tools/call", {"name": tool_id, "arguments": arguments})
            return validate_call_result(result)
        return await self.stage("tools/call", invoke)
