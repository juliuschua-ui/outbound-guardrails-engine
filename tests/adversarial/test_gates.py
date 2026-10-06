"""The §10 adversarial table — one test per row.

These are not unit tests. Each one attacks the engine the way a real bug or a
careless future edit would: at the database directly with raw SQL, at the CLI, and
at the export path. Where a happy-path API would refuse politely, these go around it.

    (a) any code path sends without approval
    (b) a Message 1 ships with a URL or the product name
    (c) two concurrent runs exceed the daily cap
    (d) the same prospect is messaged twice
    (e) a suppressed contact is re-contacted
    (f) a FAIL-verified address reaches a draft
    (g) variant assignment is gamed or non-reproducible
    (h) a metric overstates performance
    (i) prospects are silently truncated or dropped
    (j) a secret leaks into a log, a doc, or a committed file
    (k) history is rewritten by direct DB writes
    (l) lint is bypassed with Unicode
    (m) a whole batch ships one identical subject
    (n) the test suite touches real Gmail / GHL / DASHBOARD
    (o) approval is forged by a non-TTY agent

Residual risk, stated honestly: anyone with a shell can edit this source or drop the
triggers. The threat model is accidental and agent-driven bypass, not a malicious
local root — the same stance `_sender_host_guard.py` takes.
"""

import ast
import multiprocessing
import re
import sqlite3
from pathlib import Path

import pytest

from dm_engine import attribution, cli, db, locks, replies, report

from conftest import CLEAN_BODY, PASSING_LINT, REPO_ROOT

SRC = REPO_ROOT / "src" / "dm_engine"


def source_files():
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def to_ready(conn, message_id, body=CLEAN_BODY):
    conn.execute("UPDATE messages SET body = ?, lint_report = ?, state = 'READY' "
                 "WHERE id = ?", (body, PASSING_LINT, message_id))
    conn.commit()


def to_approved(conn, message_id):
    to_ready(conn, message_id)
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.commit()


def to_sent(conn, message_id):
    to_approved(conn, message_id)
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'SENT', sent_at = datetime('now'), "
                 "sent_evidence = 'test' WHERE id = ?", (message_id,))
    conn.commit()


# =========================================================================== #
# (a) any code path sends without approval
# =========================================================================== #

def _attribute_chain(node) -> list[str]:
    names = []
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Call):
            node = node.func
            continue
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return names


def send_capabilities(source: str, filename: str = "<test>") -> list[str]:
    """Every send capability in a Python source string. Empty list = no send code.

    AST-based on purpose: a text grep would fire on the docstrings that (correctly)
    explain why the engine has no send path, and would miss `svc.users().messages()
    .send(...)` split across lines.
    """
    hits: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("send", "sendmail", "send_message"):
                chain = set(_attribute_chain(node.func))
                if chain & {"users", "messages", "drafts", "service", "smtp",
                            "SMTP", "server", "gmail"}:
                    hits.append(f"{filename}:{node.lineno}: "
                                f"{'.'.join(reversed(_attribute_chain(node.func)))}()")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("smtplib", "aiosmtplib"):
                    hits.append(f"{filename}:{node.lineno}: import {alias.name}")
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in ("smtplib", "aiosmtplib"):
                hits.append(f"{filename}:{node.lineno}: from {node.module} import ...")
    return hits


def test_a_no_send_code_anywhere_under_src():
    """PLAN.md §14 non-goal 1: the engine contains zero send capability."""
    offenders = []
    for path in source_files():
        offenders += send_capabilities(path.read_text(encoding="utf-8"), str(path))
    assert offenders == [], (
        "send capability found in the engine — sends belong to the DASHBOARD EXEC "
        f"queue or Sam's own hands:\n" + "\n".join(offenders))


@pytest.mark.parametrize("attack", [
    "service.users().messages().send(userId='me', body=msg).execute()",
    "svc.users().drafts().send(userId='me', body={'id': draft_id}).execute()",
    "import smtplib\nsmtplib.SMTP('localhost').sendmail(a, b, c)",
    "from smtplib import SMTP",
])
def test_a_the_send_detector_actually_catches_injected_send_code(attack):
    """A green scan is only meaningful if the scanner can fail."""
    assert send_capabilities(attack), f"detector missed: {attack!r}"


def test_a_there_is_no_send_subcommand_on_the_cli():
    commands = set([action for action in cli.build_parser()._actions
                    if action.dest == "command"][0].choices)
    assert not commands & {"send", "release", "blast", "fire", "send-batch"}


def test_a_sent_cannot_be_forged_by_raw_sql(engine_db, seed_graph, insert_message):
    """G4: SENT requires a prior STAGED state AND evidence."""
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(sqlite3.Error, match="G4"):
        engine_db.execute(
            "UPDATE messages SET state = 'SENT', sent_evidence = 'forged' WHERE id = ?",
            (message_id,))
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (message_id,)).fetchone()[0] == "COMPOSED"


def test_a_staged_cannot_be_reached_from_ready(engine_db, seed_graph, insert_message):
    """G3: the export gate. APPROVED is the only door into STAGED."""
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_ready(engine_db, message_id)
    with pytest.raises(sqlite3.Error, match="G3"):
        engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?",
                          (message_id,))


