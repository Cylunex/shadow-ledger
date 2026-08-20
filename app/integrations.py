from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.errors import AppError
from app.schemas import AssetInit


class AssetClient:
    """Strict adapter for Shadow Asset; upload tokens are never persisted or logged."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = (settings.asset_base_url or "").rstrip("/")
        self.token = settings.read_optional_secret(settings.asset_service_token_file)

    def _headers(self) -> dict[str, str]:
        if not self.base_url or not self.token:
            raise AppError(503, "asset_not_configured", "Asset 服务未配置")
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _asset_owner_id(owner_id: str) -> str:
        """Map Ledger's stable owner key to Shadow Asset's opaque UUID owner id."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"shadow-ledger:{owner_id}"))

    def init_upload(self, owner_id: str, data: AssetInit, idempotency_key: str) -> dict[str, Any]:
        return self._init_upload(
            owner_id, data.filename, data.mime_type, data.size, idempotency_key, "ledger-evidence"
        )

    def _init_upload(
        self,
        owner_id: str,
        filename: str,
        mime_type: str,
        size: int,
        idempotency_key: str,
        retention_policy_key: str,
    ) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/v1/upload-sessions",
                headers={**self._headers(), "Idempotency-Key": idempotency_key},
                json={
                    "owner_id": self._asset_owner_id(owner_id),
                    "ownership_mode": "user_owned",
                    "access_mode": "private",
                    "sensitivity": "sensitive",
                    "retention_policy_key": retention_policy_key,
                    "display_name": filename,
                    "original_filename": filename,
                    "content_type": mime_type,
                    "size_bytes": size,
                },
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, "asset_unavailable", "Asset 服务暂不可用") from exc
        canonical = result.get("target")
        alternates = result.get("alternate_targets", [])
        targets = [canonical, *alternates]
        if not result.get("upload_session_id") or not isinstance(canonical, dict):
            raise AppError(502, "asset_invalid_response", "Asset 返回了无效上传会话")
        if any(
            not isinstance(target, dict) or urlsplit(str(target.get("url", ""))).scheme != "https"
            for target in targets
        ):
            raise AppError(502, "asset_insecure_target", "Asset 上传目标必须使用 HTTPS")
        return {
            "upload_id": result["upload_session_id"],
            "expires_at": result.get("expires_at"),
            "canonical_target": canonical,
            "alternate_targets": alternates,
        }

    def complete_upload(self, upload_id: str) -> uuid.UUID:
        try:
            response = httpx.post(
                f"{self.base_url}/v1/upload-sessions/{upload_id}/complete",
                headers=self._headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError(502, "asset_complete_failed", "Asset 完成上传失败") from exc

    def create_reference(self, payload: dict[str, Any]) -> uuid.UUID:
        try:
            response = httpx.post(
                f"{self.base_url}/v1/asset-references",
                headers=self._headers(),
                json={
                    "asset_id": payload["asset_id"],
                    "resource_uri": payload["target_uri"],
                    "usage_role": payload.get("usage", "evidence"),
                    "reference_key": payload["reference_key"],
                    "binding_mode": "latest",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError(502, "asset_reference_failed", "Asset 引用创建失败") from exc

    def store_export(
        self,
        owner_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
        idempotency_key: str,
    ) -> uuid.UUID:
        try:
            upload = self._init_upload(
                owner_id,
                filename,
                mime_type,
                len(content),
                idempotency_key,
                "ledger-export",
            )
            target = upload["canonical_target"]
            response = httpx.request(
                target.get("method", "PUT"),
                target["url"],
                headers=target.get("headers", {}),
                content=content,
                timeout=60.0,
            )
            response.raise_for_status()
            return self.complete_upload(upload["upload_id"])
        except (httpx.HTTPError, ValueError, KeyError, AppError) as exc:
            if isinstance(exc, AppError):
                raise
            raise AppError(502, "export_upload_failed", "导出上传到 Asset 失败") from exc


class CaptureParserClient:
    """Schema-only OCR/AI port. The provider can create candidates, never confirmed facts."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def parse(self, source_id: uuid.UUID, asset_id: uuid.UUID) -> dict[str, Any]:
        if not self.settings.capture_provider_url:
            raise AppError(503, "parser_not_configured", "解析服务未配置")
        key = self.settings.read_optional_secret(self.settings.capture_provider_key_file)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            response = httpx.post(
                self.settings.capture_provider_url,
                headers=headers,
                json={
                    "source_id": str(source_id),
                    "asset_id": str(asset_id),
                    "output_contract": "shadow-ledger-record-candidates-v1",
                    "draft_only": True,
                },
                timeout=60.0,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, "parser_failed", "来源解析失败") from exc
        if not isinstance(result.get("candidates", []), list):
            raise AppError(502, "parser_invalid_response", "解析结果格式无效")
        return result
