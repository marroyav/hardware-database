"""Small DB-API adapter and ordered migrations for DAPHNE production data."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import ProductionError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "sqlite:///build/daphne-production.db"
DEFAULT_MIGRATIONS = REPOSITORY_ROOT / "migrations/production"


class Database:
    """Open SQLite today and PostgreSQL when the optional driver is installed."""

    def __init__(self, url: str = DEFAULT_DATABASE_URL, migrations: Path = DEFAULT_MIGRATIONS):
        self.url = url
        self.migrations = migrations
        if url.startswith("sqlite:///"):
            self.backend = "sqlite"
            self.sqlite_path = Path(url.removeprefix("sqlite:///"))
        elif url.startswith(("postgresql://", "postgres://")):
            self.backend = "postgresql"
            self.sqlite_path = None
        else:
            self.backend = "sqlite"
            self.sqlite_path = Path(url)

    def connect(self) -> Any:
        if self.backend == "sqlite":
            assert self.sqlite_path is not None
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.sqlite_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            return connection
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ProductionError(
                "PostgreSQL requires the optional 'psycopg[binary]' package"
            ) from exc
        return psycopg.connect(self.url, row_factory=dict_row)

    def execute(self, connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.backend == "postgresql":
            sql = sql.replace("?", "%s")
        return connection.execute(sql, params)

    def migrate(self) -> list[str]:
        connection = self.connect()
        applied_now: list[str] = []
        try:
            self.execute(
                connection,
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
            )
            connection.commit()
            applied = {
                row["version"]
                for row in self.execute(
                    connection, "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            migration_dir = self.migrations / self.backend
            if not migration_dir.is_dir():
                raise ProductionError(f"migration directory does not exist: {migration_dir}")
            migration_files = sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))
            if not migration_files:
                raise ProductionError(f"no migrations found in: {migration_dir}")
            from .values import utc_now

            for path in migration_files:
                version = path.name.split("_", 1)[0]
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                if self.backend == "sqlite":
                    connection.executescript(sql)
                else:
                    connection.execute(sql)
                self.execute(
                    connection,
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
                connection.commit()
                applied_now.append(version)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return applied_now

    def require_ready(self) -> None:
        connection = self.connect()
        try:
            row = self.execute(
                connection, "SELECT version FROM schema_migrations LIMIT 1"
            ).fetchone()
            if row is None:
                raise ProductionError("production database has no applied migrations")
        except Exception as exc:
            if isinstance(exc, ProductionError):
                raise
            raise ProductionError("production database is not initialized; run migrate") from exc
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[Any]:
        connection = self.connect()
        try:
            if self.backend == "sqlite" and immediate:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