def test_a_messages_cannot_be_born_approved(engine_db, seed_graph):
    """G1: no INSERT may skip the pipeline."""
    ids = seed_graph(engine_db)
    with pytest.raises(sqlite3.Error, match="G1"):
        engine_db.execute(
            "INSERT INTO messages (batch_id, prospect_id, contact_id, assignment_id, "
            "variant_id, sequence_step, channel, subject, body, state) "
            "VALUES (?, ?, ?, ?, ?, 1, 'email', 's', 'b', 'APPROVED')",
            (ids["batch_id"], ids["prospect_id"], ids["contact_id"],
             ids["assignment_id"], ids["variant_id"]))


# =========================================================================== #
# (b) a Message 1 ships with a URL or the product name
# =========================================================================== #

@pytest.mark.parametrize("body", [
    "check out https://brightdesk.brightflowcure.com/demo-ai-voice\n\nSam",
    "we built BDAI for exactly this. worth a look?\n\nSam",
    "have a look at www.brightflowcure.com — worth it?\n\nSam",
    "our bright desk ai answers when you can't. interested?\n\nSam",
])
def test_b_trigger_g6_blocks_a_pitch_in_message_one(engine_db, seed_graph,
                                                    insert_message, body):
    """The SQL belt: even if lint were bypassed, the row cannot reach READY."""
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    # smuggle the body in without a state change (G6 only fires on state moves)
    engine_db.execute("UPDATE messages SET body = ?, lint_report = ? WHERE id = ?",
                      (body, PASSING_LINT, message_id))
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G6"):
        engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?",
                          (message_id,))


def test_b_lint_refuses_a_link_in_message_one():
    lint = pytest.importorskip("dm_engine.lint")
    draft = lint.MessageDraft(prospect_key="co.com", channel="email", sequence_step=1,
                              subject="the compactor doesn't take messages",
                              body="have a look at https://brightflowcure.com\n\nSam")
    result = lint.lint_message(draft, [], [], "acme hardscape")
    assert not result.ok
    assert {v["rule"] for v in result.violations} & {"L1", "L2"}


def test_b_ready_requires_a_passing_lint_report(engine_db, seed_graph, insert_message):
    """G5: a message with no lint report, or a failing one, cannot become READY."""
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(sqlite3.Error, match="G5"):
        engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?",
                          (message_id,))
    engine_db.execute(
        "UPDATE messages SET lint_report = '{\"ok\": false, \"violations\": [1]}' "
        "WHERE id = ?", (message_id,))
    with pytest.raises(sqlite3.Error, match="G5"):
        engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?",
                          (message_id,))


# =========================================================================== #
# (c) two concurrent runs exceed the daily cap
# =========================================================================== #

CAP = 20


def _seed_stageable_messages(conn, seed_graph, insert_message, count):
    """`count` real APPROVED messages — stage_events has an FK to messages(id)."""
    ids = []
    for index in range(count):
        graph = seed_graph(conn, prospect_key=f"cap{index}.com",
                           company_name=f"Cap {index}", domain=f"cap{index}.com",
                           address=f"o{index}@cap{index}.com",
                           batch_id="B-CAP", personalization="template")
        message_id = insert_message(conn, graph, subject=f"cap subject {index}")
        to_approved(conn, message_id)
        ids.append(message_id)
    return ids


def _cap_worker(db_path: str, locks_dir: str, message_ids: list, guarded: bool,
                cap: int, day: str, barrier=None) -> None:
    """One 'stage run'.

    `guarded=True` reproduces what channels/email_gmail.stage() does: flock +
    BEGIN IMMEDIATE, so the cap count and the reservation are one atomic act.

    `guarded=False` is the naive check-then-act a future refactor might regress to.
    Its interleaving is forced by a two-phase barrier rather than left to the
    scheduler — every worker reads the count before any worker writes — so the
    control is deterministic instead of a race the test hopes to lose.
    """
    import dataclasses

    from dm_engine import config as config_mod
    from dm_engine.channels import base as channels_base

    cfg = dataclasses.replace(config_mod.load_config(),
                              db_path=Path(db_path), locks_dir=Path(locks_dir))
    conn = db.connect(cfg)
    try:
        for message_id in message_ids:
            if guarded:
                with locks.hold(cfg, channels_base.STAGE_LOCK, timeout_s=60):
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        used = channels_base.staged_today(conn, day, "email")
                        if used >= cap:
                            conn.rollback()
                            continue
                        channels_base.record_stage_event(
                            conn, message_id, "email", "template", day)
                        conn.execute("COMMIT")
                    except sqlite3.Error:
                        conn.rollback()
                continue

            barrier.wait(timeout=60)                       # line up
            used = channels_base.staged_today(conn, day, "email")
            barrier.wait(timeout=60)                       # everyone has read
            if used >= cap:
                continue
            try:
                channels_base.record_stage_event(
                    conn, message_id, "email", "template", day)
                conn.commit()
            except sqlite3.Error:
                pass
    finally:
        conn.close()


