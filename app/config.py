from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGER_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "production"
    database_url: SecretStr | None = None
    database_url_file: Path | None = None
    oidc_issuer: str = ""
    oidc_client_id: str = "shadow-ledger"
    oidc_client_secret: SecretStr | None = None
    oidc_client_secret_file: Path | None = None
    oidc_callbacks: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    required_group: str = "ledger-users"
    session_secret: SecretStr | None = None
    session_secret_file: Path | None = None
    session_ttl_seconds: int = 86400 * 30
    default_currency: str = "CNY"
    default_timezone: str = "Asia/Shanghai"
    asset_base_url: str | None = None
    asset_service_token_file: Path | None = None
    capture_provider_url: str | None = None
    capture_provider_key_file: Path | None = None
    platform_resolver_url: str | None = None
    service_token_hashes_file: Path | None = None
    agent_registry_path: Path | None = None
    agent_secrets_dir: Path | None = None
    trusted_proxies: list[str] = Field(default_factory=list)
    lan_bypass_hosts: list[str] = Field(default_factory=list)
    dev_auth: bool = False
    max_request_bytes: int = 2_000_000
    worker_poll_seconds: float = 1.0

    @field_validator("default_currency")
    @classmethod
    def currency(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("default_currency must be a three-letter code")
        return value

    @field_validator("oidc_callbacks", "allowed_origins", "lan_bypass_hosts", mode="before")
    @classmethod
    def parse_list(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _read_secret(direct: SecretStr | None, path: Path | None, name: str) -> str:
        if direct is not None:
            return direct.get_secret_value()
        if path is None:
            raise ValueError(f"{name} is required")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read {name} file") from exc
        if not value:
            raise ValueError(f"{name} file is empty")
        return value

    @property
    def resolved_database_url(self) -> str:
        return self._read_secret(self.database_url, self.database_url_file, "database_url")

    @property
    def resolved_oidc_client_secret(self) -> str:
        return self._read_secret(
            self.oidc_client_secret, self.oidc_client_secret_file, "oidc_client_secret"
        )

    @property
    def resolved_session_secret(self) -> str:
        return self._read_secret(self.session_secret, self.session_secret_file, "session_secret")

    @staticmethod
    def read_optional_secret(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("cannot read configured secret file") from exc
        return value or None

    @model_validator(mode="after")
    def secure_production(self) -> Settings:
        if bool(self.agent_registry_path) != bool(self.agent_secrets_dir):
            raise ValueError("agent registry and secrets directory must be configured together")
        if self.env == "production":
            if self.dev_auth:
                raise ValueError("dev_auth is forbidden in production")
            if not self.oidc_issuer.startswith("https://"):
                raise ValueError("production oidc_issuer must use HTTPS")
            if not self.oidc_callbacks or not self.allowed_origins:
                raise ValueError("callback and origin allowlists are required")
            for callback in self.oidc_callbacks:
                if not callback.startswith("https://") or not callback.endswith("/auth/callback"):
                    raise ValueError("OIDC callbacks must be exact HTTPS callback URLs")
            _ = (
                self.resolved_database_url,
                self.resolved_oidc_client_secret,
                self.resolved_session_secret,
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
