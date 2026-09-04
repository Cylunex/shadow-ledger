"""Compatibility entry point; installed commands use the private app.cli namespace."""
from app.cli.emit_observed_evidence import main

if __name__ == "__main__":
    main()