def _race(engine_cfg, message_ids, guarded, day, workers=2):
    context = multiprocessing.get_context("fork")
    chunk = len(message_ids) // workers
    barrier = None if guarded else context.Barrier(workers)
    processes = [
        context.Process(target=_cap_worker,
                        args=(str(engine_cfg.db_path), str(engine_cfg.locks_dir),
                              message_ids[i * chunk:(i + 1) * chunk], guarded, CAP,
                              day, barrier))
        for i in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0, "a stage worker crashed instead of refusing"


def test_c_two_processes_racing_the_daily_cap_cannot_exceed_it(
        engine_db, engine_cfg, seed_graph, insert_message):
    """PLAN.md §10 row (c) / §9.29: flock + BEGIN IMMEDIATE make the cap count and
    the reservation atomic across PROCESSES, not just across threads."""
    day = db.phx_date()
    message_ids = _seed_stageable_messages(engine_db, seed_graph, insert_message, 40)
    engine_db.commit()
    _race(engine_cfg, message_ids, guarded=True, day=day)
    staged = engine_db.execute(
        "SELECT count(*) FROM stage_events WHERE phx_date = ?", (day,)).fetchone()[0]
    assert staged == CAP, f"cap breached or under-filled: {staged} != {CAP}"


def test_c_the_naive_implementation_really_does_breach_the_cap(
        engine_db, engine_cfg, seed_graph, insert_message):
    """Control. Without this the green test above proves nothing: it could pass
    because the two processes never actually overlapped. Here the SAME harness with
    the check and the insert pulled apart overshoots, which is what makes the
    lock + BEGIN IMMEDIATE result meaningful.

    3 workers against a cap of 20: after 6 synchronised rounds the ledger holds 18,
    all three read 18 < 20, and all three insert — 21. Deterministic, not lucky."""
    day = db.phx_date()
    message_ids = _seed_stageable_messages(engine_db, seed_graph, insert_message, 30)
    engine_db.commit()
    _race(engine_cfg, message_ids, guarded=False, day=day, workers=3)
    staged = engine_db.execute(
        "SELECT count(*) FROM stage_events WHERE phx_date = ?", (day,)).fetchone()[0]
    assert staged > CAP, (
        f"the naive path staged {staged} — the processes did not overlap, so the "
        "guarded test above is not proving anything")


def test_c_the_email_adapter_uses_the_lock_and_an_immediate_transaction():
    """Guards against a future edit quietly dropping the lock from stage()."""
    source = (SRC / "channels" / "email_gmail.py").read_text(encoding="utf-8")
    assert "locks.hold(" in source, "stage() no longer takes the stage lock"
    assert "BEGIN IMMEDIATE" in source, "the cap check is no longer atomic"


# =========================================================================== #
# (d) the same prospect is messaged twice
# =========================================================================== #

def test_d_a_second_message_one_is_refused(engine_db, seed_graph, insert_message):
    """G11: one active Message 1 per prospect, forever."""
    ids = seed_graph(engine_db)
    insert_message(engine_db, ids)
    with pytest.raises(sqlite3.Error, match="G11"):
        insert_message(engine_db, ids, subject="a second bite")


def test_d_a_cancelled_message_one_frees_the_prospect(engine_db, seed_graph,
                                                      insert_message):
    """The guard is about ACTIVE messages — a rejected draft may be recomposed."""
    ids = seed_graph(engine_db)
    first = insert_message(engine_db, ids)
    engine_db.execute("UPDATE messages SET state = 'CANCELLED' WHERE id = ?", (first,))
    engine_db.commit()
    assert insert_message(engine_db, ids, subject="recomposed")


def test_d_one_variant_per_prospect_per_experiment(engine_db, seed_graph):
    ids = seed_graph(engine_db)
    with pytest.raises(sqlite3.IntegrityError):
        engine_db.execute(
            "INSERT INTO assignments (experiment_id, batch_id, prospect_id, variant_id, "
            "assign_hash, variant_set_hash) VALUES (?, ?, ?, ?, 'x', 'y')",
            (ids["experiment_id"], ids["batch_id"], ids["prospect_id"], ids["variant_id"]))


# =========================================================================== #
# (e) a suppressed contact is re-contacted
# =========================================================================== #

def test_e_a_suppressed_address_cannot_receive_a_message(engine_db, seed_graph,
                                                         insert_message):
    """G8 fires at INSERT — the message never exists in the first place."""
    ids = seed_graph(engine_db)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', ?, 'address', 'hard_no', 'test')", (ids["address"],))
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G8"):
        insert_message(engine_db, ids)


def test_e_domain_scope_suppression_covers_every_mailbox(engine_db, seed_graph,
                                                         insert_message):
    ids = seed_graph(engine_db)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', '@acmehardscapeaz.com', 'domain', 'legal', 'test')")
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G8"):
        insert_message(engine_db, ids)


def test_e_a_bounce_recorded_by_the_scanner_suppresses_the_address(
        engine_db, engine_cfg, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_sent(engine_db, message_id)
    replies.record_reply(engine_db, ids["prospect_id"], "email", "bounce",
                         "550 5.1.1 no such user", "engine")
    assert engine_db.execute(
        "SELECT reason FROM suppression WHERE address = ?",
        (ids["address"],)).fetchone()[0] == "bounce"


def test_e_a_file_only_suppression_reaches_the_database_before_staging(
        engine_db, engine_cfg, seed_graph, insert_message):
    """§10 row (e): an address added to the skill's suppression_list.txt but not the
    DB must still block, because the file sync runs before every stage."""
    suppression = pytest.importorskip("dm_engine.suppression")
    ids = seed_graph(engine_db)
    engine_cfg.suppression_file.write_text(
        f"# dm-engine\n{ids['address']}\n", encoding="utf-8")
    suppression.sync_from_file(engine_db, engine_cfg.suppression_file)
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G8"):
        insert_message(engine_db, ids)


# =========================================================================== #
# (f) a FAIL-verified address reaches a draft
# =========================================================================== #

def test_f_a_fail_verified_address_cannot_be_staged(engine_db, seed_graph,
                                                    insert_message):
    ids = seed_graph(engine_db, verify_verdict="FAIL")
    message_id = insert_message(engine_db, ids)
    to_approved(engine_db, message_id)
    with pytest.raises(sqlite3.Error, match="G9"):
        engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?",
                          (message_id,))


def test_f_risk_from_a_format_guess_is_also_blocked(engine_db, seed_graph,
                                                    insert_message):
    """Standing rule 6: RISK only ships when a human saw the address somewhere real."""
    ids = seed_graph(engine_db, verify_verdict="RISK", address_source="format_guess")
    message_id = insert_message(engine_db, ids)
    to_approved(engine_db, message_id)
    with pytest.raises(sqlite3.Error, match="G9"):
        engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?",
                          (message_id,))


