"""The approval gate: review file out, human marks, approval in. PLAN.md §7.

What these tests are really asserting: there is no way to get a message from
READY to APPROVED except a human editing a file (or typing at a terminal), and
the file that was read is the file that gets credited.
"""

import re
from pathlib import Path

import pytest

from dm_engine import approval, state, suppression
from dm_engine.approval import (
    ApproveResult,
    NotATTY,
    ReviewFileError,
    StaleReviewFile,
    UnboundReviewFile,
    approve_from_review,
    approve_interactive,
    write_review_file,
)
from dm_engine.state import transition

from conftest import CLEAN_BODY, PASSING_LINT


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def make_ready(conn, seed_graph, insert_message, n=1, **overrides):
    """n READY messages, one per prospect (G11 allows one Message 1 each)."""
    ids_list, message_ids = [], []
    for index in range(n):
        ids = seed_graph(
            conn,
            prospect_key=f"prospect{index}.com",
            company_name=f"Company {index}",
            company_casual=f"company {index}",
            domain=f"prospect{index}.com",
            address=f"owner{index}@prospect{index}.com",
            **overrides,
        )
        message_id = insert_message(conn, ids, lint_report=PASSING_LINT,
                                    subject=f"subject number {index}")
        transition(conn, "message", message_id, "READY", "agent:lint")
        ids_list.append(ids)
        message_ids.append(message_id)
    return ids_list, message_ids


def mark(text, message_id, decision, reason=""):
    """Tick a checkbox the way Sam would in an editor."""
    heading = f"## message {message_id} "
    start = text.index(heading)
    end = text.find("\n## message ", start + 1)
    end = len(text) if end == -1 else end
    block = text[start:end]
    if decision == "approve":
        block = block.replace("- [ ] approve", "- [x] approve")
    else:
        block = block.replace("- [ ] reject:", f"- [x] reject: {reason}")
    return text[:start] + block + text[end:]


# --------------------------------------------------------------------------- #
# the review file
# --------------------------------------------------------------------------- #

