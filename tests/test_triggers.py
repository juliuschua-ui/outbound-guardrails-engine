"""G1-G11: the gates that hold even against a direct `sqlite3` write (PLAN.md §3.3).

Every test here issues the raw offending SQL — no engine code in the path — and asserts
the trigger aborts it. A trigger that exists but never fires is worse than no trigger.
"""

import sqlite3

import pytest

from conftest import PASSING_LINT


def assert_aborts(conn, tag, sql, params=()):
    """Run SQL that must be rejected by a trigger; assert the G-tag in the error."""
    with pytest.raises(sqlite3.Error) as exc:
        conn.execute(sql, params)
        conn.commit()
    assert tag in str(exc.value), f"expected {tag} abort, got: {exc.value}"
    conn.rollback()


def ready(conn, msg_id, lint_report=PASSING_LINT):
    """Move a COMPOSED message to READY the legitimate way."""
    conn.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                 (lint_report, msg_id))
    conn.commit()


def approved(conn, msg_id):
    ready(conn, msg_id)
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))
    conn.commit()


def staged(conn, msg_id):
    approved(conn, msg_id)
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
    conn.commit()


def insert_sql(ids, state=None, step=1, body="clean body", subject="a lowercase subject"):
    cols = "batch_id, prospect_id, contact_id, assignment_id, variant_id, sequence_step, " \
           "channel, subject, body"
    vals = [ids["batch_id"], ids["prospect_id"], ids["contact_id"], ids["assignment_id"],
            ids["variant_id"], step, ids["channel"], subject, body]
    if state is not None:
        cols += ", state"
        vals.append(state)
    marks = ", ".join("?" * len(vals))
    return f"INSERT INTO messages ({cols}) VALUES ({marks})", vals