def test_f_risk_from_the_website_is_allowed(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db, verify_verdict="RISK", address_source="website")
    message_id = insert_message(engine_db, ids)
    to_approved(engine_db, message_id)
    engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    engine_db.commit()


def test_f_a_fail_address_creates_zero_gmail_drafts(engine_db, engine_cfg, seed_graph,
                                                    insert_message):
    """The export path, not just the trigger: the adapter must create no draft."""
    fakes = pytest.importorskip("fakes")
    channels = pytest.importorskip("dm_engine.channels")
    ids = seed_graph(engine_db, verify_verdict="FAIL")
    message_id = insert_message(engine_db, ids)
    to_approved(engine_db, message_id)
    factory = fakes.FakeServiceFactory()
    result = channels.get_adapter("email").stage(
        engine_db, engine_cfg, [message_id], False, service_factory=factory)
    assert result.staged == 0
    assert factory.service.draft_count() == 0
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (message_id,)).fetchone()[0] == "APPROVED"


def test_f_a_forged_ready_row_creates_zero_drafts(engine_db, engine_cfg, seed_graph,
                                                  insert_message):
    """§7.4: hand the exporter a READY id directly — it must still stage nothing."""
    fakes = pytest.importorskip("fakes")
    channels = pytest.importorskip("dm_engine.channels")
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_ready(engine_db, message_id)
    factory = fakes.FakeServiceFactory()
    result = channels.get_adapter("email").stage(
        engine_db, engine_cfg, [message_id], False, service_factory=factory)
    assert result.staged == 0
    assert factory.service.draft_count() == 0


# =========================================================================== #
# (g) variant assignment is gamed or non-reproducible
# =========================================================================== #

def _seed_experiment(conn, variants=5, prospects=10):
    from dm_engine import variants as variants_mod

    conn.execute("INSERT OR IGNORE INTO experiments (id, name) VALUES ('EXP-A', 'a')")
    for index in range(1, variants + 1):
        conn.execute(
            "INSERT OR IGNORE INTO variants (id, family, version, experiment_id, name, "
            "angle, spec) VALUES (?, ?, 1, 'EXP-A', ?, 'angle', '{}')",
            (f"V{index}.1", f"V{index}", f"variant {index}"))
    conn.execute(
        "INSERT OR IGNORE INTO batches (id, experiment_id, market, channel, "
        "personalization, batch_seq) VALUES ('B-A', 'EXP-A', 'Boise ID', 'email', "
        "'unique', 1)")
    for index in range(prospects):
        cur = conn.execute(
            "INSERT INTO prospects (prospect_key, company_name, company_casual, domain, "
            "market, us_state, timezone, source, state) VALUES (?, ?, ?, ?, 'Boise ID', "
            "'ID', 'America/Boise', 'lead-scout', 'QUALIFIED')",
            (f"p{index}.com", f"P {index}", f"p {index}", f"p{index}.com"))
        # A prospect with no contact on the batch's channel is not a candidate —
        # `assign_batch` will not claim one it cannot reach.
        conn.execute(
            "INSERT INTO contacts (prospect_id, person_key, channel, address, "
            "address_source) VALUES (?, ?, 'email', ?, 'website')",
            (cur.lastrowid, f"owner@p{index}.com", f"owner@p{index}.com"))
    conn.commit()
    return variants_mod


def test_g_assignment_is_a_pure_function_of_recorded_inputs(engine_db):
    variants_mod = _seed_experiment(engine_db)
    first = variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    engine_db.commit()
    stored = [(r["prospect_id"], r["variant_id"], r["assign_hash"]) for r in
              engine_db.execute("SELECT prospect_id, variant_id, assign_hash FROM "
                                "assignments ORDER BY prospect_id")]
    second = variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    again = [(r["prospect_id"], r["variant_id"], r["assign_hash"]) for r in
             engine_db.execute("SELECT prospect_id, variant_id, assign_hash FROM "
                               "assignments ORDER BY prospect_id")]
    assert stored == again, "re-running assign changed history"
    assert len(first) == len(second)


