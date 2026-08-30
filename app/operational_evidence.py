from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import shadow_sdk


class EvidenceError(ValueError):
    pass


def platform_contract_root() -> Path:
    candidates = (
        Path(shadow_sdk.__file__).parent.parent,
        Path(shadow_sdk.__file__).parent,
        Path(__file__).parents[2] / "shadow-platform",
    )
    for candidate in candidates:
        if (candidate / "contracts" / "shadow-conformance-evidence.schema.json").is_file():
            return candidate
    raise EvidenceError("installed shadow-platform contracts are unavailable")


def build_observed_evidence(
    capability_status: dict[str, Any],
    probe_results: list[dict[str, Any]],
    *,
    evidence_id: str,
    correlation: dict[str, str],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Bind Ledger probe results to one immutable Platform deployment build."""

    selected = {
        item["capability_ref"]: item
        for item in capability_status.get("capabilities", [])
        if item.get("selected") and item.get("plugin_id") == "shadow-ledger"
    }
    if not selected:
        raise EvidenceError("capability status has no selected shadow-ledger capabilities")
    by_ref = {item.get("capability_ref"): item for item in probe_results}
    missing = sorted(set(selected) - set(by_ref))
    unknown = sorted(set(by_ref) - set(selected))
    if missing:
        raise EvidenceError(f"missing Ledger probe results: {missing}")
    if unknown:
        raise EvidenceError(f"unknown or unselected Ledger probe results: {unknown}")
    records: list[dict[str, Any]] = []
    for capability_ref in sorted(selected):
        result = by_ref[capability_ref]
        if result.get("status") not in {"passed", "failed"}:
            raise EvidenceError(f"invalid probe status for {capability_ref}")
        detail = result.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            raise EvidenceError(f"probe detail is required for {capability_ref}")
        record: dict[str, Any] = {
            "capability_ref": capability_ref,
            "stage": "observed",
            "status": result["status"],
            "detail": detail[:500],
        }
        if result.get("checks"):
            record["checks"] = result["checks"]
        records.append(record)
    timestamp = (observed_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    return {
        "version": 1,
        "protocol": "shadow.conformance-evidence.v1",
        "evidence_id": evidence_id,
        "producer": {
            "project_id": "ledger",
            "component": "capability-probe",
        },
        "deployment_id": capability_status["deployment_id"],
        "build_id": capability_status["build_id"],
        "observed_at": timestamp,
        "correlation": correlation,
        "records": records,
    }
