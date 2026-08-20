from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, make_engine
from app.integrations import AssetClient
from app.models import RecurringCommitment, Reminder
from app.schemas import AssetInit
from app.worker import create_due_reminders


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def integration_settings(tmp_path) -> Settings:
    token = tmp_path / "asset-token"
    token.write_text("test-token", encoding="utf-8")
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        oidc_client_secret="oidc-secret",
        session_secret="session-secret",
        asset_base_url="https://asset.example.invalid",
        asset_service_token_file=token,
    )


def test_asset_adapter_only_accepts_service_returned_https_targets(monkeypatch, tmp_path):
    settings = integration_settings(tmp_path)
    request = {}

    def create_upload(url, **kwargs):
        request.update(url=url, **kwargs)
        return FakeResponse(
            {
                "upload_session_id": "upload-1",
                "target": {
                    "method": "PUT",
                    "url": "https://asset.example.invalid/upload/1",
                    "headers": {"Authorization": "Upload short-lived"},
                },
                "alternate_targets": [
                    {
                        "method": "PUT",
                        "url": "https://asset-lan.example.invalid/upload/1",
                        "headers": {"Authorization": "Upload short-lived"},
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "app.integrations.httpx.post",
        create_upload,
    )
    result = AssetClient(settings).init_upload(
        "alice", AssetInit(filename="receipt.png", mime_type="image/png", size=100), "upload-key-1"
    )
    assert result["upload_id"] == "upload-1"
    assert request["url"] == "https://asset.example.invalid/v1/upload-sessions"
    assert request["json"]["original_filename"] == "receipt.png"
    assert request["json"]["owner_id"] != "alice"
    assert "app_id" not in request["json"]

    monkeypatch.setattr(
        "app.integrations.httpx.post",
        lambda *_args, **_kwargs: FakeResponse(
            {
                "upload_session_id": "upload-2",
                "target": {
                    "method": "PUT",
                    "url": "http://unsafe.example.invalid/upload/2",
                    "headers": {},
                },
                "alternate_targets": [],
            }
        ),
    )
    with pytest.raises(Exception) as caught:
        AssetClient(settings).init_upload(
            "alice",
            AssetInit(filename="receipt.png", mime_type="image/png", size=100),
            "upload-key-2",
        )
    assert getattr(caught.value, "code", None) == "asset_insecure_target"


def test_recurring_worker_creates_one_occurrence_reminder():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            RecurringCommitment(
                owner_id="alice",
                kind="subscription",
                title="云服务",
                expected_amount=None,
                currency="CNY",
                recurrence_rule="FREQ=MONTHLY",
                timezone="Asia/Shanghai",
                next_due_at=datetime.now(UTC) + timedelta(days=1),
                remind_before_seconds=259200,
                state="active",
                revision=1,
            )
        )
        db.commit()
        assert create_due_reminders(db) == 1
        assert create_due_reminders(db) == 0
        assert db.scalar(select(func.count()).select_from(Reminder)) == 1
