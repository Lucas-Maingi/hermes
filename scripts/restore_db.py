"""Restore a SQLite backup after stopping the Hermes API and worker processes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from hermes.maintenance import restore_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, help="Validated SQLite backup file")
    parser.add_argument("--db", default=os.getenv("HERMES_DB", "hermes.db"))
    parser.add_argument(
        "--confirm-replace",
        action="store_true",
        help="Required acknowledgement; the current database is first preserved as a rollback backup",
    )
    args = parser.parse_args()

    rollback = restore_database(
        Path(args.backup),
        Path(args.db),
        confirm_replace=args.confirm_replace,
    )
    if rollback:
        print(f"Previous database preserved at: {rollback}")
    print(f"Database restored from: {Path(args.backup).resolve()}")


if __name__ == "__main__":
    main()
