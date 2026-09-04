"""Compatibility entry point; installed commands use the private app.cli namespace."""
from app.cli.verify_restore import main

if __name__ == "__main__":
    main()
