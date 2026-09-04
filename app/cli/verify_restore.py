from __future__ import annotations

import argparse
import json
from pathlib import Path

from shadow_sdk.conformance import load_json_object, restore_drill_to_evidence
from shadow_sdk.plugin_contracts import PluginContractError

from app.operational_evidence import EvidenceError, platform_contract_root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate an isolated Ledger restore drill and emit restore-tested evidence"
    )
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--drill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        status = load_json_object(args.status.resolve(), label="capability status")
        evidence = restore_drill_to_evidence(
            args.drill.resolve(), status, platform_root=platform_contract_root()
        )
    except (EvidenceError, PluginContractError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
