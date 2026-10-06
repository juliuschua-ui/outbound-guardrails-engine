"""config.py + db.py: migrations, pragmas, the Phoenix day, and the test-mode guard."""

import dataclasses
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dm_engine import config, db

from conftest import CLEAN_BODY, FIXTURES, PASSING_LINT, REPO_ROOT

SCHEMA_QUERY = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"


# --------------------------------------------------------------------------- #
# migrations
# --------------------------------------------------------------------------- #

def test_migrate_applies_every_numbered_file(engine_cfg):
    """Every numbered file in the directory, in order — derived, not hardcoded, so
    a new migration does not need this test edited to stay honest."""
    expected = [version for version, _ in db.discover_migrations(engine_cfg.migrations_dir)]
    assert expected == sorted(expected) and expected[0] == 1
    conn = db.connect(engine_cfg)
    applied = db.migrate(conn, engine_cfg.migrations_dir)
    assert applied == expected
    recorded = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert recorded == expected
    conn.close()


def test_migrate_is_idempotent(engine_db, engine_cfg):
    assert db.migrate(engine_db, engine_cfg.migrations_dir) == []


def test_migrate_creates_all_tables_views_and_triggers(engine_db):
    names = {r["name"] for r in engine_db.execute(SCHEMA_QUERY)}
    for table in ("prospects", "contacts", "suppression", "experiments", "variants",
                  "batches", "assignments", "messages", "state_log", "replies",
                  "outcomes", "stage_events", "runs", "schema_migrations"):
        assert table in names, f"missing table {table}"
    assert "sheet" in names
    for trigger in ("trg_msg_born_composed", "trg_msg_approve_path", "trg_msg_stage_path",
                    "trg_msg_sent_path", "trg_msg_ready_lint", "trg_msg1_no_pitch",
                    "trg_msg2_reply_gate", "trg_msg_suppressed", "trg_email_verify_gate",
                    "trg_statelog_no_update", "trg_statelog_no_delete",
                    "trg_one_msg1_per_prospect"):
        assert trigger in names, f"missing trigger {trigger}"


def test_incremental_migration_matches_fresh_install(engine_cfg, tmp_path):
    """A db migrated in two passes has byte-identical schema to a fresh full install."""
    fresh = db.connect(engine_cfg)
    db.migrate(fresh, engine_cfg.migrations_dir)
    fresh_schema = [tuple(r) for r in fresh.execute(SCHEMA_QUERY)]
    fresh.close()

    staged_dir = tmp_path / "staged_migrations"
    staged_dir.mkdir()
    # Discovered, not hardcoded: this test's claim is "incremental == fresh" for
    # however many migrations exist, so adding 005 must not require editing it.
    ordered = sorted(engine_cfg.migrations_dir.glob("*.sql"), key=lambda p: p.name)
    assert len(ordered) >= 2, "need at least two migrations to stage incrementally"
    shutil.copy(ordered[0], staged_dir)

    cfg2 = dataclasses.replace(engine_cfg, db_path=tmp_path / "incremental.db")
    conn = db.connect(cfg2)
    assert db.migrate(conn, staged_dir) == [1]
    for path in ordered[1:]:
        shutil.copy(path, staged_dir)
    assert db.migrate(conn, staged_dir) == list(range(2, len(ordered) + 1))
    incremental_schema = [tuple(r) for r in conn.execute(SCHEMA_QUERY)]
    conn.close()

    assert incremental_schema == fresh_schema


def test_failed_migration_rolls_back(engine_cfg, tmp_path):
    """A broken migration leaves no partial schema and no version row."""
    bad_dir = tmp_path / "bad_migrations"
    bad_dir.mkdir()
    (bad_dir / "001_init.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')));\n"
        "CREATE TABLE good_one (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE oops (id INTEGER PRIMARY KEY, x TEXT NOT NULL REFERENCES nope(id));\n"
        "CREATE TABLE this_is_not_sql;\n"
    )
    conn = db.connect(dataclasses.replace(engine_cfg, db_path=tmp_path / "bad.db"))
    with pytest.raises(sqlite3.Error):
        db.migrate(conn, bad_dir)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "good_one" not in names
    conn.close()


