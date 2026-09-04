import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser(
        description="Run packaged Ledger migrations using configured secrets"
    )
    parser.add_argument("--revision", default="head")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "release"
    if not root.is_dir():
        root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, args.revision)


if __name__ == "__main__":
    main()