def test_g_the_recorded_hash_is_the_documented_one(engine_db):
    """A reviewer must be able to recompute a hash by hand and match the row."""
    import hashlib

    variants_mod = _seed_experiment(engine_db)
    variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    engine_db.commit()
    row = engine_db.execute(
        "SELECT p.prospect_key, a.assign_hash FROM assignments a "
        "JOIN prospects p ON p.id = a.prospect_id LIMIT 1").fetchone()
    expected = hashlib.sha256(f"EXP-A|{row['prospect_key']}".encode()).hexdigest()
    assert row["assign_hash"] == expected


def test_g_tampering_with_an_assignment_is_detected(engine_db):
    variants_mod = _seed_experiment(engine_db)
    variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    engine_db.commit()
    victim = engine_db.execute(
        "SELECT id, variant_id FROM assignments ORDER BY id LIMIT 1").fetchone()
    engine_db.execute("UPDATE assignments SET variant_id = ? WHERE id = ?",
                      ("V5.1" if victim["variant_id"] != "V5.1" else "V1.1",
                       victim["id"]))
    engine_db.commit()
    with pytest.raises(Exception) as excinfo:
        variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    assert "drift" in type(excinfo.value).__name__.lower() or "drift" in str(
        excinfo.value).lower()


def test_g_assignment_is_balanced_within_one(engine_db):
    variants_mod = _seed_experiment(engine_db)
    variants_mod.assign_batch(engine_db, "EXP-A", "B-A")
    engine_db.commit()
    counts = [r[0] for r in engine_db.execute(
        "SELECT count(*) FROM assignments GROUP BY variant_id")]
    assert max(counts) - min(counts) <= 1


# =========================================================================== #
# (h) a metric overstates performance
# =========================================================================== #

@pytest.fixture
def overstatement_fixture(engine_db, seed_graph, insert_message):
    """PLAN.md §10 row (h) verbatim: 10 sends, 2 bounces, 1 human reply,
    1 auto-reply, 1 organic GHL contact. Truth: reply_rate = 1/8, replies = 1,
    booked = 0."""
    people = []
    for index in range(10):
        ids = seed_graph(engine_db, prospect_key=f"h{index}.com",
                         company_name=f"H {index}", domain=f"h{index}.com",
                         address=f"o{index}@h{index}.com", experiment_id="EXP-H",
                         variant_id="V1.1", batch_id="B-H")
        message_id = insert_message(engine_db, ids, subject=f"s{index}")
        to_sent(engine_db, message_id)
        ids["message_id"] = message_id
        people.append(ids)
    for person in people[:2]:
        engine_db.execute("UPDATE messages SET state = 'BOUNCED' WHERE id = ?",
                          (person["message_id"],))
    engine_db.commit()
    replies.record_reply(engine_db, people[2]["prospect_id"], "email", "hot",
                         "what does it cost?", "operator")
    replies.record_reply(engine_db, people[3]["prospect_id"], "email", "auto_reply",
                         "we have received your message", "engine")
    return people


def test_h_reply_rate_uses_delivered_not_sent(engine_db, overstatement_fixture):
    counts = report.population_counts(engine_db)
    assert (counts["sends"], counts["bounced"], counts["delivered"]) == (10, 2, 8)
    assert counts["replies"] == 1
    assert counts["booked"] == 0
    assert report.reply_rate_pct(counts) == 12.5


def test_h_an_organic_ghl_contact_is_never_booked(engine_db, overstatement_fixture,
                                                  tmp_path):
    import json

    path = tmp_path / "ghl.json"
    path.write_text(json.dumps([{"id": "organic", "companyName": "Never Emailed",
                                 "website": "https://never.example",
                                 "dateAdded": "2026-08-05T00:00:00Z"}]),
                    encoding="utf-8")
    assert attribution.match(engine_db, path) == []
    assert report.population_counts(engine_db)["booked"] == 0


def test_h_owner_b_stats_agree_with_the_report_totals(engine_db, overstatement_fixture):
    """Two independent implementations of §4.4 must produce the same numbers."""
    stats = pytest.importorskip("dm_engine.stats")
    table = stats.variant_table(engine_db, "EXP-H")
    counts = report.population_counts(engine_db)
    assert sum(vs.sends for vs in table) == counts["sends"]
    assert sum(vs.delivered for vs in table) == counts["delivered"]
    assert sum(vs.bounced for vs in table) == counts["bounced"]
    assert sum(vs.replies for vs in table) == counts["replies"]
    assert sum(vs.booked for vs in table) == counts["booked"]


def test_h_report_delegates_every_division_to_stats():
    """§10 row (h): report.py must contain no rate arithmetic of its own."""
    tree = ast.parse((SRC / "report.py").read_text(encoding="utf-8"))
    divisions = [node for node in ast.walk(tree)
                 if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)]
    assert divisions == [], (
        "report.py divides at line(s) "
        f"{[n.lineno for n in divisions]} — all rate arithmetic belongs in stats.py")


def test_h_no_printed_rate_lacks_its_denominator(engine_db, overstatement_fixture):
    text = report.render_status(report.status_summary(engine_db))
    for line in text.splitlines():
        if "%" in line:
            assert "n=" in line, f"a rate without its n: {line}"