def test_discover_migrations_rejects_duplicate_versions(tmp_path):
    (tmp_path / "001_init.sql").write_text("SELECT 1;")
    (tmp_path / "001_other.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="duplicate migration version"):
        db.discover_migrations(tmp_path)


def test_discover_migrations_ignores_unnumbered_files(tmp_path):
    (tmp_path / "001_init.sql").write_text("SELECT 1;")
    (tmp_path / "notes.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("hi")
    assert [v for v, _ in db.discover_migrations(tmp_path)] == [1]


# --------------------------------------------------------------------------- #
# connection pragmas
# --------------------------------------------------------------------------- #

def test_connect_sets_pragmas(engine_db):
    assert engine_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert engine_db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert engine_db.row_factory is sqlite3.Row


def test_foreign_keys_are_enforced(engine_db):
    with pytest.raises(sqlite3.IntegrityError):
        engine_db.execute(
            "INSERT INTO contacts (prospect_id, channel, address, address_source) "
            "VALUES (9999, 'email', 'ghost@example.com', 'website')"
        )


# --------------------------------------------------------------------------- #
# the sheet view (SC-2: the report's four columns out of one query)
# --------------------------------------------------------------------------- #

def test_sheet_view_exposes_the_four_contract_columns(engine_db):
    cols = [d[0] for d in engine_db.execute("SELECT * FROM sheet LIMIT 0").description]
    assert cols[:4] == ["variant", "sent_to", "replied", "booked"]


def test_sheet_row_appears_after_the_full_gate_chain(engine_db, seed_graph, insert_message):
    """COMPOSED -> READY -> APPROVED -> STAGED -> SENT with every trigger armed."""
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                      (PASSING_LINT, msg_id))
    engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))
    engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
    engine_db.execute(
        "UPDATE messages SET sent_evidence = 'gmail_sent_scan:abc', "
        "sent_at = datetime('now'), state = 'SENT' WHERE id = ?", (msg_id,))
    engine_db.commit()

    rows = engine_db.execute("SELECT variant, sent_to, replied, booked FROM sheet").fetchall()
    assert len(rows) == 1
    assert rows[0]["variant"] == ids["variant_id"]
    assert rows[0]["sent_to"] == ids["address"]
    assert (rows[0]["replied"], rows[0]["booked"]) == ("n", "n")

    engine_db.execute(
        "INSERT INTO replies (prospect_id, message_id, gmail_message_id, received_at, "
        "classification, is_human, snippet, classified_by) "
        "VALUES (?, ?, 'm1', datetime('now'), 'hot', 1, 'interested', 'heuristic')",
        (ids["prospect_id"], msg_id))
    engine_db.execute(
        "INSERT INTO outcomes (prospect_id, outcome, attribution_method, attribution_confidence) "
        "VALUES (?, 'demo_booked', 'utm', 'exact')", (ids["prospect_id"],))
    engine_db.commit()
    row = engine_db.execute("SELECT replied, booked FROM sheet").fetchone()
    assert (row["replied"], row["booked"]) == ("y", "y")


def test_sheet_excludes_auto_replies_and_unsent_messages(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    assert engine_db.execute("SELECT count(*) FROM sheet").fetchone()[0] == 0, \
        "a COMPOSED message must never appear on the sheet"

    engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                      (PASSING_LINT, msg_id))
    engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))
    engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
    engine_db.execute("UPDATE messages SET sent_evidence = 'x', state = 'SENT' WHERE id = ?",
                      (msg_id,))
    engine_db.execute(
        "INSERT INTO replies (prospect_id, gmail_message_id, received_at, classification, "
        "is_human, classified_by) VALUES (?, 'auto1', datetime('now'), 'auto_reply', 0, "
        "'heuristic')", (ids["prospect_id"],))
    engine_db.commit()
    assert engine_db.execute("SELECT replied FROM sheet").fetchone()["replied"] == "n"


