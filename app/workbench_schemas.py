from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas import RecordCreate, StrictModel, _contains_sensitive_key


class FeedbackInput(StrictModel):
    revision: int = Field(ge=1)
    feedback_revision: int = Field(ge=0)
    state: Literal["dismissed", "snoozed", "handled", "active"]
    snoozed_until: datetime | None = None


class ObservationInput(StrictModel):
    external_revision: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]
    candidate: RecordCreate | None = None
    parser: str = Field(min_length=1, max_length=80)
    parser_version: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def safe(self):
        if _contains_sensitive_key(self.payload):
            raise ValueError("source payload must not contain credentials")
        if self.candidate and self.candidate.confirm:
            raise ValueError("observations never confirm facts")
        return self


class ObservationDecision(StrictModel):
    revision: int = Field(ge=1)
    action: Literal["keep", "apply"]
    record_id: UUID | None = None
    record_revision: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def target_required(self):
        if self.action == "apply" and (not self.record_id or not self.record_revision):
            raise ValueError("applying a candidate requires record_id and record_revision")
        return self


class EvidenceLinkInput(StrictModel):
    source_id: UUID
    record_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    redundant_draft_id: UUID | None = None
    redundant_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def duplicate_revision(self):
        if bool(self.redundant_draft_id) != bool(self.redundant_revision):
            raise ValueError("redundant draft and its revision are required together")
        return self


class IdentityAssignment(StrictModel):
    revision: int = Field(ge=1)
    merchant_id: UUID | None = None
    lines: dict[UUID, UUID] = Field(default_factory=dict, max_length=100)


class SuggestionDecision(StrictModel):
    action: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=500)
