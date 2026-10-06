"""cli.py — the exit codes are the contract.

A gate that refuses with exit 0 is invisible to any script wrapping this CLI, so the
codes get tested as hard as the behaviour: 0 ok, 1 partial, 2 refused/gate,
3 gate-abort, 4 auth.
"""

import csv
import json

import pytest

from dm_engine import cli, replies

from conftest import PASSING_LINT

#: PLAN.md §12.6 lists these verbatim. `variants` is added for §4.4's retire /
#: declare-winner gate, which §12.6's list omits — see the build report.
CONTRACT_SUBCOMMANDS = {
    "init", "import-prospects", "qualify", "exclude", "set-casual", "verify",
    "new-batch", "assign", "compose", "ingest-drafts", "review", "approve", "stage",
    "reconcile", "mark-sent", "scan-replies", "ingest-triage", "record-reply",
    "attrib", "book", "sheet", "stats", "metrics-emit", "suppress", "status",
    "cancel", "drop-from-batch",
}


@pytest.fixture
def run(engine_db, engine_cfg, monkeypatch):
    """Invoke main() against the tmp_path database the fixtures already migrated.

    `load_config` is redirected to the fixture Config: a bare `load_config()` would
    hand the CLI the REAL `~/work/dashboard/...` and `~/.config/pending-actions.md`
    paths, and `metrics-emit` would drop a snapshot into the live dashboard.
    (report.assert_sandboxed is the mechanical backstop for the same mistake.)
    """
    monkeypatch.setenv("DM_ENGINE_DB", str(engine_cfg.db_path))
    monkeypatch.setattr(cli.config, "load_config", lambda: engine_cfg)

    def _run(*argv):
        engine_db.commit()
        return cli.main(list(argv))

    return _run


def sent_message(conn, seed_graph, insert_message, index=0):
    ids = seed_graph(conn, prospect_key=f"co{index}.com", company_name=f"Co {index}",
                     domain=f"co{index}.com", address=f"o{index}@co{index}.com")
    message_id = insert_message(conn, ids, subject=f"subject {index}")
    conn.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'SENT', sent_at = datetime('now'), "
                 "sent_evidence = 'test' WHERE id = ?", (message_id,))
    conn.commit()
    ids["message_id"] = message_id
    return ids


# --------------------------------------------------------------------------- #
# surface
# --------------------------------------------------------------------------- #

def test_every_contract_subcommand_exists():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    available = set(actions[0].choices)
    missing = CONTRACT_SUBCOMMANDS - available
    assert not missing, f"missing subcommands: {sorted(missing)}"


def test_there_is_no_send_subcommand():
    """PLAN.md §14 non-goal 1 — the engine contains zero send capability."""
    parser = cli.build_parser()
    commands = set([a for a in parser._actions if a.dest == "command"][0].choices)
    for banned in ("send", "send-batch", "release", "fire", "blast"):
        assert banned not in commands


def test_approve_has_no_bypass_flag():
    """§7.3: no --yes, no --force, no env override on approval. Ever."""
    parser = cli.build_parser()
    approve = [a for a in parser._actions if a.dest == "command"][0].choices["approve"]
    options = {opt for action in approve._actions for opt in action.option_strings}
    for banned in ("--yes", "-y", "--force", "--auto", "--non-interactive",
                   "--no-confirm", "--assume-yes"):
        assert banned not in options


def test_exit_codes_are_the_documented_five():
    assert (cli.EXIT_OK, cli.EXIT_PARTIAL, cli.EXIT_REFUSED, cli.EXIT_GATE_ABORT,
            cli.EXIT_AUTH) == (0, 1, 2, 3, 4)


# --------------------------------------------------------------------------- #
# the DASHBOARD review lane — `decide` proves a caller, not a terminal
# --------------------------------------------------------------------------- #

def test_decide_has_no_default_actor():
    """Unlike every other actor-taking command, `decide` must not silently
    default to 'operator' — whatever calls it has to say who it really is."""
    parser = cli.build_parser()
    decide = [a for a in parser._actions if a.dest == "command"][0].choices["decide"]
    actor_action = next(a for a in decide._actions if a.dest == "actor")
    assert actor_action.required is True
    assert actor_action.default is None