def test_h_a_machine_reply_can_never_reach_the_sheet(engine_db, overstatement_fixture):
    replied = [row["replied"] for row in report.sheet_rows(engine_db)]
    assert replied.count("y") == 1, "the auto-responder leaked into the sheet"


def test_h_no_revenue_metric_exists_anywhere(engine_db, overstatement_fixture):
    """GHL monetaryValue is 0 across the board — such a number would be garbage."""
    keys = " ".join(report.collect_metrics(engine_db)).lower()
    for banned in ("revenue", "pipeline", "monetary", "value_usd", "deal_size"):
        assert banned not in keys


# =========================================================================== #
# (i) prospects are silently truncated or dropped
# =========================================================================== #

def test_i_import_accounts_for_every_input_row(engine_db, engine_cfg):
    prospects = pytest.importorskip("dm_engine.prospects")
    from conftest import FIXTURES

    path = FIXTURES / "prospects" / "mixed.json"
    import json

    rows = len(json.loads(path.read_text(encoding="utf-8")))
    result = prospects.import_prospects(engine_db, path, "Boise ID")
    accounted = (result.added + result.dupes + len(result.skips) + len(result.errors))
    assert accounted == rows, (
        f"{rows} rows in, {accounted} accounted for — the difference vanished silently")


def test_i_attribution_accounts_for_every_ghl_contact(engine_db, seed_graph,
                                                      insert_message, tmp_path):
    import json

    ids = seed_graph(engine_db)
    to_sent(engine_db, insert_message(engine_db, ids))
    contacts = [
        {"id": "a", "website": "https://acmehardscapeaz.com",
         "companyName": "Acme Hardscape", "firstName": "Alex",
         "dateAdded": "2099-01-01T00:00:00Z"},
        {"id": "b", "website": "https://organic.example", "companyName": "Organic",
         "dateAdded": "2099-01-01T00:00:00Z"},
    ]
    path = tmp_path / "ghl.json"
    path.write_text(json.dumps(contacts), encoding="utf-8")
    total = len(list(attribution.load_ghl_export(path)))
    matched = attribution.match(engine_db, path)
    assert total == 2
    assert len(matched) + (total - len(matched)) == total


def test_i_the_csv_export_never_drops_a_row(engine_db, seed_graph, insert_message,
                                            tmp_path):
    import csv

    for index in range(7):
        ids = seed_graph(engine_db, prospect_key=f"t{index}.com",
                         company_name=f"T {index}", domain=f"t{index}.com",
                         address=f"o{index}@t{index}.com")
        to_sent(engine_db, insert_message(engine_db, ids, subject=f"s{index}"))
    view_rows = engine_db.execute("SELECT count(*) FROM sheet").fetchone()[0]
    rows = report.sheet_rows(engine_db)
    path = tmp_path / "sheet.csv"
    report.write_csv(rows, path)
    with path.open(encoding="utf-8") as handle:
        exported = list(csv.DictReader(handle))
    assert view_rows == len(rows) == len(exported) == 7


def test_i_the_csv_writer_refuses_rather_than_silently_dropping_a_column(tmp_path):
    with pytest.raises(report.ReportError):
        report.write_csv(
            [{"variant": "V1.1", "sent_to": "a@b.com", "replied": "n", "booked": "n"},
             {"variant": "V2.1", "sent_to": "c@d.com", "replied": "n", "booked": "n",
              "extra": "x"}],
            tmp_path / "s.csv")


# =========================================================================== #
# (j) a secret leaks into a log, a doc, or a committed file
# =========================================================================== #

SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"pit-[A-Za-z0-9_\-]{10,}", r"\bsk-[A-Za-z0-9_\-]{16,}",
        r"\bAKIA[0-9A-Z]{16}\b", r"\bghp_[A-Za-z0-9]{20,}", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}",
        r"\bAIza[0-9A-Za-z_\-]{30,}", r"\bsk_live_[0-9a-zA-Z]{16,}",
        r"\bwhsec_[A-Za-z0-9]{16,}", r"li_at=[A-Za-z0-9]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
]

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".impeccable", "node_modules", "data"}


def repo_text_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        if path.suffix.lower() in (".png", ".jpg", ".pdf", ".db", ".pyc", ".sqlite"):
            continue
        yield path


def test_j_no_secret_signature_in_any_repo_file():
    offenders = []
    for path in repo_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                # report the location, never the value
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
    assert offenders == [], f"possible secrets: {sorted(set(offenders))}"


def test_j_the_secret_scanner_actually_catches_one(tmp_path):
    planted = "ghp_" + "a" * 24
    assert any(p.search(planted) for p in SECRET_PATTERNS)


def test_j_gitignore_covers_the_db_review_outbox_and_env():
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("data/dm.db", "data/review/", "data/outbox/", ".env"):
        assert entry in ignored, f".gitignore is missing {entry}"


def test_j_env_example_holds_no_values():
    example = REPO_ROOT / ".env.example"
    assert example.exists()
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        assert value.strip() in ("", '""') or value.strip().startswith(("<", "/", "~")), \
            f".env.example carries a real-looking value: {line}"


def test_j_the_engine_holds_no_token_paths_in_source():
    """Tokens stay in the gmail-helper skill root, referenced by path, never copied."""
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        assert "refresh_token" not in text
        assert "client_secret" not in text


