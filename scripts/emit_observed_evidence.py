from __future__ import annotations

import argparse
import json
from pathlib import Path

from shadow_sdk.conformance import apply_evidence, load_json_object
from shadow_sdk.plugin_contracts import PluginContractError

from app.operational_evidence import (
    EvidenceError,
    build_observed_evidence,
    platform_contract_root,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit Platform observed-stage evidence from bounded Ledger probe results"
    )
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--probe-results", type=Path, required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        status = load_json_object(args.status.resolve(), label="capability status")
        probe = load_json_object(args.probe_results.resolve(), label="Ledger probe results")
        evidence = build_observed_evidence(
            status,
            probe["records"],
            evidence_id=args.evidence_id,
            correlation=probe["correlation"],
        )
        # Validation also enforces deployment/build binding and lifecycle ordering.
        apply_evidence(status, evidence, platform_root=platform_contract_root())
    except (EvidenceError, KeyError, PluginContractError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