def test_decide_is_not_tty_gated(run):
    """The whole point of this command: it must NOT call _require_tty."""
    import inspect
    source = inspect.getsource(cli.cmd_decide)
    assert "_require_tty" not in source


def test_review_json_and_decide_work_without_a_tty(run, engine_db, engine_cfg,
                                                    seed_graph, insert_message, capsys):
    """End to end: a batch reaches READY, `review-json` renders it, `decide`
    approves ONE message through the exact same gate `approve` uses — no TTY."""
    from dm_engine.approval import write_review_file

    ids = seed_graph(engine_db, prospect_key="dashboardlane.com", company_name="Dashboard Lane",
                     company_casual="dashboard lane", domain="dashboardlane.com",
                     address="owner@dashboardlane.com")
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?", (message_id,))
    engine_db.commit()
    write_review_file(engine_db, engine_cfg, ids["batch_id"])

    assert run("review-json", "--batch", ids["batch_id"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["batch"] == ids["batch_id"]
    assert payload["blocks"][0]["message_id"] == message_id
    assert payload["blocks"][0]["mark"] is None

    assert run("decide", "--batch", ids["batch_id"], str(message_id),
              "--choice", "approve", "--actor", "dashboard-ui") == cli.EXIT_OK
    capsys.readouterr()

    from dm_engine import state
    assert state.current_state(engine_db, "message", message_id) == "APPROVED"
    log = engine_db.execute(
        "SELECT actor FROM state_log WHERE entity_id = ? AND to_state='APPROVED'",
        (str(message_id),)).fetchone()
    assert log["actor"] == "dashboard-ui"

    # re-rendering shows the decision, and a second identical decide is a no-op
    assert run("review-json", "--batch", ids["batch_id"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocks"][0]["mark"] == "approve"


def test_log_reply_has_no_default_actor():
    """`record-reply` may default to 'operator' because a TTY already proved one
    was there. `log-reply` has no such proof, so the caller must name itself —
    reply rows are the evidence the whole experiment is scored on."""
    parser = cli.build_parser()
    log_reply = [a for a in parser._actions if a.dest == "command"][0].choices["log-reply"]
    actor_action = next(a for a in log_reply._actions if a.dest == "actor")
    assert actor_action.required is True
    assert actor_action.default is None


def test_log_reply_is_not_tty_gated_but_record_reply_still_is(run, capsys):
    """The gate moves, it does not disappear: `record-reply --class hot` must
    still refuse without a terminal, or the TTY path would be pointless."""
    import inspect
    assert "_require_tty" not in inspect.getsource(cli.cmd_log_reply)
    assert "_require_tty" in inspect.getsource(cli.cmd_record_reply)
    assert run("record-reply", "1", "--channel", "email", "--class", "hot") \
        == cli.EXIT_REFUSED


def test_log_reply_records_a_human_reply_and_names_its_actor(
        run, engine_db, seed_graph, capsys):
    ids = seed_graph(engine_db, prospect_key="logreply.com", company_name="Log Reply",
                     domain="logreply.com", address="owner@logreply.com")
    assert run("log-reply", str(ids["prospect_id"]), "--channel", "messenger",
               "--class", "hot", "--snippet", "sure, send it over",
               "--actor", "dashboard-ui") == cli.EXIT_OK
    row = engine_db.execute(
        "SELECT classified_by, classification, is_human, snippet FROM replies "
        "WHERE prospect_id = ?", (ids["prospect_id"],)).fetchone()
    assert (row["classified_by"], row["classification"], row["is_human"]) \
        == ("dashboard-ui", "hot", 1)
    assert row["snippet"] == "sure, send it over", "verbatim text is the audit trail"


def test_log_reply_still_refuses_sms(run, capsys):
    assert run("log-reply", "1", "--channel", "sms", "--class", "hot",
               "--actor", "dashboard-ui") == cli.EXIT_REFUSED
    assert "TCPA" in capsys.readouterr().err


def test_review_json_reports_the_database_state_not_just_the_checkbox(
        run, engine_db, engine_cfg, seed_graph, insert_message, capsys):
    """The review file records a decision; the database records what became of
    it. Without the state, a UI cannot tell an approved message from one already
    staged into a Gmail draft — so it keeps offering work that is finished, which
    is exactly how the DASHBOARD DM tab kept showing staged batches as outstanding."""
    from dm_engine.approval import write_review_file

    ids = seed_graph(engine_db, prospect_key="reviewstate.com", company_name="Review State",
                     company_casual="review state", domain="reviewstate.com",
                     address="owner@reviewstate.com")
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?", (message_id,))
    engine_db.commit()
    write_review_file(engine_db, engine_cfg, ids["batch_id"])

    assert run("review-json", "--batch", ids["batch_id"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["blocks"][0]["state"] == "READY"

    # the checkbox stops changing after approval; the state does not
    run("decide", "--batch", ids["batch_id"], str(message_id),
        "--choice", "approve", "--actor", "dashboard-ui")
    capsys.readouterr()
    engine_db.execute("UPDATE messages SET state = 'STAGED', staged_at = datetime('now') "
                      "WHERE id = ?", (message_id,))
    engine_db.commit()

    assert run("review-json", "--batch", ids["batch_id"]) == cli.EXIT_OK
    block = json.loads(capsys.readouterr().out)["blocks"][0]
    assert block["mark"] == "approve", "the file still only knows the decision"
    assert block["state"] == "STAGED", "but the caller can now see it is already done"


def test_decide_on_one_message_is_not_partial_because_its_batch_mates_are_still_pending(
        run, engine_db, engine_cfg, seed_graph, insert_message, capsys):
    """Live bug, found live: `approve_from_review()` walks every row in the
    review file, so on a real multi-message batch the other rows are always
    "skipped: no box marked" until they're decided too. That used to make
    `decide` report EXIT_PARTIAL for a call that fully succeeded at the one
    thing it was asked to do — which DASHBOARD's UI painted as a red error
    banner on every real click, even though the message was approved. Success
    here is about THIS call's target, not about whether the whole file is
    fully decided yet."""
    from dm_engine.approval import write_review_file

    # Two different prospects sharing one batch — trg_one_msg1_per_prospect forbids
    # two step-1 messages on the SAME prospect, but a real batch is many prospects.
    first_ids = seed_graph(engine_db, prospect_key="dashboardpartial-one.com",
                           company_name="Dashboard Partial One", company_casual="dashboard partial one",
                           domain="dashboardpartial-one.com", address="owner@dashboardpartial-one.com",
                           batch_id="B-DASHBOARD-PARTIAL")
    second_ids = seed_graph(engine_db, prospect_key="dashboardpartial-two.com",
                            company_name="Dashboard Partial Two", company_casual="dashboard partial two",
                            domain="dashboardpartial-two.com", address="owner@dashboardpartial-two.com",
                            batch_id="B-DASHBOARD-PARTIAL")
    first_id = insert_message(engine_db, first_ids, lint_report=PASSING_LINT, subject="msg one")
    second_id = insert_message(engine_db, second_ids, lint_report=PASSING_LINT, subject="msg two")
    engine_db.execute("UPDATE messages SET state = 'READY' WHERE id IN (?, ?)",
                      (first_id, second_id))
    engine_db.commit()
    write_review_file(engine_db, engine_cfg, "B-DASHBOARD-PARTIAL")

    assert run("decide", "--batch", "B-DASHBOARD-PARTIAL", str(first_id),
              "--choice", "approve", "--actor", "dashboard-ui") == cli.EXIT_OK
    capsys.readouterr()

    from dm_engine import state
    assert state.current_state(engine_db, "message", first_id) == "APPROVED"
    assert state.current_state(engine_db, "message", second_id) == "READY"


def test_decide_records_a_run_row(run, engine_db, engine_cfg, seed_graph,
                                  insert_message, capsys):
    """Auditable like every other state-changing command (§12.6)."""
    from dm_engine.approval import write_review_file

    ids = seed_graph(engine_db, prospect_key="dashboardaudit.com", company_name="Dashboard Audit",
                     company_casual="dashboard audit", domain="dashboardaudit.com",
                     address="owner@dashboardaudit.com")
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    engine_db.execute("UPDATE messages SET state = 'READY' WHERE id = ?", (message_id,))
    engine_db.commit()
    write_review_file(engine_db, engine_cfg, ids["batch_id"])

    run("decide", "--batch", ids["batch_id"], str(message_id),
       "--choice", "approve", "--actor", "dashboard-ui")

    row = engine_db.execute(
        "SELECT command, status FROM runs WHERE command = 'decide' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and row["status"] == "ok"


#: Every subcommand that takes --channel, and whether it reaches a staging adapter.
#: A channel missing from any of these is a channel the operator cannot drive from
#: the CLI, however complete the adapter is.
CHANNEL_COMMANDS = ["new-batch", "stage", "reconcile", "record-reply", "suppress"]


@pytest.mark.parametrize("command", CHANNEL_COMMANDS)
def test_every_channel_option_offers_the_dm_channels(command):
    parser = cli.build_parser()
    sub = [a for a in parser._actions if a.dest == "command"][0].choices[command]
    channel = [a for a in sub._actions if a.dest == "channel"][0]
    assert {"whatsapp", "messenger"} <= set(channel.choices), (
        f"`dmctl {command} --channel` cannot name the DM channels")


@pytest.mark.parametrize("command", CHANNEL_COMMANDS)
def test_sms_is_still_offered_only_so_it_can_be_refused(command):
    """§I-10 — TCPA. `sms` stays a CHOICE so the refusal explains itself instead of
    argparse printing 'invalid choice', and `reconcile` never offered it at all."""
    parser = cli.build_parser()
    sub = [a for a in parser._actions if a.dest == "command"][0].choices[command]
    channel = [a for a in sub._actions if a.dest == "channel"][0]
    assert ("sms" in channel.choices) == (command != "reconcile")


def test_suppress_accepts_a_whatsapp_number_and_folds_its_punctuation(run, engine_db,
                                                                      capsys):
    assert run("suppress", "+1 (602) 555-0134", "--channel", "whatsapp",
               "--reason", "hard_no") == cli.EXIT_OK
    row = engine_db.execute(
        "SELECT address, scope FROM suppression WHERE channel = 'whatsapp'").fetchone()
    assert (row["address"], row["scope"]) == ("6025550134", "address")


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_new_batch_opens_a_dm_batch(run, engine_db, seed_graph, channel):
    seed_graph(engine_db)                       # gives us the EXP-TEST experiment
    assert run("new-batch", f"B-{channel}-1", "--experiment", "EXP-TEST",
               "--market", "Glendale AZ", "--channel", channel) == cli.EXIT_OK
    assert engine_db.execute(
        "SELECT channel FROM batches WHERE id = ?",
        (f"B-{channel}-1",)).fetchone()["channel"] == channel


# --------------------------------------------------------------------------- #
# refusals (exit 2)
# --------------------------------------------------------------------------- #

def test_approve_refuses_on_a_non_tty_without_a_review_file(run, capsys):
    """§10 row (o): a headless agent cannot approve."""
    assert run("approve", "--batch", "B-TEST-1") == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "non-TTY" in err and "--yes" in err


def test_review_json_refuses_without_a_review_file(run, capsys):
    assert run("review-json", "--batch", "B-NOPE") == cli.EXIT_REFUSED


def test_decide_refuses_without_a_review_file(run, capsys):
    assert run("decide", "--batch", "B-NOPE", "1", "--choice", "approve",
              "--actor", "dashboard-ui") == cli.EXIT_REFUSED


def test_record_reply_refuses_sms(run, capsys):
    assert run("record-reply", "1", "--channel", "sms", "--class", "hot") \
        == cli.EXIT_REFUSED
    assert "TCPA" in capsys.readouterr().err


def test_new_batch_refuses_sms(run, capsys):
    assert run("new-batch", "B-SMS", "--market", "Boise ID", "--channel", "sms") \
        == cli.EXIT_REFUSED
    assert "TCPA" in capsys.readouterr().err


def test_stage_refuses_sms(run, capsys):
    assert run("stage", "--batch", "B-TEST-1", "--channel", "sms") == cli.EXIT_REFUSED
    assert "TCPA" in capsys.readouterr().err


def test_commands_refuse_before_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DM_ENGINE_TEST", "1")
    monkeypatch.setenv("DM_ENGINE_DB", str(tmp_path / "empty.db"))
    assert cli.main(["sheet"]) == cli.EXIT_REFUSED
    assert "dmctl init" in capsys.readouterr().err


def test_config_guard_refuses_a_database_outside_the_sandbox(monkeypatch, capsys):
    monkeypatch.setenv("DM_ENGINE_TEST", "1")
    monkeypatch.setenv("DM_ENGINE_DB", "/etc/dm.db")
    assert cli.main(["status"]) == cli.EXIT_REFUSED
    assert "refusing db_path" in capsys.readouterr().err


def test_variants_force_requires_a_reason(run, capsys):
    assert run("variants", "retire", "V1.1", "--force") == cli.EXIT_REFUSED
    assert "--reason" in capsys.readouterr().err


def test_a_missing_file_is_a_refusal_not_a_traceback(run, capsys):
    assert run("ingest-triage", "/nonexistent/triage.json") == cli.EXIT_REFUSED


# --------------------------------------------------------------------------- #
# gate aborts (exit 3)
# --------------------------------------------------------------------------- #

def test_mark_sent_on_a_composed_message_is_a_gate_abort(run, engine_db, seed_graph,
                                                         insert_message, capsys):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    code = run("mark-sent", str(message_id), "--evidence", "forged")
    assert code == cli.EXIT_GATE_ABORT
    assert "gate abort" in capsys.readouterr().err
    row = engine_db.execute(
        "SELECT state, sent_at, sent_evidence FROM messages WHERE id = ?",
        (message_id,)).fetchone()
    assert row["state"] == "COMPOSED"
    # a refused mark-sent must not leave send evidence behind on the row
    assert row["sent_at"] is None and row["sent_evidence"] is None


def test_mark_sent_from_staged_succeeds(run, engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    engine_db.execute("UPDATE messages SET lint_report = ?, state = 'READY' "
                      "WHERE id = ?", (PASSING_LINT, message_id))
    engine_db.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?",
                      (message_id,))
    engine_db.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    engine_db.commit()
    assert run("mark-sent", str(message_id), "--evidence", "sent by hand") == cli.EXIT_OK
    row = engine_db.execute("SELECT state, sent_evidence FROM messages WHERE id = ?",
                            (message_id,)).fetchone()
    assert (row["state"], row["sent_evidence"]) == ("SENT", "sent by hand")


# --------------------------------------------------------------------------- #
# happy paths (exit 0)
# --------------------------------------------------------------------------- #

def test_sheet_prints_the_four_columns(run, engine_db, seed_graph, insert_message,
                                       capsys):
    sent_message(engine_db, seed_graph, insert_message)
    assert run("sheet") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "variant | sent_to | replied | booked" in out
    assert "(1 rows)" in out


def test_sheet_csv_export_writes_every_row(run, engine_db, seed_graph, insert_message,
                                           tmp_path):
    for index in range(3):
        sent_message(engine_db, seed_graph, insert_message, index)
    path = tmp_path / "sheet.csv"
    assert run("sheet", "--csv", str(path)) == cli.EXIT_OK
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert list(rows[0]) == ["variant", "sent_to", "replied", "booked"]


def test_empty_sheet_is_not_an_error(run, capsys):
    assert run("sheet") == cli.EXIT_OK
    assert "empty" in capsys.readouterr().out


def test_metrics_emit_writes_into_the_redirected_dir(run, engine_cfg, capsys,
                                                     tmp_path):
    assert run("metrics-emit") == cli.EXIT_OK
    written = list(engine_cfg.dashboard_metrics_dir.glob("pull-*.json"))
    assert len(written) == 1
    assert written[0].is_relative_to(tmp_path)
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert set(payload) == {"generatedAt", "metrics"}


def test_status_runs_on_an_empty_engine(run, capsys):
    assert run("status") == cli.EXIT_OK
    assert "reply-rate denominator" in capsys.readouterr().out


def test_record_reply_needs_a_human_at_the_terminal(run, engine_db, seed_graph,
                                                    insert_message, capsys):
    """Finding M5 — one CLI call used to manufacture the reply that unlocks G7."""
    ids = sent_message(engine_db, seed_graph, insert_message)
    assert run("record-reply", str(ids["prospect_id"]), "--channel", "email",
               "--class", "hot", "--snippet", "what does it cost?") == cli.EXIT_REFUSED
    assert "ingest-triage" in capsys.readouterr().err
    assert engine_db.execute("SELECT count(*) FROM replies").fetchone()[0] == 0


def test_record_reply_then_sheet_shows_the_reply(run, engine_db, seed_graph,
                                                 insert_message, capsys, monkeypatch):
    ids = sent_message(engine_db, seed_graph, insert_message)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    assert run("record-reply", str(ids["prospect_id"]), "--channel", "email",
               "--class", "hot", "--snippet", "what does it cost?") == cli.EXIT_OK
    capsys.readouterr()
    run("sheet")
    line = [l for l in capsys.readouterr().out.splitlines() if "o0@co0.com" in l][0]
    assert line.split(" | ")[2] == "y"


def test_record_reply_of_an_autoresponder_does_not_show_as_replied(
        run, engine_db, seed_graph, insert_message, capsys):
    ids = sent_message(engine_db, seed_graph, insert_message)
    assert run("record-reply", str(ids["prospect_id"]), "--channel", "email",
               "--class", "auto_reply", "--snippet", "out of office") == cli.EXIT_OK
    capsys.readouterr()
    run("sheet")
    line = [l for l in capsys.readouterr().out.splitlines() if "o0@co0.com" in l][0]
    assert line.split(" | ")[2] == "n"


def test_book_records_the_attribution_method(run, engine_db, seed_graph,
                                             insert_message):
    ids = sent_message(engine_db, seed_graph, insert_message)
    assert run("book", str(ids["prospect_id"]), "--method", "manual_confirm",
               "--evidence", "ghl-42") == cli.EXIT_OK
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert (row["attribution_method"], row["attribution_confidence"]) \
        == ("manual_confirm", "manual")


def test_attrib_reports_organic_contacts_separately(run, engine_db, seed_graph,
                                                    insert_message, tmp_path, capsys):
    sent_message(engine_db, seed_graph, insert_message)
    path = tmp_path / "ghl.json"
    path.write_text(json.dumps([
        {"id": "g1", "companyName": "Nobody", "website": "https://nobody.example",
         "dateAdded": "2026-08-03T00:00:00Z"}]), encoding="utf-8")
    assert run("attrib", "--ghl-export", str(path)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ghl contacts read=1" in out
    assert "unmatched(organic)=1" in out


def test_suppress_then_status(run, engine_db, capsys):
    assert run("suppress", "dead@example.com", "--reason", "bounce") == cli.EXIT_OK
    assert engine_db.execute(
        "SELECT count(*) FROM suppression WHERE address = 'dead@example.com'"
    ).fetchone()[0] == 1


def test_ingest_triage_from_a_real_shaped_file(run, engine_db, seed_graph,
                                               insert_message, tmp_path, capsys):
    ids = sent_message(engine_db, seed_graph, insert_message)
    path = tmp_path / "triage-2026-08-01-1100.json"
    path.write_text(json.dumps({
        "generatedAt": "2026-08-01T11:00:00Z",
        "items": [{"id": "thread-9", "kind": "reply-draft",
                   "from": f"Owner <{ids['address']}>", "company": "Co 0",
                   "classification": "question", "summary": "how does it work?",
                   "draftId": None,
                   "link": "https://mail.google.com/mail/u/0/#inbox/thread-9",
                   "suggested": "answer + bridge"}]}), encoding="utf-8")
    assert run("ingest-triage", str(path)) == cli.EXIT_OK
    assert "recorded from triage file: 1" in capsys.readouterr().out


def test_scan_replies_reports_auth_failure_as_exit_4(run, engine_cfg, capsys):
    """Test mode has no credentials; a headless run must exit 4, not crash."""
    code = run("scan-replies")
    assert code == cli.EXIT_AUTH
    assert "auth" in capsys.readouterr().err.lower()
    assert engine_cfg.pending_actions.exists(), \
        "an auth failure must land in the pending-actions ledger"
    assert "gmail_multi_auth" in engine_cfg.pending_actions.read_text(encoding="utf-8")


def test_run_rows_are_recorded_for_auditable_commands(run, engine_db, engine_cfg,
                                                      tmp_path):
    path = tmp_path / "ghl.json"
    path.write_text("[]", encoding="utf-8")
    run("attrib", "--ghl-export", str(path))
    row = engine_db.execute(
        "SELECT command, status, detail FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["command"] == "attrib"
    assert row["status"] == "ok"
    assert json.loads(row["detail"])["read"] == 0


# --------------------------------------------------------------------------- #
# call-throughs into the other owners' modules
# --------------------------------------------------------------------------- #

def test_stats_renders_owner_b_output_without_a_winner_at_low_n(
        run, engine_db, seed_graph, insert_message, capsys):
    ids = sent_message(engine_db, seed_graph, insert_message)
    replies.record_reply(engine_db, ids["prospect_id"], "email", "hot", "yes", "operator")
    assert run("stats", "--experiment", "EXP-TEST") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "%" not in out
    assert "SAMPLE TOO SMALL" in out


def test_stats_replies_prints_verbatim_snippets(run, engine_db, seed_graph,
                                                insert_message, capsys):
    ids = sent_message(engine_db, seed_graph, insert_message)
    replies.record_reply(engine_db, ids["prospect_id"], "email", "hot",
                         "we tried a chatbot and hated it", "operator")
    assert run("stats", "--experiment", "EXP-TEST", "--replies") == cli.EXIT_OK
    assert "we tried a chatbot and hated it" in capsys.readouterr().out


def test_variants_gate_refuses_below_min_n_with_exit_3(run, engine_db, capsys):
    """§4.4: no winner and no retirement on a coin-flip sample."""
    engine_db.execute("INSERT INTO experiments (id, name) VALUES ('EXP-G', 'g')")
    engine_db.execute(
        "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES ('V9.1', 'V9', 1, 'EXP-G', 'n', 'a', '{}')")
    engine_db.commit()
    for action in ("retire", "declare-winner"):
        assert run("variants", action, "V9.1") == cli.EXIT_GATE_ABORT
        assert "too small" in capsys.readouterr().err


def test_variants_force_is_allowed_but_leaves_a_permanent_audit_row(run, engine_db):
    engine_db.execute("INSERT INTO experiments (id, name) VALUES ('EXP-G', 'g')")
    engine_db.execute(
        "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES ('V9.1', 'V9', 1, 'EXP-G', 'n', 'a', '{}')")
    engine_db.commit()
    assert run("variants", "retire", "V9.1", "--force",
               "--reason", "copy was wrong") == cli.EXIT_OK
    row = engine_db.execute(
        "SELECT actor, reason FROM state_log WHERE entity = 'variant'").fetchone()
    assert (row["actor"], row["reason"]) == ("operator", "copy was wrong")


def test_unwired_modules_refuse_loudly_rather_than_crash(run, monkeypatch, capsys):
    """Whatever is still missing at integration must exit 2 with a named seam."""
    import importlib

    real = importlib.import_module

    def fake(name, package=None):
        if name == ".prospects":
            raise ImportError("simulated missing module")
        return real(name, package)

    monkeypatch.setattr(cli.importlib, "import_module", fake)
    assert run("qualify", "1") == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "NOT WIRED YET" in err and "prospects.py" in err
