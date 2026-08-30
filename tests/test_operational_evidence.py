from __future__ import annotations

from datetime import UTC, datetime

import pytest
from shadow_sdk.plugin_contracts import contract_schema_path, validate_document

from app.operational_evidence import (
    EvidenceError,
    build_observed_evidence,
    platform_contract_root,
)


def status() -> dict:
    return {
        "deployment_id": "shadow-home",
        "build_id": "a" * 64,
        "capabilities": [
            {
                "capability_ref": (
                    "shadow://capabilities/shadow-ledger/ledger-home/ledger.summary.read"
                ),
                "plugin_id": "shadow-ledger",
                "selected": True,
            },
            {
                "capability_ref": (
                    "shadow://capabilities/shadow-other/other-home/other.records.read"
                ),
                "plugin_id": "shadow-other",
                "selected": True,
            },
        ],
    }


def correlation() -> dict[str, str]:
    return {
        "run_id": "ledger-probe-one",
        "correlation_id": "ledger-conformance-one",
        "trace_id": "trace-one",
        "request_id": "request-one",
    }


def test_observed_evidence_is_bound_to_exact_platform_build():
    capability_ref = status()["capabilities"][0]["capability_ref"]
    evidence = build_observed_evidence(
        status(),
        [
            {
                "capability_ref": capability_ref,
                "status": "passed",
                "detail": "bounded summary probe passed without domain payload output",
                "checks": [
                    {"name": "summary-contract", "category": "contract", "status": "passed"},
                    {"name": "summary-auth", "category": "security", "status": "passed"},
                ],
            }
        ],
        evidence_id="ledger-observed-one",
        correlation=correlation(),
        observed_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert evidence["protocol"] == "shadow.conformance-evidence.v1"
    assert evidence["deployment_id"] == "shadow-home"
    assert evidence["build_id"] == "a" * 64
    assert evidence["records"][0]["stage"] == "observed"
    assert "shadow-other" not in str(evidence)
    validate_document(
        evidence,
        contract_schema_path(
            platform_contract_root(), "shadow-conformance-evidence.schema.json"
        ),
        label="ledger observed evidence",
    )


def test_observed_evidence_requires_every_selected_ledger_capability():
    with pytest.raises(EvidenceError, match="missing Ledger probe results"):
        build_observed_evidence(
            status(),
            [],
            evidence_id="ledger-observed-missing",
            correlation=correlation(),
        )
