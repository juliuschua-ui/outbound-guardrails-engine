"""SQLite connection, migration runner, Phoenix calendar date. Contract: PLAN.md §12.2.

Connections are plain `sqlite3` in the driver's default (legacy) transaction mode, so
`with conn:` still gives you a transaction and callers that need `BEGIN IMMEDIATE`
(the cap-check-and-insert in §10c) can issue it themselves.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

#: Arizona does not observe DST, so the Phoenix calendar day is a fixed UTC-7 shift.
#: Deliberately not `zoneinfo('America/Phoenix')` — no tzdata dependency, no surprise
#: when the system tz database is absent. Matches sendqueue.js PHX_OFFSET_MS.
PHX_UTC_OFFSET = timedelta(hours=-7)

_MIGRATION_RE = re.compile(r"^(\d+)_[^/]*\.sql$")


def connect(cfg: Config) -> sqlite3.Connection:
    """Open the engine database with the pragmas every caller depends on."""
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # OFF by default — must be per connection
    conn.execute("PRAGMA journal_mode = WAL")     # persistent, but idempotent to re-assert
    conn.execute("PRAGMA busy_timeout = 5000")    # concurrent stage runs queue instead of failing
    return conn


def discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return (version, path) pairs in numeric order. Raises on duplicate versions."""
    found: dict[int, Path] = {}
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {found[version]} and {path}")
        found[version] = path
    return sorted(found.items())


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, migrations_dir: Path) -> list[int]:
    """Apply every migration not yet recorded, each in its own transaction.

    Returns the versions applied by THIS call (empty list when already current).
    Applied migration files are immutable — a schema change is always a new file.
    """
    pending = [(v, p) for v, p in discover_migrations(migrations_dir)
               if v not in applied_versions(conn)]
    if not pending:
        return []

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # we drive BEGIN/COMMIT inside the script text
    applied: list[int] = []
    try:
        for version, path in pending:
            sql = path.read_text(encoding="utf-8").strip()
            if not sql.endswith(";"):
                sql += ";"
            script = (
                "BEGIN;\n"
                f"{sql}\n"
                f"INSERT INTO schema_migrations (version) VALUES ({version});\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except Exception:
                _rollback_quietly(conn)
                raise
            applied.append(version)
    finally:
        conn.isolation_level = previous_isolation
    return applied


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # the failing statement already ended the transaction


def phx_date(now: datetime | None = None) -> str:
    """'YYYY-MM-DD' for the Phoenix calendar day at fixed UTC-7.

    A naive datetime is read as UTC — callers pass `datetime.now(timezone.utc)` or
    nothing at all; anything tz-aware is converted first.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) + PHX_UTC_OFFSET).date().isoformat()