# --------------------------------------------------------------------------- #
# phx_date — fixed UTC-7, no tz database
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("utc_iso, expected", [
    ("2026-08-01T12:00:00+00:00", "2026-08-01"),   # midday UTC = 05:00 Phoenix
    ("2026-08-02T06:59:59+00:00", "2026-08-01"),   # 23:59:59 Phoenix — still yesterday
    ("2026-08-02T07:00:00+00:00", "2026-08-02"),   # 00:00:00 Phoenix — the day flips
    ("2026-01-15T07:00:00+00:00", "2026-01-15"),   # winter: same offset, no DST anywhere
    ("2026-07-15T07:00:00+00:00", "2026-07-15"),   # summer: AZ does not spring forward
    ("2026-03-09T06:30:00+00:00", "2026-03-08"),   # day after US DST start — still UTC-7
])
def test_phx_date_boundaries(utc_iso, expected):
    assert db.phx_date(datetime.fromisoformat(utc_iso)) == expected


def test_phx_date_treats_naive_datetimes_as_utc():
    naive = datetime(2026, 8, 2, 6, 59, 59)
    aware = datetime(2026, 8, 2, 6, 59, 59, tzinfo=timezone.utc)
    assert db.phx_date(naive) == db.phx_date(aware) == "2026-08-01"


def test_phx_date_converts_other_timezones():
    # 02:00 on 2026-08-02 at UTC+9 == 17:00 2026-08-01 UTC == 10:00 Phoenix
    tokyo = datetime(2026, 8, 2, 2, 0, tzinfo=timezone(timedelta(hours=9)))
    assert db.phx_date(tokyo) == "2026-08-01"


def test_phx_date_defaults_to_now():
    assert len(db.phx_date()) == 10 and db.phx_date()[4] == "-"


# --------------------------------------------------------------------------- #
# config guard (PLAN §10 row (n))
# --------------------------------------------------------------------------- #

def test_load_config_defaults_inside_the_repo(monkeypatch):
    monkeypatch.delenv("DM_ENGINE_DB", raising=False)
    cfg = config.load_config()
    assert cfg.root == REPO_ROOT
    assert cfg.db_path == REPO_ROOT / "data" / "dm.db"
    assert cfg.migrations_dir == REPO_ROOT / "data" / "migrations"
    assert cfg.gmail_skill_root.name == "gmail-helper"
    assert cfg.suppression_file.name == "suppression_list.txt"
    assert cfg.test_mode is True


def test_load_config_refuses_a_db_path_outside_repo_or_tmp(monkeypatch):
    monkeypatch.setenv("DM_ENGINE_DB", str(Path.home() / "real-dm.db"))
    with pytest.raises(config.ConfigError, match="refusing db_path"):
        config.load_config()


def test_load_config_refuses_the_dashboard_database(monkeypatch):
    monkeypatch.setenv("DM_ENGINE_DB", str(Path.home() / "work/dashboard/data/dashboard.db"))
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_load_config_accepts_a_tmp_db_in_test_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DM_ENGINE_DB", str(tmp_path / "dm.db"))
    assert config.load_config().db_path == (tmp_path / "dm.db").resolve()


def test_load_config_guard_holds_outside_test_mode(tmp_path, monkeypatch):
    """Without DM_ENGINE_TEST=1 even a tmp path is refused — prod db stays in the repo."""
    monkeypatch.setenv("DM_ENGINE_TEST", "0")
    monkeypatch.setenv("DM_ENGINE_DB", str(tmp_path / "dm.db"))
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_load_config_creates_engine_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("DM_ENGINE_DB", str(tmp_path / "nested" / "dm.db"))
    cfg = config.load_config()
    assert cfg.db_path.parent.is_dir()
    for path in (cfg.review_dir, cfg.outbox_dir, cfg.locks_dir, cfg.variants_dir):
        assert path.is_dir()


