"""Create a consistent SQLite backup of the configured Hermes database."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from hermes.maintenance import backup_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("HERMES_DB", "hermes.db"))
    parser.add_argument("--directory", default="backups")
    args = parser.parse_args()

    source = Path(args.db)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(args.directory) / f"{source.stem}-{stamp}.sqlite3"
    saved = backup_database(source, destination)
    print(f"Backup created: {saved}")


if __name__ == "__main__":
    main()