# --------------------------------------------------------------------------- #
# G1 — messages are born COMPOSED
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("state", ["APPROVED", "READY", "STAGED", "SENT", "REPLIED"])
def test_g1_blocks_inserting_a_message_in_any_advanced_state(engine_db, seed_graph, state):
    ids = seed_graph(engine_db)
    sql, vals = insert_sql(ids, state=state)
    assert_aborts(engine_db, "G1", sql, vals)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_g1_allows_the_default_and_explicit_composed(engine_db, seed_graph):
    ids = seed_graph(engine_db)
    sql, vals = insert_sql(ids)                       # no state column -> DEFAULT 'COMPOSED'
    engine_db.execute(sql, vals)
    sql, vals = insert_sql(ids, state="COMPOSED", step=2)
    engine_db.execute(sql, vals)
    engine_db.commit()
    assert engine_db.execute(
        "SELECT count(*) FROM messages WHERE state = 'COMPOSED'").fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# G2 — APPROVED only from READY
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("from_state", ["COMPOSED", "STAGED", "REJECTED", "LINT_FAILED"])
def test_g2_blocks_approval_from_anything_but_ready(engine_db, seed_graph, insert_message,
                                                    from_state):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    if from_state == "STAGED":
        staged(engine_db, msg_id)
    elif from_state != "COMPOSED":
        engine_db.execute("UPDATE messages SET state = ? WHERE id = ?", (from_state, msg_id))
        engine_db.commit()
    assert_aborts(engine_db, "G2",
                  "UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))


def test_g2_allows_ready_to_approved(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    approved(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "APPROVED"


# --------------------------------------------------------------------------- #
# G3 — STAGED only from APPROVED (the export gate)
# --------------------------------------------------------------------------- #

def test_g3_blocks_staging_a_ready_message(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    ready(engine_db, msg_id)
    assert_aborts(engine_db, "G3",
                  "UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "READY"


def test_g3_blocks_staging_straight_from_composed(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    assert_aborts(engine_db, "G3",
                  "UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))


def test_g3_allows_approved_to_staged(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    staged(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "STAGED"


# --------------------------------------------------------------------------- #
# G4 — SENT only from STAGED, and only with evidence
# --------------------------------------------------------------------------- #

def test_g4_blocks_sent_without_evidence(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    staged(engine_db, msg_id)
    assert_aborts(engine_db, "G4",
                  "UPDATE messages SET state = 'SENT' WHERE id = ?", (msg_id,))


def test_g4_blocks_sent_from_a_non_staged_state(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    approved(engine_db, msg_id)
    assert_aborts(engine_db, "G4",
                  "UPDATE messages SET sent_evidence = 'forged', state = 'SENT' WHERE id = ?",
                  (msg_id,))


def test_g4_blocks_faking_a_send_on_a_fresh_message(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    assert_aborts(engine_db, "G4",
                  "UPDATE messages SET sent_evidence = 'nope', state = 'SENT' WHERE id = ?",
                  (msg_id,))


def test_g4_allows_staged_plus_evidence(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    staged(engine_db, msg_id)
    engine_db.execute(
        "UPDATE messages SET sent_evidence = 'gmail_sent_scan:19a', state = 'SENT' WHERE id = ?",
        (msg_id,))
    engine_db.commit()
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "SENT"


# --------------------------------------------------------------------------- #
# G5 — READY requires a passing lint_report
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("lint_report, label", [
    (None, "null report"),
    ('{"ok": false, "violations": [{"rule": "L1"}]}', "ok=false"),
    ('{}', "valid JSON with no ok key"),
    ('{"violations": []}', "report missing the ok key"),
    ('{"ok": 0}', "ok=0"),
    ('{"ok": "true"}', "ok as a string"),
])
def test_g5_blocks_ready_without_a_passing_lint_report(engine_db, seed_graph, insert_message,
                                                       lint_report, label):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    assert_aborts(engine_db, "G5",
                  "UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                  (lint_report, msg_id))


def test_g5_rejects_a_malformed_lint_report(engine_db, seed_graph, insert_message):
    """Not JSON at all -> json_extract errors -> the transition still fails closed."""
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    with pytest.raises(sqlite3.Error):
        engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                          ("not json at all", msg_id))
    engine_db.rollback()
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "COMPOSED"


def test_g5_allows_ready_with_ok_true(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    ready(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "READY"


# --------------------------------------------------------------------------- #
# G6 — Message-1 content gate (no link, no product name)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", [
    "take a look at http://example.com when you get a sec",
    "book here: https://brightdesk.brightflowcure.com/demo-ai-voice",
    "we are at www.brightflowcure.com",
    "this is what brightflowcure does",
    "BDAI answers the phone for you",
    "our bright desk ai handles it",
])
def test_g6_blocks_a_message_1_carrying_a_link_or_product_name(engine_db, seed_graph,
                                                               insert_message, body):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids, body=body, lint_report=PASSING_LINT)
    assert_aborts(engine_db, "G6",
                  "UPDATE messages SET state = 'READY' WHERE id = ?", (msg_id,))


def test_g6_blocks_a_body_swapped_in_after_lint(engine_db, seed_graph, insert_message):
    """The adversarial (b) path: bypass lint by raw-SQL editing the body, then approve."""
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    ready(engine_db, msg_id)
    engine_db.execute("UPDATE messages SET body = ? WHERE id = ?",
                      ("here is the demo https://brightdesk.brightflowcure.com/demo-ai-voice",
                       msg_id))
    engine_db.commit()
    assert_aborts(engine_db, "G6",
                  "UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))


def test_g6_is_case_insensitive_on_the_product_name(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids, body="bdai will answer it", lint_report=PASSING_LINT)
    assert_aborts(engine_db, "G6",
                  "UPDATE messages SET state = 'READY' WHERE id = ?", (msg_id,))


def test_g6_does_not_touch_message_2(engine_db, seed_graph, insert_message):
    """Step 2 is the demo-link message — the gate must not fire there."""
    ids = seed_graph(engine_db)
    msg_id = insert_message(
        engine_db, ids, sequence_step=2,
        body="here is the demo: https://brightdesk.brightflowcure.com/demo-ai-voice")
    ready(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "READY"


# --------------------------------------------------------------------------- #
# G7 — Message 2 requires a recorded human reply
# --------------------------------------------------------------------------- #

def test_g7_blocks_message_2_approval_without_any_reply(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids, sequence_step=2)
    ready(engine_db, msg_id)
    assert_aborts(engine_db, "G7",
                  "UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))


def test_g7_blocks_message_2_when_the_only_reply_is_an_auto_responder(engine_db, seed_graph,
                                                                     insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids, sequence_step=2)
    ready(engine_db, msg_id)
    engine_db.execute(
        "INSERT INTO replies (prospect_id, gmail_message_id, received_at, classification, "
        "is_human, classified_by) VALUES (?, 'ooo1', datetime('now'), 'vacation', 0, "
        "'heuristic')", (ids["prospect_id"],))
    engine_db.commit()
    assert_aborts(engine_db, "G7",
                  "UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))


def test_g7_blocks_message_2_when_the_reply_belongs_to_another_prospect(engine_db, seed_graph,
                                                                       insert_message):
    ids = seed_graph(engine_db)
    other = seed_graph(engine_db, prospect_key="othercompany.com", company_name="Other Co",
                       domain="othercompany.com", address="sam@othercompany.com")
    msg_id = insert_message(engine_db, ids, sequence_step=2)
    ready(engine_db, msg_id)
    engine_db.execute(
        "INSERT INTO replies (prospect_id, gmail_message_id, received_at, classification, "
        "is_human, classified_by) VALUES (?, 'r-other', datetime('now'), 'hot', 1, 'operator')",
        (other["prospect_id"],))
    engine_db.commit()
    assert_aborts(engine_db, "G7",
                  "UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))


def test_g7_allows_message_2_after_a_human_reply(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids, sequence_step=2)
    ready(engine_db, msg_id)
    engine_db.execute(
        "INSERT INTO replies (prospect_id, gmail_message_id, received_at, classification, "
        "is_human, classified_by) VALUES (?, 'r1', datetime('now'), 'hot', 1, 'operator')",
        (ids["prospect_id"],))
    engine_db.commit()
    engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (msg_id,))
    engine_db.commit()
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "APPROVED"


# --------------------------------------------------------------------------- #
# G8 — no message may reference a suppressed contact
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("channel, address, scope", [
    ("email", "alex@acmehardscapeaz.com", "address"),
    ("all",   "alex@acmehardscapeaz.com", "address"),
    ("email", "@acmehardscapeaz.com", "domain"),
    ("email", "alex@acmehardscapeaz.com", "person"),
])
def test_g8_blocks_a_message_to_a_suppressed_contact(engine_db, seed_graph, channel, address,
                                                     scope):
    ids = seed_graph(engine_db)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES (?, ?, ?, 'hard_no', 'reply_scan')", (channel, address, scope))
    engine_db.commit()
    sql, vals = insert_sql(ids)
    assert_aborts(engine_db, "G8", sql, vals)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_g8_ignores_suppression_on_a_different_channel(engine_db, seed_graph):
    ids = seed_graph(engine_db)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason) "
        "VALUES ('linkedin', 'alex@acmehardscapeaz.com', 'address', 'hard_no')")
    engine_db.commit()
    sql, vals = insert_sql(ids)
    engine_db.execute(sql, vals)
    engine_db.commit()
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_g8_survives_the_file_sync_path(engine_db, seed_graph):
    """Suppression added after composing still blocks the NEXT message insert."""
    ids = seed_graph(engine_db)
    sql, vals = insert_sql(ids)
    engine_db.execute(sql, vals)
    engine_db.commit()
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', 'alex@acmehardscapeaz.com', 'address', 'unsubscribe', 'file_sync')")
    engine_db.commit()
    sql, vals = insert_sql(ids, step=2)
    assert_aborts(engine_db, "G8", sql, vals)


# --------------------------------------------------------------------------- #
# G9 — email staging requires PASS, or RISK from a human-seen source
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict, source", [
    ("FAIL", "website"),
    ("RISK", "format_guess"),
    ("RISK", "apollo"),
    ("RISK", "directory"),
    (None, "website"),
])
def test_g9_blocks_staging_an_unverified_email(engine_db, seed_graph, insert_message,
                                               verdict, source):
    ids = seed_graph(engine_db, verify_verdict=verdict, address_source=source)
    msg_id = insert_message(engine_db, ids)
    approved(engine_db, msg_id)
    assert_aborts(engine_db, "G9",
                  "UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "APPROVED"


@pytest.mark.parametrize("verdict, source", [
    ("PASS", "format_guess"),
    ("RISK", "website"),
    ("RISK", "reply"),
    ("RISK", "manual"),
])
def test_g9_allows_pass_or_human_sourced_risk(engine_db, seed_graph, insert_message,
                                              verdict, source):
    ids = seed_graph(engine_db, verify_verdict=verdict, address_source=source)
    msg_id = insert_message(engine_db, ids)
    staged(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "STAGED"


def test_g9_does_not_apply_to_linkedin(engine_db, seed_graph, insert_message):
    """LinkedIn cards have no email verdict — the gate must not block the human-send queue."""
    ids = seed_graph(engine_db, channel="linkedin", batch_channel="linkedin",
                     address="https://linkedin.com/in/alex-x", verify_verdict=None,
                     address_source="linkedin")
    msg_id = insert_message(engine_db, ids, subject=None)
    staged(engine_db, msg_id)
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "STAGED"


# --------------------------------------------------------------------------- #
# G10 — state_log is append-only
# --------------------------------------------------------------------------- #

def _log_row(conn, msg_id=1):
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason) "
        "VALUES ('message', ?, 'READY', 'APPROVED', 'operator', 'review file')", (str(msg_id),))
    conn.commit()
    return conn.execute("SELECT id FROM state_log ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_g10_blocks_updating_the_audit_log(engine_db):
    log_id = _log_row(engine_db)
    assert_aborts(engine_db, "G10",
                  "UPDATE state_log SET actor = 'engine' WHERE id = ?", (log_id,))


def test_g10_blocks_deleting_the_audit_log(engine_db):
    log_id = _log_row(engine_db)
    assert_aborts(engine_db, "G10", "DELETE FROM state_log WHERE id = ?", (log_id,))
    assert_aborts(engine_db, "G10", "DELETE FROM state_log")
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] == 1


def test_g10_allows_appending(engine_db):
    _log_row(engine_db)
    _log_row(engine_db, msg_id=2)
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# G11 — one active Message 1 per prospect
# --------------------------------------------------------------------------- #

def test_g11_blocks_a_second_message_1(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    insert_message(engine_db, ids)
    sql, vals = insert_sql(ids)
    assert_aborts(engine_db, "G11", sql, vals)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_g11_blocks_a_second_message_1_even_after_the_first_was_sent(engine_db, seed_graph,
                                                                    insert_message):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    staged(engine_db, msg_id)
    engine_db.execute("UPDATE messages SET sent_evidence = 'x', state = 'SENT' WHERE id = ?",
                      (msg_id,))
    engine_db.commit()
    sql, vals = insert_sql(ids)
    assert_aborts(engine_db, "G11", sql, vals)


@pytest.mark.parametrize("dead_state", ["LINT_FAILED", "REJECTED", "CANCELLED"])
def test_g11_allows_a_retry_after_the_first_attempt_died(engine_db, seed_graph, insert_message,
                                                         dead_state):
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    engine_db.execute("UPDATE messages SET state = ? WHERE id = ?", (dead_state, msg_id))
    engine_db.commit()
    sql, vals = insert_sql(ids)
    engine_db.execute(sql, vals)
    engine_db.commit()
    assert engine_db.execute(
        "SELECT count(*) FROM messages WHERE state = 'COMPOSED'").fetchone()[0] == 1


def test_g11_allows_message_2_alongside_message_1(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    insert_message(engine_db, ids)
    insert_message(engine_db, ids, sequence_step=2)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_g11_is_per_prospect(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    other = seed_graph(engine_db, prospect_key="othercompany.com", company_name="Other Co",
                       domain="othercompany.com", address="sam@othercompany.com")
    insert_message(engine_db, ids)
    insert_message(engine_db, other)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# the point of all of the above
# --------------------------------------------------------------------------- #

def test_triggers_fire_on_a_plain_sqlite3_connection(engine_db, engine_cfg, seed_graph,
                                                     insert_message):
    """No engine pragmas, no engine code — the gates are in the file, not in Python."""
    ids = seed_graph(engine_db)
    msg_id = insert_message(engine_db, ids)
    ready(engine_db, msg_id)
    engine_db.commit()

    raw = sqlite3.connect(engine_cfg.db_path)
    try:
        with pytest.raises(sqlite3.Error) as exc:
            raw.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (msg_id,))
        assert "G3" in str(exc.value)
        with pytest.raises(sqlite3.Error) as exc:
            raw.execute("UPDATE messages SET sent_evidence = 'x', state = 'SENT' WHERE id = ?",
                        (msg_id,))
        assert "G4" in str(exc.value)
        raw.rollback()
    finally:
        raw.close()
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (msg_id,)).fetchone()[0] == "READY"
