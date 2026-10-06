"""state.transition(): the legal edge set, and every illegal edge refused.

PLAN.md §7.2, §12.5. The point of this file is that nothing reaches a prospect by
accident. Two properties matter more than the rest:

  * the legal edge table is written out here a second time, by hand. If somebody
    widens `state.MESSAGE_TRANSITIONS` — say by adding READY -> STAGED to "make
    staging easier" — this test fails before the gate is gone.
  * the edges that look harmless are tested explicitly: READY -> STAGED skips
    human approval; SENT -> APPROVED rewrites history.
"""

import sqlite3

import pytest

from dm_engine import state
from dm_engine.state import IllegalTransition, transition

from conftest import PASSING_LINT

MESSAGE_STATES = [
    "COMPOSED", "LINT_FAILED", "READY", "APPROVED", "STAGED", "SENT",
    "BOUNCED", "REPLIED", "CLOSED", "REJECTED", "CANCELLED",
]

#: The contract, restated independently of the implementation.
EXPECTED_MESSAGE_EDGES = {
    ("COMPOSED", "READY"), ("COMPOSED", "LINT_FAILED"), ("COMPOSED", "CANCELLED"),
    ("LINT_FAILED", "COMPOSED"), ("LINT_FAILED", "CANCELLED"),
    ("READY", "APPROVED"), ("READY", "REJECTED"), ("READY", "CANCELLED"),
    ("APPROVED", "STAGED"), ("APPROVED", "CANCELLED"),
    ("STAGED", "SENT"), ("STAGED", "CANCELLED"),
    ("SENT", "REPLIED"), ("SENT", "BOUNCED"), ("SENT", "CLOSED"),
    ("REPLIED", "CLOSED"), ("BOUNCED", "CLOSED"),
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def advance(conn, message_id, *states, evidence="test evidence"):
    """Walk a message along legal edges so a test can start from any state."""
    for target in states:
        transition(conn, "message", message_id, target, "engine",
                   reason="test setup", evidence=evidence)


def ready_message(conn, seed_graph, insert_message, **kwargs):
    ids = seed_graph(conn, **kwargs)
    message_id = insert_message(conn, ids, lint_report=PASSING_LINT)
    return ids, message_id


# --------------------------------------------------------------------------- #
# the table itself
# --------------------------------------------------------------------------- #

def test_the_legal_edge_set_is_exactly_what_the_plan_says():
    actual = {(src, dst) for src, targets in state.MESSAGE_TRANSITIONS.items()
              for dst in targets}
    assert actual == EXPECTED_MESSAGE_EDGES, (
        "the message state machine changed. Widening it is how outreach escapes "
        "the approval gate — update PLAN.md §7.2 and this test deliberately, or "
        "revert the change."
    )


def test_every_message_state_in_the_schema_appears_in_the_table():
    assert set(state.MESSAGE_TRANSITIONS) == set(MESSAGE_STATES)


def test_terminal_states_go_nowhere():
    for terminal in ("REJECTED", "CANCELLED", "CLOSED"):
        assert state.MESSAGE_TRANSITIONS[terminal] == frozenset()


def test_staging_cannot_be_reached_without_approval():
    """The whole engine in one assertion."""
    assert "STAGED" not in state.MESSAGE_TRANSITIONS["READY"]
    assert "STAGED" not in state.MESSAGE_TRANSITIONS["COMPOSED"]
    assert state.MESSAGE_TRANSITIONS["APPROVED"] >= frozenset({"STAGED"})


# --------------------------------------------------------------------------- #
# accepted transitions
# --------------------------------------------------------------------------- #

def test_the_happy_path_walks_composed_to_sent(engine_db, seed_graph, insert_message):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    advance(engine_db, message_id, "READY", "APPROVED", "STAGED")
    transition(engine_db, "message", message_id, "SENT", "operator",
               reason="sent by hand", evidence="manual_confirm:smoke")
    row = engine_db.execute(
        "SELECT state, approved_at, staged_at, sent_at, sent_evidence FROM messages "
        "WHERE id = ?", (message_id,)).fetchone()
    assert row["state"] == "SENT"
    assert row["approved_at"] and row["staged_at"] and row["sent_at"]
    assert row["sent_evidence"] == "manual_confirm:smoke"


def test_every_transition_writes_one_audit_row(engine_db, seed_graph, insert_message):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    transition(engine_db, "message", message_id, "READY", "agent:lint",
               reason="lint passed", evidence="lint_report")
    rows = engine_db.execute(
        "SELECT * FROM state_log WHERE entity='message' AND entity_id = ?",
        (str(message_id),)).fetchall()
    assert len(rows) == 1
    assert (rows[0]["from_state"], rows[0]["to_state"]) == ("COMPOSED", "READY")
    assert rows[0]["actor"] == "agent:lint"
    assert rows[0]["reason"] == "lint passed"
    assert rows[0]["evidence"] == "lint_report"


def test_lint_failed_can_be_recomposed(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    advance(engine_db, message_id, "LINT_FAILED", "COMPOSED")
    assert state.current_state(engine_db, "message", message_id) == "COMPOSED"


def test_note_records_an_audit_row_without_moving(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    state.note(engine_db, "message", message_id, "engine",
               "VIOLATION: attempted stage of an unapproved message")
    row = engine_db.execute(
        "SELECT from_state, to_state, reason FROM state_log WHERE entity_id = ?",
        (str(message_id),)).fetchone()
    assert row["from_state"] == row["to_state"] == "COMPOSED"
    assert row["reason"].startswith("VIOLATION")
    assert state.current_state(engine_db, "message", message_id) == "COMPOSED"


# --------------------------------------------------------------------------- #
# refused transitions
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path, illegal", [
    ([], "APPROVED"),                                    # COMPOSED -> APPROVED
    ([], "STAGED"),                                      # COMPOSED -> STAGED
    ([], "SENT"),                                        # COMPOSED -> SENT
    ([], "REPLIED"),
    (["READY"], "STAGED"),                               # skips approval
    (["READY"], "SENT"),
    (["READY", "APPROVED"], "SENT"),                     # claims an unmade send
    (["READY", "APPROVED"], "READY"),                    # un-approves backwards
    (["READY", "APPROVED", "STAGED"], "APPROVED"),       # un-stages
    (["READY", "APPROVED", "STAGED"], "READY"),
    (["READY", "REJECTED"], "APPROVED"),                 # revives a rejection
    (["CANCELLED"], "READY"),
])
def test_illegal_edges_are_refused(engine_db, seed_graph, insert_message, path, illegal):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    advance(engine_db, message_id, *path)
    before = state.current_state(engine_db, "message", message_id)
    with pytest.raises(IllegalTransition):
        transition(engine_db, "message", message_id, illegal, "engine",
                   evidence="forced")
    assert state.current_state(engine_db, "message", message_id) == before


def test_sent_to_approved_cannot_rewrite_history(engine_db, seed_graph, insert_message):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    advance(engine_db, message_id, "READY", "APPROVED", "STAGED")
    transition(engine_db, "message", message_id, "SENT", "operator",
               evidence="manual_confirm:x")
    for target in ("APPROVED", "STAGED", "READY", "COMPOSED"):
        with pytest.raises(IllegalTransition):
            transition(engine_db, "message", message_id, target, "engine",
                       evidence="forced")
    assert state.current_state(engine_db, "message", message_id) == "SENT"


def test_a_self_edge_is_not_a_no_op_it_is_refused(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(IllegalTransition, match="COMPOSED -> COMPOSED"):
        transition(engine_db, "message", message_id, "COMPOSED", "engine")


def test_sent_requires_evidence(engine_db, seed_graph, insert_message):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    advance(engine_db, message_id, "READY", "APPROVED", "STAGED")
    with pytest.raises(IllegalTransition, match="evidence"):
        transition(engine_db, "message", message_id, "SENT", "operator")
    assert state.current_state(engine_db, "message", message_id) == "STAGED"


def test_an_invented_state_is_refused(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(IllegalTransition, match="not a known message state"):
        transition(engine_db, "message", message_id, "DELIVERED", "engine")


def test_an_unknown_entity_is_a_programming_error(engine_db):
    with pytest.raises(ValueError, match="unknown entity"):
        transition(engine_db, "invoice", 1, "PAID", "engine")


def test_a_missing_row_raises_lookup_error(engine_db):
    with pytest.raises(LookupError):
        transition(engine_db, "message", 9999, "READY", "engine")


def test_actor_is_required(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(ValueError, match="actor"):
        transition(engine_db, "message", message_id, "READY", "")


# --------------------------------------------------------------------------- #
# the SQL backstop still holds when the Python layer is bypassed
# --------------------------------------------------------------------------- #

def test_a_raw_sql_jump_to_staged_is_aborted_by_trigger_g3(engine_db, seed_graph,
                                                           insert_message):
    """state.py is the readable gate; the trigger is the one a shell cannot dodge."""
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    transition(engine_db, "message", message_id, "READY", "engine")
    with pytest.raises(sqlite3.IntegrityError, match="G3"):
        engine_db.execute("UPDATE messages SET state='STAGED' WHERE id = ?", (message_id,))
    engine_db.rollback()
    assert state.current_state(engine_db, "message", message_id) == "READY"


def test_a_raw_sql_jump_to_sent_is_aborted_by_trigger_g4(engine_db, seed_graph,
                                                         insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(sqlite3.IntegrityError, match="G4"):
        engine_db.execute("UPDATE messages SET state='SENT' WHERE id = ?", (message_id,))
    engine_db.rollback()


# --------------------------------------------------------------------------- #
# transaction behaviour — this is what makes the cap check atomic
# --------------------------------------------------------------------------- #

def test_transition_joins_an_open_transaction_and_does_not_commit_it(
        engine_db, seed_graph, insert_message):
    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    engine_db.execute("BEGIN IMMEDIATE")
    transition(engine_db, "message", message_id, "READY", "engine")
    assert engine_db.in_transaction, "transition must not commit the caller's transaction"
    engine_db.rollback()
    assert state.current_state(engine_db, "message", message_id) == "COMPOSED"
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] == 0


def test_transition_commits_when_it_opened_the_transaction(engine_db, seed_graph,
                                                           insert_message, engine_cfg):
    from dm_engine import db

    ids, message_id = ready_message(engine_db, seed_graph, insert_message)
    transition(engine_db, "message", message_id, "READY", "engine")
    other = db.connect(engine_cfg)
    try:
        row = other.execute("SELECT state FROM messages WHERE id = ?",
                            (message_id,)).fetchone()
        assert row["state"] == "READY", "a second connection must see the committed state"
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# prospects and batches use the same machine
# --------------------------------------------------------------------------- #

def test_prospect_transitions(engine_db, seed_graph):
    ids = seed_graph(engine_db, prospect_state="NEW")
    transition(engine_db, "prospect", ids["prospect_id"], "QUALIFIED", "operator")
    assert state.current_state(engine_db, "prospect", ids["prospect_id"]) == "QUALIFIED"
    with pytest.raises(IllegalTransition):
        transition(engine_db, "prospect", ids["prospect_id"], "NEW", "operator")


def test_suppression_is_permanent(engine_db, seed_graph):
    ids = seed_graph(engine_db, prospect_state="NEW")
    transition(engine_db, "prospect", ids["prospect_id"], "SUPPRESSED", "operator",
               reason="hard no")
    with pytest.raises(IllegalTransition):
        transition(engine_db, "prospect", ids["prospect_id"], "QUALIFIED", "operator")


def test_batch_transitions(engine_db, seed_graph):
    ids = seed_graph(engine_db)
    transition(engine_db, "batch", ids["batch_id"], "COMPOSED", "engine")
    transition(engine_db, "batch", ids["batch_id"], "REVIEWED", "operator")
    with pytest.raises(IllegalTransition):
        transition(engine_db, "batch", ids["batch_id"], "OPEN", "engine")
    transition(engine_db, "batch", ids["batch_id"], "STAGED", "engine")
    assert state.current_state(engine_db, "batch", ids["batch_id"]) == "STAGED"


def test_legal_targets_reports_the_table(engine_db):
    assert state.legal_targets("message", "APPROVED") == frozenset({"STAGED", "CANCELLED"})
    with pytest.raises(IllegalTransition):
        state.legal_targets("message", "NOPE")