def test_engine_cfg_fixture_keeps_writes_inside_tmp(engine_cfg, tmp_path):
    """Every path another owner's test might write to is redirected under tmp_path."""
    for path in (engine_cfg.db_path, engine_cfg.review_dir, engine_cfg.outbox_dir,
                 engine_cfg.locks_dir, engine_cfg.variants_dir, engine_cfg.suppression_file,
                 engine_cfg.dashboard_metrics_dir, engine_cfg.dashboard_approvals_dir,
                 engine_cfg.pending_actions):
        assert path.is_relative_to(tmp_path), f"{path} escapes tmp_path"


def test_googleapiclient_import_is_blocked():
    with pytest.raises(ImportError):
        __import__("googleapiclient.discovery")


# --------------------------------------------------------------------------- #
# prospect import fixtures (contract §12.7 — Owner B consumes these)
# --------------------------------------------------------------------------- #

def _load(name):
    return json.loads((FIXTURES / "prospects" / name).read_text())


#: SPEC §6 — the address arrays a prospect file may carry, one per staging channel.
CONTRACT_ADDRESS_ARRAYS = ("emails", "linkedin", "whatsapp", "messenger")
VALID_SOURCES = ("website", "reply", "linkedin", "directory", "apollo", "manual",
                 "format_guess")


def test_prospect_fixtures_match_the_import_contract():
    records = _load("one.json") + _load("mixed.json")
    assert isinstance(records, list)
    for rec in records:
        assert rec["company_name"] and rec["us_state"] and rec["source"]
        for array in CONTRACT_ADDRESS_ARRAYS:
            assert isinstance(rec.get(array, []), list)
            for entry in rec.get(array, []):
                # every array takes the same {"address": ..., "source": ...} shape
                assert isinstance(entry, (dict, str))
                if isinstance(entry, dict):
                    assert entry["address"]
                    assert entry.get("source", "website") in VALID_SOURCES
        for email in rec.get("emails", []):
            assert "@" in email["address"]
        for li in rec.get("linkedin", []):
            assert li["address"].startswith("https://linkedin.com/")
        for page in rec.get("messenger", []):
            assert any(host in page["address"]
                       for host in ("m.me/", "facebook.com/", "fb.me/"))
    assert len(_load("one.json")) == 1


def test_prospect_fixtures_cover_both_dm_channels():
    """Owner B's fixtures are the contract Owner D stages from — a DM channel with
    no fixture is a channel nothing downstream is ever exercised against."""
    records = _load("one.json") + _load("mixed.json")
    for array in ("whatsapp", "messenger"):
        assert any(rec.get(array) for rec in records), f"no fixture carries {array}"
    # ...and one number the import must refuse rather than silently drop
    numbers = [entry["address"] for rec in records for entry in rec.get("whatsapp", [])]
    assert any(not any(ch.isdigit() for ch in n) for n in numbers), \
        "no unusable-number case in the fixtures"


def test_prospect_fixtures_cover_the_required_edge_cases():
    mixed = _load("mixed.json")
    domains = [r.get("website_url", "") for r in mixed]
    assert sum("acmehardscapeaz.com" in d for d in domains) == 2, "duplicate-domain pair"
    assert any("owner_first_name" not in r for r in mixed), "missing owner name"
    assert any(r["us_state"] not in ("AZ",) for r in mixed), "non-Arizona (national) prospect"
    assert any("website_url" not in r for r in mixed), "prospect with no website"

    malformed = _load("malformed.json")
    assert any(not isinstance(r, dict) for r in malformed)
    assert any(isinstance(r, dict) and "company_name" not in r for r in malformed)
    assert any(isinstance(r, dict) and "us_state" not in r for r in malformed)
    assert any(isinstance(r, dict) and r.get("us_state") == "AB" for r in malformed)

    with pytest.raises(json.JSONDecodeError):
        _load("broken_not_json.json")


def test_clean_seed_body_would_pass_the_message_1_content_gate():
    lowered = CLEAN_BODY.lower()
    for banned in ("http://", "https://", "www.", "brightflowcure", "bdai", "bright desk ai"):
        assert banned not in lowered