def test_review_file_shows_everything_a_human_needs(engine_db, engine_cfg, seed_graph,
                                                    insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    text = path.read_text()

    assert path.name.endswith("-review.md")
    assert f"## message {message_ids[0]}" in text
    assert "owner0@prospect0.com" in text          # the exact recipient
    assert "PASS via website" in text              # verification provenance
    assert "company 0" in text                     # casualized name, human-checkable
    assert "V1.1" in text                          # variant under test
    assert CLEAN_BODY.splitlines()[0] in text      # the full body, not a summary
    assert "- [ ] approve" in text and "- [ ] reject:" in text
    assert "approve-all: no" in text
    assert re.search(r"send window: 08:00-18:00 America/Boise = \d\d:\d\d-\d\d:\d\d MST",
                     text), "prospect-local window must be printed against MST"


def test_review_file_lists_messages_it_did_not_include(engine_db, engine_cfg,
                                                       seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    transition(engine_db, "message", message_ids[1], "CANCELLED", "operator")
    text = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"]).read_text()
    assert "not included in this review" in text
    assert f"message {message_ids[1]}" in text.split("not included in this review")[1]


def test_review_file_flags_a_fail_verified_address_as_dropped(engine_db, engine_cfg,
                                                              seed_graph, insert_message):
    ids = seed_graph(engine_db, verify_verdict="FAIL")
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    text = write_review_file(engine_db, engine_cfg, ids["batch_id"]).read_text()
    tail = text.split("not included in this review")[1]
    assert "FAIL verdict" in tail and str(message_id) in tail


# --------------------------------------------------------------------------- #
# approving
# --------------------------------------------------------------------------- #

def test_a_ticked_box_approves_exactly_that_message(engine_db, engine_cfg, seed_graph,
                                                    insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=3)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert (result.approved, result.rejected, result.skipped) == (1, 0, 2)
    assert state.current_state(engine_db, "message", message_ids[0]) == "APPROVED"
    assert state.current_state(engine_db, "message", message_ids[1]) == "READY"
    assert state.current_state(engine_db, "message", message_ids[2]) == "READY"


def test_approval_records_the_file_path_and_its_sha256(engine_db, engine_cfg,
                                                       seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))
    approve_from_review(engine_db, engine_cfg, path)

    row = engine_db.execute(
        "SELECT approved_via, approved_at FROM messages WHERE id = ?",
        (message_ids[0],)).fetchone()
    assert row["approved_via"] == f"review_file:{path}"
    assert row["approved_at"]

    log = engine_db.execute(
        "SELECT actor, evidence FROM state_log WHERE entity_id = ? AND to_state='APPROVED'",
        (str(message_ids[0]),)).fetchone()
    assert log["actor"] == "operator"
    assert str(path) in log["evidence"]
    assert re.search(r"sha256:[0-9a-f]{64}", log["evidence"]), (
        "the exact bytes a human read must be recoverable from the audit log")


def test_a_ticked_reject_kills_the_message_with_its_reason(engine_db, engine_cfg,
                                                           seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "reject", "reads like a pitch"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert (result.approved, result.rejected) == (0, 1)
    assert state.current_state(engine_db, "message", message_ids[0]) == "REJECTED"
    log = engine_db.execute(
        "SELECT reason FROM state_log WHERE entity_id = ? AND to_state='REJECTED'",
        (str(message_ids[0]),)).fetchone()
    assert "reads like a pitch" in log["reason"]


def test_approve_all_yes_takes_the_whole_batch(engine_db, engine_cfg, seed_graph,
                                               insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=3)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(path.read_text().replace("approve-all: no", "approve-all: yes"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert result.approved == 3
    for message_id in message_ids:
        assert state.current_state(engine_db, "message", message_id) == "APPROVED"


def test_approve_all_does_not_override_an_explicit_reject(engine_db, engine_cfg,
                                                          seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    text = path.read_text().replace("approve-all: no", "approve-all: yes")
    path.write_text(mark(text, message_ids[1], "reject", "no"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert (result.approved, result.rejected) == (1, 1)
    assert state.current_state(engine_db, "message", message_ids[1]) == "REJECTED"


def test_approve_all_does_not_bypass_the_content_digest(engine_db, engine_cfg,
                                                        seed_graph, insert_message):
    """approve-all is a shortcut for ticking the boxes, never past the digest.

    The body is swapped WITHOUT bumping `updated_at` — the one edit the mtime
    comparison in `_assert_not_stale` cannot see, which is the whole reason the
    content digest exists. `approve-all: yes` used to skip `_bind()` entirely for
    an unmarked block, so this shipped copy nobody read.
    """
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(path.read_text().replace("approve-all: no", "approve-all: yes"))

    engine_db.execute("UPDATE messages SET body = 'copy nobody reviewed' WHERE id = ?",
                      (message_ids[1],))
    engine_db.commit()

    with pytest.raises(StaleReviewFile, match="no longer matches"):
        approve_from_review(engine_db, engine_cfg, path)
    for message_id in message_ids:
        assert state.current_state(engine_db, "message", message_id) == "READY"


def test_approve_all_cannot_reach_a_block_from_another_batch(engine_db, engine_cfg,
                                                             seed_graph, insert_message):
    """The cross-batch binding is not optional under approve-all either.

    A block appended by hand for a message in a DIFFERENT batch, left unmarked so
    that approve-all takes it: no staleness relation constrains that block, so the
    binding in `_bind()` is the only thing standing in front of it.
    """
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    other = seed_graph(engine_db, prospect_key="other.example", company_name="Other Co",
                       company_casual="other co", domain="other.example",
                       address="owner@other.example", batch_id="B-TEST-2")
    other_id = insert_message(engine_db, other, lint_report=PASSING_LINT)
    transition(engine_db, "message", other_id, "READY", "agent:lint")

    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(
        path.read_text().replace("approve-all: no", "approve-all: yes")
        + f"\n---\n\n## message {other_id} — Other Co [V1.1]\n\n- [ ] approve\n")

    with pytest.raises(ReviewFileError):
        approve_from_review(engine_db, engine_cfg, path)
    assert state.current_state(engine_db, "message", other_id) == "READY"
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_a_hand_written_file_is_refused(engine_db, engine_cfg, seed_graph,
                                        insert_message, tmp_path):
    """A markdown file an agent typed approves nothing — approve-all or not.

    It carries everything a forger can produce: the heading, a `- batch:` line
    naming the real batch, `approve-all: yes`, and a `## message <id>` block per
    READY message. What it cannot produce is the `- content: sha256:` line, which
    is derived from the copy in the database.
    """
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    forged = tmp_path / "forged-review.md"
    forged.write_text(
        f"# review — {ids_list[0]['batch_id']}\n\n"
        f"- batch: {ids_list[0]['batch_id']}\n"
        "- approve-all: yes\n\n"
        + "".join(f"\n---\n\n## message {message_id} — Company [V1.1]\n\n"
                 for message_id in message_ids),
        encoding="utf-8")

    with pytest.raises(UnboundReviewFile, match="content: sha256:"):
        approve_from_review(engine_db, engine_cfg, forged)
    for message_id in message_ids:
        assert state.current_state(engine_db, "message", message_id) == "READY"


def test_a_file_with_no_batch_header_is_refused(engine_db, engine_cfg, seed_graph,
                                                insert_message):
    """Gate one: without `- batch:` the file cannot say which batch it read."""
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    text = mark(path.read_text(), message_ids[0], "approve")
    path.write_text("\n".join(line for line in text.splitlines()
                              if not line.startswith("- batch:")) + "\n")

    with pytest.raises(UnboundReviewFile, match="no `- batch: <id>` header"):
        approve_from_review(engine_db, engine_cfg, path)
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_an_appended_block_with_no_content_line_is_refused(engine_db, engine_cfg,
                                                           seed_graph, insert_message):
    """Gate two: a REAL review file plus one hand-appended block is still refused.

    The appended block wins the parse (later block for the same id overwrites the
    generated one), so this is the shape a forger actually has — the file's own
    provenance is real and only the approving block is invented.
    """
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(
        path.read_text()
        + f"\n---\n\n## message {message_ids[1]} — Company 1 [V1.1]\n\n- [x] approve\n")

    with pytest.raises(UnboundReviewFile, match="appended by hand"):
        approve_from_review(engine_db, engine_cfg, path)
    for message_id in message_ids:
        assert state.current_state(engine_db, "message", message_id) == "READY"


# --------------------------------------------------------------------------- #
# the suppression re-check at approval time
# --------------------------------------------------------------------------- #

def test_an_address_suppressed_after_review_is_refused_at_approval(
        engine_db, engine_cfg, seed_graph, insert_message):
    """The file may be minutes old. G8 and G12 are BEFORE INSERT triggers, so
    neither can fire on a READY -> APPROVED update — this re-check is what stops
    a suppression recorded since the human read the file from being ignored."""
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))

    suppression.add(engine_db, "email", ids_list[0]["address"], "address",
                    "hard_no", "reply_scan")

    result = approve_from_review(engine_db, engine_cfg, path)

    assert (result.approved, result.skipped) == (0, 1)
    assert re.search(r"refused at approval:.*is suppressed \(hard_no\)",
                     result.notes[0][1]), result.notes
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_a_prospect_suppressed_after_review_is_refused_at_approval(
        engine_db, engine_cfg, seed_graph, insert_message):
    """The second half of the same gate: the COMPANY asked to stop, and there is
    no suppression row against this particular mailbox to catch it."""
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))

    transition(engine_db, "prospect", ids_list[0]["prospect_id"], "SUPPRESSED",
               "operator", reason="asked to stop on the phone")

    result = approve_from_review(engine_db, engine_cfg, path)

    assert (result.approved, result.skipped) == (0, 1)
    assert "the prospect is SUPPRESSED" in result.notes[0][1]
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_both_boxes_ticked_refuses_to_guess(engine_db, engine_cfg, seed_graph,
                                            insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    text = mark(path.read_text(), message_ids[0], "approve")
    path.write_text(mark(text, message_ids[0], "reject", "unsure"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert result.skipped == 1 and result.approved == 0
    assert "both approve and reject" in result.notes[0][1]
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_an_unmarked_file_approves_nothing(engine_db, engine_cfg, seed_graph,
                                           insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    result = approve_from_review(engine_db, engine_cfg, path)
    assert result == ApproveResult(0, 0, 2, result.notes)
    for message_id in message_ids:
        assert state.current_state(engine_db, "message", message_id) == "READY"


def test_an_empty_batch_is_a_quiet_no_op(engine_db, engine_cfg, seed_graph):
    ids = seed_graph(engine_db)
    path = write_review_file(engine_db, engine_cfg, ids["batch_id"])
    with pytest.raises(ReviewFileError, match="no `## message"):
        approve_from_review(engine_db, engine_cfg, path)


def test_a_non_ready_message_cannot_be_approved_from_a_review_file(
        engine_db, engine_cfg, seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))
    transition(engine_db, "message", message_ids[0], "CANCELLED", "operator")

    result = approve_from_review(engine_db, engine_cfg, path)

    assert result.approved == 0 and result.skipped == 1
    assert "not READY" in result.notes[0][1]
    assert state.current_state(engine_db, "message", message_ids[0]) == "CANCELLED"


def test_an_unknown_message_id_in_the_file_is_ignored(engine_db, engine_cfg, seed_graph,
                                                      insert_message):
    """A block for a message that does not exist approves nothing and is reported.

    Built by appending to a REAL review file: a hand-written one is now refused
    outright for having no `- batch:` header, which is a different guarantee tested
    in `test_a_hand_written_file_is_refused`. This one has to get past that gate to
    reach the unknown-id path at all.
    """
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## message 4242 — Ghost Co [V1.1]\n\n"
          f"- content: sha256:{'0' * 64}\n\n- [x] approve\n",
        encoding="utf-8")

    result = approve_from_review(engine_db, engine_cfg, path)
    assert "unknown message id" in " ".join(text for _, text in result.notes)
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_a_missing_file_is_an_error_not_an_approval(engine_db, engine_cfg, tmp_path):
    with pytest.raises(ReviewFileError):
        approve_from_review(engine_db, engine_cfg, tmp_path / "nope.md")


# --------------------------------------------------------------------------- #
# the DASHBOARD review lane — parse_review_blocks / set_review_mark
# --------------------------------------------------------------------------- #

def test_parse_review_blocks_matches_what_a_human_would_read(engine_db, engine_cfg,
                                                              seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])

    blocks = approval.parse_review_blocks(path)

    assert [b["message_id"] for b in blocks] == message_ids
    first = blocks[0]
    assert first["batch_id"] == ids_list[0]["batch_id"]
    assert first["company_name"] == "Company 0"
    assert first["variant_id"] == "V1.1"
    assert first["to"] == "owner0@prospect0.com"
    assert first["subject"] == "subject number 0"
    assert CLEAN_BODY.splitlines()[0] in first["body"]
    assert first["mark"] is None
    assert first["reject_reason"] == ""
    assert first["channel"] == "email", "must be split from the raw '{channel} · step N' line"
    assert first["sequence_step"] == 1


def test_parse_review_blocks_reads_an_existing_mark(engine_db, engine_cfg, seed_graph,
                                                     insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "reject", "too salesy"))

    blocks = {b["message_id"]: b for b in approval.parse_review_blocks(path)}
    assert blocks[message_ids[0]]["mark"] == "reject"
    assert blocks[message_ids[0]]["reject_reason"] == "too salesy"
    assert blocks[message_ids[1]]["mark"] is None


def test_set_review_mark_approve_matches_a_hand_edit(engine_db, engine_cfg, seed_graph,
                                                      insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    hand_path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    hand_text = mark(hand_path.read_text(), message_ids[0], "approve")

    api_path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    approval.set_review_mark(api_path, message_ids[0], "approve")

    assert api_path.read_text() == hand_text


def test_set_review_mark_reject_matches_a_hand_edit(engine_db, engine_cfg, seed_graph,
                                                     insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    hand_path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    hand_text = mark(hand_path.read_text(), message_ids[0], "reject", "too salesy")

    api_path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    approval.set_review_mark(api_path, message_ids[0], "reject", "too salesy")

    assert api_path.read_text() == hand_text


def test_set_review_mark_never_touches_the_content_digest(engine_db, engine_cfg,
                                                           seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    before = re.search(r"content: sha256:([0-9a-f]{64})", path.read_text()).group(1)

    approval.set_review_mark(path, message_ids[0], "approve")

    after = re.search(r"content: sha256:([0-9a-f]{64})", path.read_text()).group(1)
    assert before == after


def test_set_review_mark_only_moves_the_one_message_it_targets(engine_db, engine_cfg,
                                                                seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=3)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])

    approval.set_review_mark(path, message_ids[1], "approve")

    blocks = {b["message_id"]: b for b in approval.parse_review_blocks(path)}
    assert blocks[message_ids[0]]["mark"] is None
    assert blocks[message_ids[1]]["mark"] == "approve"
    assert blocks[message_ids[2]]["mark"] is None


def test_set_review_mark_then_approve_from_review_stages_that_message(
        engine_db, engine_cfg, seed_graph, insert_message):
    """The whole point: a click can do exactly what a hand-edit + approve did,
    through the SAME gate — same digest check, same suppression re-check."""
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])

    approval.set_review_mark(path, message_ids[0], "approve")
    result = approve_from_review(engine_db, engine_cfg, path, actor="dashboard-ui")

    assert (result.approved, result.rejected, result.skipped) == (1, 0, 1)
    assert state.current_state(engine_db, "message", message_ids[0]) == "APPROVED"
    log = engine_db.execute(
        "SELECT actor FROM state_log WHERE entity_id = ? AND to_state='APPROVED'",
        (str(message_ids[0]),)).fetchone()
    assert log["actor"] == "dashboard-ui", (
        "provenance must say who really triggered it — never silently 'operator'")


def test_set_review_mark_rejects_a_bad_decision_value(engine_db, engine_cfg, seed_graph,
                                                       insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    with pytest.raises(ValueError):
        approval.set_review_mark(path, message_ids[0], "maybe")


def test_set_review_mark_on_an_unknown_message_id_is_loud(engine_db, engine_cfg,
                                                           seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    with pytest.raises(ReviewFileError):
        approval.set_review_mark(path, 999999, "approve")


# --------------------------------------------------------------------------- #
# staleness — the file must match what is in the database
# --------------------------------------------------------------------------- #

def test_a_stale_review_file_aborts_the_whole_batch(engine_db, engine_cfg, seed_graph,
                                                    insert_message):
    """Recomposed after review = the text on disk is not the text that would ship."""
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    path = write_review_file(engine_db, engine_cfg, ids_list[0]["batch_id"])
    path.write_text(mark(path.read_text(), message_ids[0], "approve"))

    engine_db.execute(
        "UPDATE messages SET body = 'a different body entirely', "
        "updated_at = datetime('now', '+1 hour') WHERE id = ?", (message_ids[1],))
    engine_db.commit()

    with pytest.raises(StaleReviewFile, match="no longer matches"):
        approve_from_review(engine_db, engine_cfg, path)
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


# --------------------------------------------------------------------------- #
# database gates surface as refusals, not crashes
# --------------------------------------------------------------------------- #

def test_message_two_without_a_reply_is_refused_readably(engine_db, engine_cfg,
                                                         seed_graph, insert_message):
    """Trigger G7 fires; the batch must survive it as a skip with an explanation."""
    ids = seed_graph(engine_db)
    step2 = insert_message(engine_db, ids, sequence_step=2, lint_report=PASSING_LINT,
                           body="glad it resonated — brightdesk.brightflowcure.com/demo-ai-voice",
                           subject="re: the compactor")
    transition(engine_db, "message", step2, "READY", "agent:lint")
    path = write_review_file(engine_db, engine_cfg, ids["batch_id"])
    path.write_text(mark(path.read_text(), step2, "approve"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert result.approved == 0 and result.skipped == 1
    assert "G7" in result.notes[0][1]
    assert state.current_state(engine_db, "message", step2) == "READY"


def test_a_link_in_message_one_is_refused_by_trigger_g6(engine_db, engine_cfg,
                                                        seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    transition(engine_db, "message", message_id, "READY", "agent:lint")
    engine_db.execute("UPDATE messages SET body = ? WHERE id = ?",
                      ("check out https://brightdesk.brightflowcure.com", message_id))
    engine_db.commit()
    path = write_review_file(engine_db, engine_cfg, ids["batch_id"])
    path.write_text(mark(path.read_text(), message_id, "approve"))

    result = approve_from_review(engine_db, engine_cfg, path)

    assert result.approved == 0
    assert "G6" in result.notes[0][1]


# --------------------------------------------------------------------------- #
# interactive approval — no headless path exists
# --------------------------------------------------------------------------- #

def test_interactive_approval_refuses_without_a_terminal(engine_db, engine_cfg,
                                                         seed_graph, insert_message):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message)
    with pytest.raises(NotATTY, match="terminal"):
        approve_interactive(engine_db, engine_cfg, ids_list[0]["batch_id"])
    assert state.current_state(engine_db, "message", message_ids[0]) == "READY"


def test_interactive_approval_needs_the_literal_word(engine_db, engine_cfg, seed_graph,
                                                     insert_message, monkeypatch):
    ids_list, message_ids = make_ready(engine_db, seed_graph, insert_message, n=2)
    monkeypatch.setattr(approval.sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["APPROVE", "y"])       # 'y' is NOT approval
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    result = approve_interactive(engine_db, engine_cfg, ids_list[0]["batch_id"])

    assert (result.approved, result.skipped) == (1, 1)
    assert state.current_state(engine_db, "message", message_ids[0]) == "APPROVED"
    assert state.current_state(engine_db, "message", message_ids[1]) == "READY"
    row = engine_db.execute("SELECT approved_via FROM messages WHERE id = ?",
                            (message_ids[0],)).fetchone()
    assert row["approved_via"] == "interactive_tty"


def test_approval_module_has_no_bypass_flag():
    """No --yes, no --force, no env override — asserted over CODE, not prose.

    An AST walk, so the module is free to *describe* the bypasses it refuses to
    implement without the test tripping over its own documentation.
    """
    import ast

    tree = ast.parse(Path(approval.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        # An override can only enter through the environment, the command line,
        # or a flag parser. Prose describing those is fine; reading them is not.
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "argv"}:
            offenders.append(f"reads .{node.attr}")
        if isinstance(node, ast.Name) and node.id == "getenv":
            offenders.append("reads getenv")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     + [getattr(node, "module", None) or ""])
            for name in names:
                if name.split(".")[0] in {"argparse", "optparse", "click"}:
                    offenders.append(f"imports {name}")
    assert not offenders, (
        f"approval.py contains a bypass surface: {offenders}. Approval has no "
        "flag and no env override by design (PLAN.md §7.3)."
    )
