"""Migration 006: the SQL belt and `canonical_messenger` must agree on profile.php.

`contacts.address_canon` (generated, read by trigger G8) and `suppression.canonical_messenger`
(Python, read at the stage gate) are a pair. They were allowed to drift once: Python learned to
read `profile.php?id=<N>` while the generated column still folded every such URL onto the literal
handle `profile.php`. A drift means one belt silently stops matching, which is invisible until a
prospect is contacted after a hard no.

Also pins the backfill planner, so the one-time canonicalisation script cannot start
un-suppressing addresses.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from dm_engine import db
from dm_engine.suppression import canonical_messenger

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "data" / "migrations"

#: address -> the identity BOTH belts must agree on.
MESSENGER_CASES = [
    "https://facebook.com/profile.php?id=100093",
    "https://www.facebook.com/profile.php?id=100093&ref=bookmarks",
    "https://facebook.com/profile.php?ref=x&id=778899",
    "https://facebook.com/profile.php",              # names nobody
    "https://facebook.com/profile.php?ref=nope",     # no id -> names nobody
    "https://m.me/pinelandscape",
    "https://www.facebook.com/example?ref=bookmarks",
    "https://facebook.com/messages/t/12345",
    "https://fb.me/paverpros",
]


@pytest.fixture()
def migrated(tmp_path):
    """A fresh database with the whole migration chain applied."""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    applied = db.migrate(conn, MIGRATIONS)
    assert 6 in applied, f"migration 006 did not run: {applied}"
    required = {r["name"]: r for r in conn.execute("PRAGMA table_info(prospects)")
                if r["notnull"] and r["dflt_value"] is None and r["name"] != "id"}
    cols = ", ".join(required)
    marks = ", ".join("?" * len(required))
    conn.execute(f"INSERT INTO prospects (id, {cols}) VALUES (1, {marks})",
                 ["x"] * len(required))
    yield conn
    conn.close()


@pytest.mark.parametrize("address", MESSENGER_CASES)
def test_the_generated_column_agrees_with_canonical_messenger(migrated, address):
    migrated.execute(
        "INSERT INTO contacts (prospect_id, channel, address, address_source, created_at) "
        "VALUES (1, 'messenger', ?, 'website', '2026-09-06')", (address,))
    row = migrated.execute(
        "SELECT addr_handle, address_canon FROM contacts WHERE address = ?", (address,)
    ).fetchone()
    expected = canonical_messenger(address)
    assert row["addr_handle"] == expected, (
        f"SQL belt says {row['addr_handle']!r}, Python says {expected!r} for {address!r}")
    assert row["address_canon"] == expected, "address_canon must follow addr_handle for messenger"


def test_two_different_profiles_do_not_collapse_onto_one_identity(migrated):
    """The whole point: one hard-no must not suppress every profile.php prospect."""
    a = "https://facebook.com/profile.php?id=111111"
    b = "https://facebook.com/profile.php?id=222222"
    for i, addr in enumerate((a, b), start=1):
        migrated.execute(
            "INSERT INTO contacts (prospect_id, channel, address, address_source, created_at) "
            "VALUES (1, 'messenger', ?, 'website', '2026-09-06')", (addr,))
    canons = [r["address_canon"] for r in
              migrated.execute("SELECT address_canon FROM contacts ORDER BY id")]
    assert canons == ["111111", "222222"], canons
    assert len(set(canons)) == 2, "distinct profiles must keep distinct identities"


def test_the_backfill_planner_never_unsuppresses_an_address(migrated):
    """The one-time script may rewrite or de-duplicate, never drop a live suppression."""
    spec = importlib.util.spec_from_file_location(
        "backfill", REPO / "scripts" / "backfill_suppression_canon.py")
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    rows = [
        ("email", "Sam.Taylor+outreach@Gmail.com", "address"),   # raw -> canonical
        ("email", "clean@example.com", "address"),                # already canonical
        ("email", "example.com", "domain"),                       # domains stay as written
    ]
    for channel, address, scope in rows:
        migrated.execute(
            "INSERT INTO suppression (channel, address, scope, reason, source, created_at) "
            "VALUES (?, ?, ?, 'hard_no', 'file_sync', '2026-09-06')", (channel, address, scope))

    rewrite, collide, clean = backfill.plan(migrated)
    assert len(rewrite) == 1, [r[1] for r in rewrite]
    assert rewrite[0][1] == "samtaylor@gmail.com", rewrite[0][1]
    assert collide == []
    assert clean == 2, "the canonical row and the domain row are both left alone"
    # planning is read-only
    assert migrated.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 3
