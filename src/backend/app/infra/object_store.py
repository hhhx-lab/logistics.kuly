"""
对象存储适配层。

约束（方案 4.1–4.3）：
- 桶保持私有；浏览器只拿限对象、限大小、短有效期的**仅写入**签名地址。
- 对象键由服务端随机生成，按 tenant/Case/类别分前缀；用户文件名只进元数据。
- 签名时使用浏览器实际访问的 Host（可能与容器内地址不同）。
- 正式读取一律走后端鉴权 Range 代理，不发可绕过撤权的直链。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings
from app.documents.models import StoredObjectCategory

# 首版只接受这四种格式，按魔数字节判定，不信任后缀与 Content-Type
MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    # DOCX 是 zip 容器
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04", b"PK\x05\x06"),
}
ACCEPTED_MIME = tuple(MAGIC_SIGNATURES)


@dataclass(frozen=True)
class ObjectHead:
    size_bytes: int
    etag: str


class ObjectStoreError(RuntimeError):
    pass


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        if not (settings.s3_endpoint and settings.s3_bucket and settings.s3_access_key and settings.s3_secret_key):
            raise ObjectStoreError("对象存储尚未配置")
        self._bucket = settings.s3_bucket
        self._ttl = settings.upload_signed_url_ttl_seconds
        common = {
            "region_name": settings.s3_region or "us-east-1",
            "aws_access_key_id": settings.s3_access_key.get_secret_value(),
            "aws_secret_access_key": settings.s3_secret_key.get_secret_value(),
            "config": Config(signature_version="s3v4", connect_timeout=5, read_timeout=20, retries={"max_attempts": 1}),
        }
        # 服务端读写用内部地址；签名给浏览器用公开地址，两者可能不同
        self._internal = boto3.client("s3", endpoint_url=settings.s3_endpoint, **common)
        self._public = boto3.client(
            "s3", endpoint_url=settings.s3_public_endpoint or settings.s3_endpoint, **common
        )

    @staticmethod
    def build_key(
        tenant_id: UUID,
        case_id: UUID,
        category: StoredObjectCategory,
        *,
        session_id: UUID | None = None,
    ) -> str:
        """
        对象键含随机后缀，不可猜测；用户文件名绝不拼进键里。
        不同类别用不同前缀，便于分别设置生命周期与清理。
        """
        parts = [f"t/{tenant_id}", f"c/{case_id}", category.value]
        if session_id is not None:
            parts.append(f"s/{session_id}")
        parts.append(secrets.token_urlsafe(24))
        return "/".join(parts)

    def presign_put(self, key: str, *, content_length: int) -> str:
        """
        签发仅写入地址。绑定 Content-Length 让对象存储自身也能拒绝超限，
        不只依赖前端声明；U05 仍按实际字节复核。
        """
        return self._public.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentLength": content_length},
            ExpiresIn=self._ttl,
        )

    def head(self, key: str) -> ObjectHead | None:
        try:
            r = self._internal.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStoreError(f"读取对象元数据失败：{exc}") from exc
        return ObjectHead(size_bytes=int(r["ContentLength"]), etag=(r.get("ETag") or "").strip('"'))

    def read_all(self, key: str) -> bytes:
        body = self._internal.get_object(Bucket=self._bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def read_range(self, key: str, start: int, end: int) -> bytes:
        """Range 代理按段读取，避免把整份原件读进内存。"""
        body = self._internal.get_object(Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end}")["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def stream(self, key: str, *, chunk_size: int = 512 * 1024):
        """分块流式读取，用于全量下载与哈希复算。"""
        body = self._internal.get_object(Bucket=self._bucket, Key=key)["Body"]
        try:
            while chunk := body.read(chunk_size):
                yield chunk
        finally:
            body.close()

    def sha256_of(self, key: str) -> tuple[str, int]:
        """
        流式复算哈希与字节数。

        不能用多段上传的 ETag 当 SHA-256，必须自己算。
        """
        digest = hashlib.sha256()
        total = 0
        for chunk in self.stream(key):
            digest.update(chunk)
            total += len(chunk)
        return digest.hexdigest(), total

    def copy(self, source_key: str, target_key: str) -> None:
        self._internal.copy_object(
            Bucket=self._bucket, Key=target_key, CopySource={"Bucket": self._bucket, "Key": source_key}
        )

    def delete(self, key: str) -> None:
        self._internal.delete_object(Bucket=self._bucket, Key=key)

    def close(self) -> None:
        """仅由拥有此客户端生命周期的调用方关闭，不能中断共享响应。"""
        self._internal.close()
        self._public.close()

    def delete_all_versions(self, key: str) -> int:
        """
        整票删除必须清掉所有 object versions 与 delete markers，
        只删「当前版本」会留下可恢复的历史版本。
        """
        removed = 0
        paginator = self._internal.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=key):
            for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ())):
                if entry["Key"] != key:
                    continue
                self._internal.delete_object(Bucket=self._bucket, Key=key, VersionId=entry["VersionId"])
                removed += 1
        return removed

    def delete_prefix(self, prefix: str) -> int:
        """按前缀清理该票所有对象的全部版本，用于 CasePurgeWorker。"""
        removed = 0
        paginator = self._internal.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ())):
                self._internal.delete_object(Bucket=self._bucket, Key=entry["Key"], VersionId=entry["VersionId"])
                removed += 1
        return removed


def sniff_mime(head_bytes: bytes) -> str | None:
    """按魔数判定真实格式。DOCX 与其他 zip 无法只靠魔数区分，交由调用方再验结构。"""
    for mime, signatures in MAGIC_SIGNATURES.items():
        if any(head_bytes.startswith(sig) for sig in signatures):
            return mime
    return None