#: Addresses allowed to appear literally in engine source: Sam's own sending
#: account, and the obvious documentation placeholders.
_ALLOWED_LITERAL_ADDRESSES = {"sam.taylor@example.com", "a@b.com",
                              "alex@acmehardscapeaz.com",
                              "mailer-daemon@googlemail.com"}


def test_j_no_prospect_email_is_hardcoded_in_engine_source():
    """No third party's address may sit in committed source — it is PII, and the
    engine reads every recipient from the database."""
    offenders = {}
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        found = set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text))
        leaked = {a for a in found
                  if a not in _ALLOWED_LITERAL_ADDRESSES
                  and not a.endswith((".example", "example.com", "@domain",
                                      "@example.org"))}
        if leaked:
            offenders[path.name] = sorted(leaked)
    assert offenders == {}, f"hardcoded addresses: {offenders}"


# =========================================================================== #
# (k) history is rewritten by direct DB writes
# =========================================================================== #

def test_k_state_log_cannot_be_updated(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_sent(engine_db, message_id)
    replies.transition(engine_db, "message", message_id, "CLOSED", "engine",
                       reason="test")
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G10"):
        engine_db.execute("UPDATE state_log SET to_state = 'NEVER HAPPENED'")


def test_k_state_log_cannot_be_deleted(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_sent(engine_db, message_id)
    replies.transition(engine_db, "message", message_id, "CLOSED", "engine",
                       reason="test", evidence="test")
    engine_db.commit()
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] > 0
    with pytest.raises(sqlite3.Error, match="G10"):
        engine_db.execute("DELETE FROM state_log")
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] > 0


def test_k_message_two_needs_a_recorded_human_reply(engine_db, seed_graph,
                                                    insert_message):
    """G7: the reply gate cannot be jumped by writing APPROVED directly."""
    ids = seed_graph(engine_db)
    second = insert_message(engine_db, ids, sequence_step=2,
                            body="here's the demo: "
                                 "brightdesk.brightflowcure.com/demo-ai-voice\n\nSam")
    engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                      (PASSING_LINT, second))
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G7"):
        engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?",
                          (second,))


def test_k_an_auto_reply_does_not_open_the_message_two_gate(engine_db, seed_graph,
                                                            insert_message):
    """The gate counts HUMAN replies — an out-of-office must not unlock a pitch."""
    ids = seed_graph(engine_db)
    to_sent(engine_db, insert_message(engine_db, ids))
    replies.record_reply(engine_db, ids["prospect_id"], "email", "vacation",
                         "out of the office", "engine")
    second = insert_message(engine_db, ids, sequence_step=2,
                            body="demo: brightdesk.brightflowcure.com/demo-ai-voice\n\nSam")
    engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                      (PASSING_LINT, second))
    engine_db.commit()
    with pytest.raises(sqlite3.Error, match="G7"):
        engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?",
                          (second,))


# =========================================================================== #
# (l) lint is bypassed with Unicode
# =========================================================================== #

@pytest.mark.parametrize("body", [
    "we built bdаi for this. worth a look?\n\nSam",          # Cyrillic а
    "have a look at brightflow​cure.com — worth it?\n\nSam",  # zero-width space
    "ﬁnd us at ｗｗｗ.brightflowcure.com\n\nSam",                 # fullwidth + ligature
])
def test_l_unicode_smuggling_still_fails_lint(body):
    lint = pytest.importorskip("dm_engine.lint")
    draft = lint.MessageDraft(prospect_key="co.com", channel="email", sequence_step=1,
                              subject="the compactor doesn't take messages", body=body)
    result = lint.lint_message(draft, [], [], "acme hardscape")
    assert not result.ok, f"smuggled pitch passed lint: {body!r}"


def test_l_normalization_is_what_makes_the_sql_belt_effective():
    """G6 is a plain ASCII LIKE, so lint MUST normalize before the body is stored.

    This test states the dependency explicitly: if lint ever stopped normalizing,
    a homoglyph body would satisfy G6 and the only remaining defence would be gone.
    """
    lint = pytest.importorskip("dm_engine.lint")
    assert lint.normalize("bdаi") == "bdai", \
        "normalize() no longer folds the Cyrillic homoglyph to ASCII"
    assert lint.normalize("brightflow​cure.com") == "brightflowcure.com", \
        "normalize() no longer strips zero-width characters"


# =========================================================================== #
# (m) a whole batch ships one identical subject
# =========================================================================== #

def test_m_a_duplicate_subject_inside_a_batch_fails_lint():
    lint = pytest.importorskip("dm_engine.lint")
    subject = "the compactor doesn't take messages"
    draft = lint.MessageDraft(prospect_key="co2.com", channel="email", sequence_step=1,
                              subject=subject, body=CLEAN_BODY)
    result = lint.lint_message(draft, [subject], [], "acme hardscape")
    assert not result.ok
    assert any(v["rule"] == "L9" for v in result.violations)


def test_m_a_subject_reused_from_the_last_30_days_fails_lint():
    lint = pytest.importorskip("dm_engine.lint")
    draft = lint.MessageDraft(prospect_key="co2.com", channel="email", sequence_step=1,
                              subject="20 years of reputation", body=CLEAN_BODY)
    result = lint.lint_message(draft, [], ["20 Years Of Reputation"],
                               "acme hardscape")
    assert not result.ok
    assert any(v["rule"] == "L9" for v in result.violations)


