"""Build Ledger + the configured local SDK as portable wheels, without deploying."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New, empty build output directory"
    )
    parser.add_argument(
        "--platform", type=Path, default=Path(__file__).resolve().parents[2] / "shadow-platform"
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument(
        "--offline", action="store_true", help="Use only already cached build dependencies"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output must be empty; existing releases are never overwritten")
    if not (args.platform / "pyproject.toml").is_file():
        parser.error("provide the configured Shadow Platform SDK source with --platform")
    output.mkdir(parents=True, exist_ok=True)
    for project in (args.platform.resolve(), root):
        subprocess.run(
            [
                "uv",
                *(["--offline"] if args.offline else []),
                "--cache-dir",
                str(args.cache_dir.resolve()),
                "build",
                "--wheel",
                "--out-dir",
                str(output),
                str(project),
            ],
            check=True,
        )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 2:
        raise RuntimeError("expected Ledger and SDK wheels")
    manifest = {
        "format": 1,
        "scope": "application-and-sdk-wheels",
        "notice": "第三方依赖仍需按锁文件准备；不含凭据、生产配置、数据库或完整离线依赖。",
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
        ),
        "artifacts": [
            {
                "file": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "bytes": wheel.stat().st_size,
            }
            for wheel in wheels
        ],
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Built application and SDK wheels with checksums; no deployment performed.")


if __name__ == "__main__":
    main()
