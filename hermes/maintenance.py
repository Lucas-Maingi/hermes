"""Safe SQLite backup and restore helpers for single-instance deployments."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _validate_database(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError(f"SQLite integrity check failed for {path}")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"businesses", "conversations"}.issubset(tables):
            raise ValueError(f"{path} does not contain a Hermes platform database")


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Create a consistent SQLite snapshot without overwriting an existing file."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("Backup destination must differ from the live database")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    _validate_database(source_path)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(source_path)) as source_connection:
            with closing(sqlite3.connect(temporary_path)) as destination_connection:
                source_connection.backup(destination_connection, pages=256)
                result = destination_connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        if destination_path.exists():
            raise FileExistsError(destination_path)
        os.replace(temporary_path, destination_path)
        if os.name != "nt":
            destination_path.chmod(0o600)
        return destination_path
    finally:
        temporary_path.unlink(missing_ok=True)


def restore_database(
    backup: str | Path,
    destination: str | Path,
    *,
    confirm_replace: bool = False,
) -> Path | None:
    """Restore a snapshot, first saving the current database as a rollback copy."""
    source_path = Path(backup).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("Backup and restore destination must differ")
    _validate_database(source_path)
    if not confirm_replace:
        raise ValueError("Pass confirm_replace=True after stopping the application")

    rollback_path = None
    if destination_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rollback_path = destination_path.with_name(
            f"{destination_path.name}.pre-restore-{stamp}-{uuid.uuid4().hex[:8]}.bak"
        )
        backup_database(destination_path, rollback_path)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_path)) as source_connection:
        with closing(sqlite3.connect(destination_path)) as destination_connection:
            source_connection.backup(destination_connection, pages=256)
            result = destination_connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValueError("Restored database integrity check failed")
    if os.name != "nt":
        destination_path.chmod(0o600)
    return rollback_path
