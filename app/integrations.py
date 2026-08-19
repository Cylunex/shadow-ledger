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

    def init_upload(self, owner_id: str, data: AssetInit, idempotency_key: str) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/api/v1/uploads/init",
                headers={**self._headers(), "Idempotency-Key": idempotency_key},
                json={"app_id": "ledger", "owner_id": owner_id, **data.model_dump()},
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, "asset_unavailable", "Asset 服务暂不可用") from exc
        targets = [result.get("canonical_target"), *result.get("alternate_targets", [])]
        if not result.get("upload_id") or not targets[0]:
            raise AppError(502, "asset_invalid_response", "Asset 返回了无效上传会话")
        if any(urlsplit(target).scheme != "https" for target in targets if target):
            raise AppError(502, "asset_insecure_target", "Asset 上传目标必须使用 HTTPS")
        return result

    def complete_upload(self, upload_id: str) -> uuid.UUID:
        try:
            response = httpx.post(
                f"{self.base_url}/api/v1/uploads/{upload_id}/complete",
                headers=self._headers(),
                json={"app_id": "ledger"},
                timeout=10.0,
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["asset_id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError(502, "asset_complete_failed", "Asset 完成上传失败") from exc

    def create_reference(self, payload: dict[str, Any]) -> uuid.UUID:
        try:
            response = httpx.post(
                f"{self.base_url}/api/v1/references",
                headers=self._headers(),
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["reference_id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AppError(502, "asset_reference_failed", "Asset 引用创建失败") from exc

    def store_export(self, filename: str, mime_type: str, content: bytes) -> uuid.UUID:
        try:
            response = httpx.post(
                f"{self.base_url}/api/v1/assets",
                headers=self._headers(),
                data={"app_id": "ledger", "usage": "export"},
                files={"file": (filename, content, mime_type)},
                timeout=60.0,
            )
            response.raise_for_status()
            return uuid.UUID(response.json()["asset_id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
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
