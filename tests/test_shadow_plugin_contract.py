from __future__ import annotations

from pathlib import Path

import shadow_sdk
import yaml
from shadow_sdk.plugin_contracts import validate_plugin

from app.main import create_app

ROOT = Path(__file__).parents[1]


def _platform_root() -> Path:
    candidates = (
        ROOT.parent / "shadow-platform",
        ROOT.parents[1] / "shadow-platform",
        Path(shadow_sdk.__file__).parent,
    )
    return next(
        path
        for path in candidates
        if (path / "contracts" / "shadow-plugin.schema.json").is_file()
    )


def _application_routes(router: object) -> set[tuple[str, str]]:
    collected: set[tuple[str, str]] = set()
    for route in getattr(router, "routes", []):
        included = getattr(route, "original_router", None)
        if included is not None:
            collected.update(_application_routes(included))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        collected.update((path, method) for method in getattr(route, "methods", set()))
    return collected


def test_shadow_plugin_contract_matches_machine_routes(settings) -> None:
    plugin = validate_plugin(ROOT, _platform_root())
    contract = yaml.safe_load((ROOT / "contracts" / "agent.openapi.yaml").read_text("utf-8"))
    declared_routes = {
        (path, method.upper())
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    actual_routes = _application_routes(create_app(settings))

    assert plugin.plugin_id == "shadow-ledger"
    assert plugin.version == "1.0.0"
    assert declared_routes <= actual_routes
    assert {item["id"] for item in plugin.agent_manifest["capabilities"]} == {
        "ledger.summary.read",
        "ledger.records.read",
        "ledger.budgets.read",
        "ledger.records.draft",
    }


def test_machine_openapi_does_not_claim_account_payment_or_final_write() -> None:
    contract = yaml.safe_load((ROOT / "contracts" / "agent.openapi.yaml").read_text("utf-8"))
    serialized = yaml.safe_dump(contract, sort_keys=True).lower()
    draft_properties = contract["components"]["schemas"]["AgentRecordDraftCreate"]["properties"]

    assert {"account_id", "payment_method", "exchange_rate", "confirm"}.isdisjoint(
        draft_properties
    )
    assert "/confirm" not in serialized
    assert "/exports" not in serialized
