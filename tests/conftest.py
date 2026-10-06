"""Shared test scaffolding. Contract: PLAN.md §13.

Two guarantees are mechanical here, not aspirational:

1. `DM_ENGINE_TEST=1` is set at import time — before any `dm_engine` module loads — so
   `gmail_compat.get_service()` raises TestModeError and `config.load_config()` refuses
   a database outside tmp/repo for the whole session.
2. A `sys.meta_path` blocker fails any test that imports `googleapiclient` (or the
   other Google auth SDKs). A test that reaches for the real Gmail API errors on the
   import line instead of hitting the network.

Fixtures other owners build on:
  engine_cfg     Config with every writable dir redirected into tmp_path
  engine_db      live sqlite3.Connection, freshly migrated (001-003), FK ON, WAL
  seed_graph     factory: seed_graph(conn, ...) -> dict of ids for a minimal
                 experiment/variant/batch/prospect/contact/assignment graph
  insert_message factory: insert_message(conn, ids, ...) -> message id (born COMPOSED)
"""

import dataclasses
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ["DM_ENGINE_TEST"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# --------------------------------------------------------------------------- #
# no-real-Gmail import hook
# --------------------------------------------------------------------------- #

_BLOCKED_PREFIXES = (
    "googleapiclient",
    "google_auth_oauthlib",
    "google.auth",
    "google.oauth2",
    "oauth2client",
)


class BlockedImport(ImportError):
    """A test tried to import a live Google API client."""


class _GoogleApiBlocker:
    """meta_path finder that refuses the Google SDKs instead of importing them."""

    def find_spec(self, fullname, path=None, target=None):
        for prefix in _BLOCKED_PREFIXES:
            if fullname == prefix or fullname.startswith(prefix + "."):
                raise BlockedImport(
                    f"tests must not import {fullname!r} — inject a fake service_factory "
                    "(tests/fakes.py) instead of touching the real Gmail API"
                )
        return None


def _install_import_blocker() -> None:
    already = sorted(
        name for name in sys.modules
        if any(name == p or name.startswith(p + ".") for p in _BLOCKED_PREFIXES)
    )
    if already:
        raise AssertionError(f"Google API client already imported before tests: {already}")
    if not any(isinstance(f, _GoogleApiBlocker) for f in sys.meta_path):
        sys.meta_path.insert(0, _GoogleApiBlocker())


_install_import_blocker()

from dm_engine import config, db  # noqa: E402  (must follow the env + hook setup)


# --------------------------------------------------------------------------- #
# core fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def engine_cfg(tmp_path, monkeypatch) -> config.Config:
    """Config whose every writable path lives under tmp_path.

    `migrations_dir` stays the committed repo directory (that is the code under test);
    `gmail_skill_root` stays real because it is only ever read for script paths — the
    one file the engine would WRITE there, suppression_list.txt, is redirected.
    """
    monkeypatch.setenv("DM_ENGINE_TEST", "1")
    monkeypatch.setenv("DM_ENGINE_DB", str(tmp_path / "dm.db"))
    cfg = dataclasses.replace(
        config.load_config(),
        variants_dir=tmp_path / "variants",
        review_dir=tmp_path / "review",
        outbox_dir=tmp_path / "outbox",
        locks_dir=tmp_path / "locks",
        suppression_file=tmp_path / "suppression_list.txt",
        dashboard_metrics_dir=tmp_path / "dashboard" / "metrics",
        dashboard_approvals_dir=tmp_path / "dashboard" / "approvals",
        pending_actions=tmp_path / "pending-actions.md",
    )
    config.ensure_dirs(cfg)
    cfg.dashboard_metrics_dir.mkdir(parents=True, exist_ok=True)
    cfg.dashboard_approvals_dir.mkdir(parents=True, exist_ok=True)
    (cfg.outbox_dir / "linkedin").mkdir(parents=True, exist_ok=True)
    (cfg.outbox_dir / "dry-run").mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def engine_db(engine_cfg) -> sqlite3.Connection:
    """Fresh migrated database in tmp_path. Yields an open connection."""
    conn = db.connect(engine_cfg)
    db.migrate(conn, engine_cfg.migrations_dir)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# seeding helpers (factories, so a test can seed several prospects)
# --------------------------------------------------------------------------- #

def _seed_graph(
    conn: sqlite3.Connection,
    *,
    prospect_key: str = "acmehardscapeaz.com",
    company_name: str = "Acme Hardscape",
    company_casual: str = "acme hardscape",
    domain: str | None = "acmehardscapeaz.com",
    market: str = "Boise ID",
    us_state: str = "ID",
    timezone: str = "America/Boise",
    prospect_state: str = "QUALIFIED",
    address: str = "alex@acmehardscapeaz.com",
    channel: str = "email",
    address_source: str = "website",
    verify_verdict: str | None = "PASS",
    person_key: str | None = None,
    experiment_id: str = "EXP-TEST",
    variant_id: str = "V1.1",
    batch_id: str = "B-TEST-1",
    batch_channel: str = "email",
    personalization: str = "unique",
) -> dict:
    """Insert the minimal FK graph a message needs. Idempotent for shared parents."""
    conn.execute(
        "INSERT OR IGNORE INTO experiments (id, name, hypothesis) VALUES (?, ?, ?)",
        (experiment_id, "test experiment", "test hypothesis"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (variant_id, variant_id.split(".")[0], 1, experiment_id, "test variant",
         "burned before", json.dumps({"structure": ["pain_opener", "soft_question"]})),
    )
    conn.execute(
        "INSERT OR IGNORE INTO batches (id, experiment_id, market, channel, personalization, batch_seq) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (batch_id, experiment_id, market, batch_channel, personalization, 1),
    )
    cur = conn.execute(
        "INSERT INTO prospects (prospect_key, company_name, company_casual, domain, market, "
        "us_state, timezone, owner_first_name, source, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (prospect_key, company_name, company_casual, domain, market, us_state, timezone,
         "Alex", "lead-scout", prospect_state),
    )
    prospect_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO contacts (prospect_id, person_key, channel, address, address_source, "
        "verify_verdict, verified_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (prospect_id, person_key or address, channel, address, address_source, verify_verdict),
    )
    contact_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO assignments (experiment_id, batch_id, prospect_id, variant_id, "
        "assign_hash, variant_set_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (experiment_id, batch_id, prospect_id, variant_id, "deadbeef", "cafebabe"),
    )
    assignment_id = cur.lastrowid
    conn.commit()
    return {
        "experiment_id": experiment_id,
        "variant_id": variant_id,
        "batch_id": batch_id,
        "prospect_id": prospect_id,
        "contact_id": contact_id,
        "assignment_id": assignment_id,
        "address": address,
        "channel": channel,
    }


#: A body that passes G6 (no link, no product name) — the default for seeded messages.
CLEAN_BODY = (
    "most hardscape owners I talk to already tried an answering service for the phones "
    "and it made things worse. did you end up back at voicemail too?\n\nSam"
)
PASSING_LINT = json.dumps({"ok": True, "violations": []})


def _insert_message(
    conn: sqlite3.Connection,
    ids: dict,
    *,
    sequence_step: int = 1,
    channel: str | None = None,
    subject: str | None = "the compactor doesn't take messages",
    body: str = CLEAN_BODY,
    state: str = "COMPOSED",
    lint_report: str | None = None,
) -> int:
    """Insert a message row (COMPOSED by default — G1 forbids anything else)."""
    cur = conn.execute(
        "INSERT INTO messages (batch_id, prospect_id, contact_id, assignment_id, variant_id, "
        "sequence_step, channel, subject, body, state, lint_report) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ids["batch_id"], ids["prospect_id"], ids["contact_id"], ids["assignment_id"],
         ids["variant_id"], sequence_step, channel or ids["channel"], subject, body,
         state, lint_report),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def seed_graph():
    return _seed_graph


@pytest.fixture
def insert_message():
    return _insert_message