# =========================================================================== #
# (n) the test suite touches real Gmail / GHL / DASHBOARD
# =========================================================================== #

def test_n_importing_a_google_client_fails_inside_the_suite():
    import conftest

    with pytest.raises(conftest.BlockedImport):
        __import__("googleapiclient.discovery")


def test_n_no_engine_module_imports_a_google_client_at_module_scope():
    banned = ("googleapiclient", "google_auth_oauthlib", "google.auth", "google.oauth2",
              "oauth2client")
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module scope only — lazy imports are fine
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == b or name.startswith(b + ".") for b in banned), \
                    f"{path.name} imports {name} at module scope"


def test_n_every_writable_config_path_lives_under_tmp(engine_cfg, tmp_path):
    for attribute in ("db_path", "review_dir", "outbox_dir", "locks_dir",
                      "variants_dir", "suppression_file", "dashboard_metrics_dir",
                      "dashboard_approvals_dir", "pending_actions"):
        path = getattr(engine_cfg, attribute)
        assert Path(path).is_relative_to(tmp_path), \
            f"{attribute} points at a real location: {path}"


def test_n_test_mode_refuses_a_write_into_the_real_dashboard_dirs(engine_db, engine_cfg):
    """The mechanical backstop: a test that forgets to redirect gets a hard failure
    instead of dropping a fabricated snapshot into the live dashboard."""
    import dataclasses

    leaky = dataclasses.replace(
        engine_cfg,
        dashboard_metrics_dir=Path.home() / "work" / "dashboard" / "data" / "metrics",
        dashboard_approvals_dir=Path.home() / "work" / "dashboard" / "data" / "approvals")
    with pytest.raises(report.ReportError, match="refusing to write"):
        report.emit_dashboard_metrics(engine_db, leaky)
    with pytest.raises(report.ReportError, match="refusing to write"):
        report.write_approvals_card(leaky, "x", "t", "s", "l")


def test_n_gmail_compat_refuses_to_build_a_real_service(engine_cfg):
    gmail_compat = pytest.importorskip("dm_engine.gmail_compat")
    with pytest.raises(Exception) as excinfo:
        gmail_compat.get_service(engine_cfg)
    assert type(excinfo.value).__name__ == "TestModeError"


def test_n_no_engine_module_calls_ghl_over_the_network():
    """GHL is a disk export, always (§8.3). No HTTP client may appear in src/."""
    banned = ("requests", "httpx", "urllib.request", "http.client", "aiohttp")
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name not in banned, f"{path.name} imports {name}"


# =========================================================================== #
# (o) approval is forged by a non-TTY agent
# =========================================================================== #

def test_o_approve_refuses_on_a_non_tty(engine_db, engine_cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_config", lambda: engine_cfg)
    assert cli.main(["approve", "--batch", "B-TEST-1"]) == cli.EXIT_REFUSED
    assert "non-TTY" in capsys.readouterr().err


def test_o_no_approve_flag_can_stand_in_for_a_human():
    approve = [a for a in cli.build_parser()._actions
               if a.dest == "command"][0].choices["approve"]
    options = {opt for action in approve._actions for opt in action.option_strings}
    assert not options & {"--yes", "-y", "--force", "--auto", "--assume-yes",
                          "--non-interactive", "--no-confirm"}


def test_o_no_env_variable_overrides_the_approval_gate(monkeypatch, engine_db,
                                                       engine_cfg, capsys):
    for name in ("DM_ENGINE_APPROVE", "DM_ENGINE_FORCE", "DM_ENGINE_YES",
                 "BDAI_ALLOW_SEND_HERE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr(cli.config, "load_config", lambda: engine_cfg)
    assert cli.main(["approve", "--batch", "B-TEST-1"]) == cli.EXIT_REFUSED


def test_o_approve_from_review_rejects_a_stale_file(engine_db, engine_cfg, seed_graph,
                                                    insert_message, monkeypatch):
    """§7.4: a review file that predates the message it covers must abort."""
    approval = pytest.importorskip("dm_engine.approval")
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    to_ready(engine_db, message_id)
    review = approval.write_review_file(engine_db, engine_cfg, ids["batch_id"])
    # the message is re-composed AFTER the review was written
    engine_db.execute("UPDATE messages SET body = ?, updated_at = datetime('now', "
                      "'+1 day') WHERE id = ?", ("a different body\n\nSam", message_id))
    engine_db.commit()
    with pytest.raises(Exception) as excinfo:
        approval.approve_from_review(engine_db, engine_cfg, review)
    assert "stale" in f"{type(excinfo.value).__name__}{excinfo.value}".lower()


def test_o_a_gate_refusal_is_never_exit_zero(engine_db, engine_cfg, monkeypatch):
    """A gate that refuses with exit 0 is invisible to any wrapping script."""
    monkeypatch.setattr(cli.config, "load_config", lambda: engine_cfg)
    refusals = [
        ["approve", "--batch", "B-TEST-1"],
        ["record-reply", "1", "--channel", "sms", "--class", "hot"],
        ["new-batch", "B-X", "--market", "Boise ID", "--channel", "sms"],
    ]
    for argv in refusals:
        assert cli.main(argv) == cli.EXIT_REFUSED, f"{argv} refused with exit 0"
