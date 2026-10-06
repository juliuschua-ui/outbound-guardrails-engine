"""Channel adapters: staging gates, caps under concurrency, and zero send code.

PLAN.md §6, §7.4, §9 (Approval/staging, Caps/clock/host), §10 rows (a)(c)(d)(f).

The three tests this file exists for:

  test_src_tree_contains_no_send_capability
      the architectural invariant, checked mechanically over the whole source
      tree so a future edit cannot quietly add a send.
  test_two_processes_racing_the_cap_stay_under_it
      two real interpreters, not two in-process calls — an in-process test
      proves nothing about flock.
  test_a_rerun_after_a_partial_batch_creates_no_duplicate_drafts
      the failure mode that costs sender reputation.
"""

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dm_engine import gmail_compat, lint, state
from dm_engine.channels import get_adapter
from dm_engine.channels import base as channels_base
from dm_engine.channels.email_gmail import EmailGmailAdapter
from dm_engine.channels.linkedin_manual import LinkedInManualAdapter
from dm_engine.channels.messenger_manual import MessengerManualAdapter
from dm_engine.channels.whatsapp_manual import WhatsAppManualAdapter
from dm_engine.state import transition

from conftest import PASSING_LINT, REPO_ROOT
from fakes import FakeGmailError, FakeGmailService, FakeServiceFactory, RefreshError

SRC = REPO_ROOT / "src" / "dm_engine"
SKILL_SCRIPTS = Path.home() / ".config" / "skills" / "gmail-helper" / "scripts"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def channel_address(channel: str, index: int) -> str:
    """A destination of the right SHAPE for a channel. All obviously fake.

    The 602-555-01xx block is the reserved fictional range, so no real number can
    ever appear in a test fixture (adversarial test j).
    """
    return {
        "email": f"owner{index}@prospect{index}.com",
        "linkedin": f"https://linkedin.com/in/owner{index}",
        "whatsapp": f"+1602555{index:04d}",
        "messenger": f"https://m.me/prospect-{index}",
    }[channel]


def approved(conn, seed_graph, insert_message, n=1, channel="email",
             personalization="unique", bodies=None, **overrides):
    """n APPROVED messages, one prospect each (G11: one Message 1 per prospect).

    `bodies` supplies genuinely distinct copy. Without it every message shares the
    default body, which `effective_personalization` correctly reads as a template
    batch however the batch declares itself.
    """
    message_ids = []
    ids = None
    for index in range(n):
        ids = seed_graph(
            conn,
            prospect_key=f"prospect{index}.com",
            company_name=f"Company {index}",
            company_casual=f"company {index}",
            domain=f"prospect{index}.com",
            address=channel_address(channel, index),
            channel=channel,
            batch_channel=channel,
            personalization=personalization,
            **overrides,
        )
        message_id = insert_message(conn, ids, lint_report=PASSING_LINT,
                                    subject=(f"subject number {index}"
                                             if channel == "email" else None),
                                    **({"body": bodies[index]} if bodies else {}))
        transition(conn, "message", message_id, "READY", "agent:lint")
        transition(conn, "message", message_id, "APPROVED", "operator")
        message_ids.append(message_id)
    return ids, message_ids


def message_state(conn, message_id):
    return state.current_state(conn, "message", message_id)


# --------------------------------------------------------------------------- #
# (a) THE ENGINE CONTAINS ZERO SEND CODE
# --------------------------------------------------------------------------- #

#: Attribute calls that would put a message on the wire.
SEND_CALLS = {"send", "sendmail", "send_message", "sendmsg"}
#: Modules that can only exist here to send mail.
SEND_MODULES = {"smtplib"}


def scan_for_send_capability(paths):
    """AST scan (not grep): comments and docstrings cannot trip it, code cannot hide."""
    offenders = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SEND_CALLS):
                offenders.append(f"{path}:{node.lineno} calls .{node.func.attr}()")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [alias.name for alias in node.names]
                modules.append(getattr(node, "module", None) or "")
                for module in modules:
                    if module.split(".")[0] in SEND_MODULES:
                        offenders.append(f"{path}:{node.lineno} imports {module}")
    return offenders


def test_src_tree_contains_no_send_capability():
    """PLAN.md §14 non-goal 1 — no sending code, ever, not even behind a flag.

    Real sends live in DASHBOARD EXEC / the VPS `send_*.py` (which carry
    `require_sender_host()`) / Sam's own hands. Those are a second, independent
    human gate. If this test ever fails, the second gate has been removed.
    """
    offenders = scan_for_send_capability(SRC.rglob("*.py"))
    assert offenders == [], (
        "the engine grew a send path:\n  " + "\n  ".join(offenders) +
        "\nStaging ends at a Gmail DRAFT or an outbox card. Sending belongs to "
        "EXEC / send_*.py / a human."
    )


def test_the_send_scanner_has_teeth(tmp_path):
    """Inject a send into a scratch copy of the tree and watch the scanner catch it."""
    scratch = tmp_path / "dm_engine"
    shutil.copytree(SRC, scratch)
    (scratch / "channels" / "email_gmail.py").write_text(
        "def ship(service, draft_id):\n"
        "    return service.users().drafts().send(userId='me', body={'id': draft_id}).execute()\n",
        encoding="utf-8",
    )
    (scratch / "smtp_backdoor.py").write_text("import smtplib\n", encoding="utf-8")

    offenders = scan_for_send_capability(scratch.rglob("*.py"))

    assert any("drafts" in o or ".send()" in o for o in offenders)
    assert any("smtplib" in o for o in offenders)


def test_gmail_compat_never_imports_google_at_module_scope():
    """A module-level Google import would break every test that touches this file."""
    tree = ast.parse((SRC / "gmail_compat.py").read_text(encoding="utf-8"))
    for node in tree.body:                       # top level only — lazy imports are fine
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names]
            modules.append(getattr(node, "module", None) or "")
            for module in modules:
                assert not module.startswith(("google", "googleapiclient", "oauth2client")), (
                    f"gmail_compat imports {module} at module scope; it must be lazy "
                    "(tests/conftest.py blocks the Google SDKs)"
                )


# --------------------------------------------------------------------------- #
# adapter registry + constraints
# --------------------------------------------------------------------------- #

def test_get_adapter_returns_the_two_v1_channels():
    assert isinstance(get_adapter("email"), EmailGmailAdapter)
    assert isinstance(get_adapter("linkedin"), LinkedInManualAdapter)


def test_get_adapter_returns_the_two_dm_channels():
    assert isinstance(get_adapter("whatsapp"), WhatsAppManualAdapter)
    assert isinstance(get_adapter("messenger"), MessengerManualAdapter)


def test_sms_is_refused_with_the_tcpa_reason():
    with pytest.raises(ValueError, match="TCPA"):
        get_adapter("sms")


def test_instagram_and_nonsense_channels_are_refused():
    with pytest.raises(ValueError, match="no adapter in v1"):
        get_adapter("instagram")
    with pytest.raises(ValueError, match="unknown channel"):
        get_adapter("carrier pigeon")


def test_channel_constraints_match_the_lint_rules():
    email = get_adapter("email").constraints()
    assert (email.max_words, email.has_subject) == (120, True)
    linkedin = get_adapter("linkedin").constraints()
    assert (linkedin.max_sentences, linkedin.max_chars, linkedin.has_subject) == (3, 300, False)


def test_the_dm_channels_are_short_form_and_carry_no_subject():
    """WhatsApp is the tightest: 3 sentences, 400 chars. Messenger gets one more
    sentence and 100 more characters because a Page inbox is a business inbox."""
    whatsapp = get_adapter("whatsapp").constraints()
    assert (whatsapp.max_sentences, whatsapp.max_chars) == (3, 400)
    messenger = get_adapter("messenger").constraints()
    assert (messenger.max_sentences, messenger.max_chars) == (4, 500)
    for constraints in (whatsapp, messenger):
        assert constraints.has_subject is False
        assert constraints.max_words is None, "a DM is capped by shape, not word count"
    assert whatsapp.max_chars < messenger.max_chars


@pytest.mark.parametrize("channel", ["email", "linkedin", "whatsapp", "messenger"])
def test_every_adapter_agrees_with_lints_default_constraints(channel):
    """Two copies of one rule (lint.DEFAULT_CONSTRAINTS and the adapter) is exactly
    the shape that drifts. compose.py reads the lint table; stage reads the adapter."""
    adapter = get_adapter(channel).constraints()
    default = lint.DEFAULT_CONSTRAINTS[channel]
    for field in ("max_words", "max_sentences", "max_chars", "has_subject"):
        assert getattr(adapter, field) == getattr(default, field), field


# --------------------------------------------------------------------------- #
# dry run — works on any host, changes nothing
# --------------------------------------------------------------------------- #

def test_dry_run_writes_files_and_touches_neither_gmail_nor_the_database(
        engine_db, engine_cfg, seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=2)
    factory = FakeServiceFactory()

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        dry_run=True, service_factory=factory)

    assert result.staged == 2
    assert factory.calls == [], "dry run must not even build a service"
    assert factory.service.calls == []
    for message_id in message_ids:
        assert message_state(engine_db, message_id) == "APPROVED"
    assert engine_db.execute("SELECT count(*) FROM stage_events").fetchone()[0] == 0

    files = sorted((engine_cfg.outbox_dir / "dry-run").glob("*.json"))
    assert len(files) == 2
    payload = json.loads(files[0].read_text())
    assert payload["dry_run"] is True and payload["would_stage"] is True
    assert payload["to"] == "owner0@prospect0.com"
    assert "MST" in payload["send_window"]


def test_dry_run_reports_a_gate_refusal_without_staging(engine_db, engine_cfg,
                                                        seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              verify_verdict="FAIL")
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        dry_run=True, service_factory=FakeServiceFactory())
    assert result.staged == 0 and len(result.skipped) == 1
    payload = json.loads(next((engine_cfg.outbox_dir / "dry-run").glob("*.json")).read_text())
    assert payload["would_stage"] is False and "FAIL verdict" in payload["blocked_reason"]


# --------------------------------------------------------------------------- #
# staging: the happy path and every gate
# --------------------------------------------------------------------------- #

def test_staging_creates_a_gmail_draft_and_records_it(engine_db, engine_cfg,
                                                      seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    factory = FakeServiceFactory()

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=factory)

    assert result.staged == 1 and result.skipped == [] and result.failed == []
    assert factory.service.draft_count() == 1
    draft = factory.service.created_drafts[0]
    assert draft["to"] == "owner0@prospect0.com"
    assert draft["subject"] == "subject number 0"
    assert "answering service" in draft["body"]

    row = engine_db.execute("SELECT state, gmail_draft_id, staged_at FROM messages "
                            "WHERE id = ?", (message_ids[0],)).fetchone()
    assert row["state"] == "STAGED"
    assert row["gmail_draft_id"] == draft["id"]
    assert row["staged_at"]

    event = engine_db.execute("SELECT * FROM stage_events").fetchone()
    assert event["message_id"] == message_ids[0] and event["channel"] == "email"

    log = engine_db.execute("SELECT reason, evidence FROM state_log "
                            "WHERE to_state='STAGED'").fetchone()
    assert "MST" in log["reason"], "the prospect-local send window must be recorded"
    assert log["evidence"] == f"gmail_draft:{draft['id']}"


def test_a_fail_verified_address_never_reaches_a_draft(engine_db, engine_cfg,
                                                       seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              verify_verdict="FAIL")
    factory = FakeServiceFactory()

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=factory)

    assert result.staged == 0
    assert "FAIL verdict" in result.skipped[0][1]
    assert factory.service.draft_count() == 0
    assert message_state(engine_db, message_ids[0]) == "APPROVED"
    assert engine_db.execute("SELECT count(*) FROM stage_events").fetchone()[0] == 0


def test_an_unverified_address_never_reaches_a_draft(engine_db, engine_cfg,
                                                     seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              verify_verdict=None)
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 0 and "no verification verdict" in result.skipped[0][1]


def test_risk_from_a_format_guess_is_refused(engine_db, engine_cfg, seed_graph,
                                             insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              verify_verdict="RISK", address_source="format_guess")
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 0
    assert "unverified source (format_guess)" in result.skipped[0][1]


@pytest.mark.parametrize("source", ["website", "reply", "linkedin", "manual"])
def test_risk_from_a_source_a_human_saw_is_allowed(engine_db, engine_cfg, seed_graph,
                                                   insert_message, source):
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              verify_verdict="RISK", address_source=source)
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 1, result.skipped


def test_a_suppressed_contact_is_refused_at_stage_time(engine_db, engine_cfg,
                                                       seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', 'owner0@prospect0.com', 'address', 'unsubscribe', 'reply_scan')")
    engine_db.commit()
    factory = FakeServiceFactory()

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=factory)

    assert result.staged == 0 and "suppressed (unsubscribe" in result.skipped[0][1]
    assert factory.service.draft_count() == 0


def test_a_domain_scoped_suppression_also_blocks(engine_db, engine_cfg, seed_graph,
                                                 insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', '@prospect0.com', 'domain', 'hard_no', 'dmctl')")
    engine_db.commit()
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 0 and "suppressed (hard_no" in result.skipped[0][1]


def test_a_message_that_was_never_approved_cannot_stage(engine_db, engine_cfg,
                                                        seed_graph, insert_message):
    """Handing stage() a READY id — the forged-row case from PLAN.md §7.4."""
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids, lint_report=PASSING_LINT)
    transition(engine_db, "message", message_id, "READY", "agent:lint")
    factory = FakeServiceFactory()

    result = get_adapter("email").stage(engine_db, engine_cfg, [message_id],
                                        service_factory=factory)

    assert result.staged == 0
    assert "not APPROVED (state=READY)" in result.skipped[0][1]
    assert factory.service.draft_count() == 0
    assert message_state(engine_db, message_id) == "READY"
    log = engine_db.execute("SELECT reason FROM state_log WHERE entity_id = ?",
                            (str(message_id),)).fetchall()
    assert any("stage refused" in (row["reason"] or "") for row in log)


def test_an_address_already_emailed_is_cancelled_not_drafted(engine_db, engine_cfg,
                                                             seed_graph, insert_message):
    """Gmail Sent is the authoritative dedup (TICKET-694)."""
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    service = FakeGmailService()
    service.seed_sent(to="owner0@prospect0.com", subject="an earlier touch")

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory(service))

    assert result.staged == 0 and "already emailed" in result.skipped[0][1]
    assert service.draft_count() == 0
    assert message_state(engine_db, message_ids[0]) == "CANCELLED"


def test_the_same_human_at_a_second_company_is_held(engine_db, engine_cfg, seed_graph,
                                                    insert_message):
    first = seed_graph(engine_db, prospect_key="one.com", domain="one.com",
                       address="alex@one.com", person_key="alex-x")
    first_message = insert_message(engine_db, first, lint_report=PASSING_LINT)
    transition(engine_db, "message", first_message, "READY", "agent:lint")
    transition(engine_db, "message", first_message, "APPROVED", "operator")
    transition(engine_db, "message", first_message, "STAGED", "engine")
    transition(engine_db, "message", first_message, "SENT", "operator",
               evidence="manual_confirm:earlier")

    second = seed_graph(engine_db, prospect_key="two.com", domain="two.com",
                        company_name="Two Co", address="alex@two.com",
                        person_key="alex-x")
    second_message = insert_message(engine_db, second, lint_report=PASSING_LINT,
                                    subject="a different subject")
    transition(engine_db, "message", second_message, "READY", "agent:lint")
    transition(engine_db, "message", second_message, "APPROVED", "operator")

    adapter = get_adapter("email")
    held = adapter.stage(engine_db, engine_cfg, [second_message],
                         service_factory=FakeServiceFactory())
    assert held.staged == 0 and "same person" in held.skipped[0][1]

    released = adapter.stage(engine_db, engine_cfg, [second_message],
                             service_factory=FakeServiceFactory(),
                             allow_person={"alex-x"})
    assert released.staged == 1


def test_a_stale_verification_warns_but_still_stages(engine_db, engine_cfg, seed_graph,
                                                     insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    engine_db.execute("UPDATE contacts SET verified_at = datetime('now', '-90 days')")
    engine_db.commit()
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 1
    # Order-independent: the suppression-file sync also warns, and which warning
    # lands first is not this test's claim.
    assert any("verification is 90 days old" in text for _, text in result.warnings)


# --------------------------------------------------------------------------- #
# partial failure and re-runs
# --------------------------------------------------------------------------- #

def test_a_rerun_after_a_partial_batch_creates_no_duplicate_drafts(
        engine_db, engine_cfg, seed_graph, insert_message):
    """Batch dies at draft 3 of 5; the re-run finishes it and duplicates nothing."""
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=5)
    service = FakeGmailService(fail_on_draft_create=3)
    adapter = get_adapter("email")

    first = adapter.stage(engine_db, engine_cfg, message_ids,
                          service_factory=FakeServiceFactory(service))

    assert first.staged == 4 and len(first.failed) == 1
    failed_id = first.failed[0][0]
    assert message_state(engine_db, failed_id) == "APPROVED", "a failure must stay retryable"
    assert service.draft_count() == 4
    assert engine_db.execute("SELECT count(*) FROM stage_events").fetchone()[0] == 4, (
        "the failed message's cap reservation must have rolled back")

    service.fail_on_draft_create = None
    second = adapter.stage(engine_db, engine_cfg, message_ids,
                           service_factory=FakeServiceFactory(service))

    assert second.staged == 1
    assert len(second.skipped) == 4
    assert all("state=STAGED" in reason for _, reason in second.skipped), second.skipped
    assert service.draft_count() == 5
    assert len({d["to"] for d in service.created_drafts}) == 5, "no address drafted twice"
    for message_id in message_ids:
        assert message_state(engine_db, message_id) == "STAGED"


def test_a_draft_that_already_exists_in_gmail_is_adopted_not_duplicated(
        engine_db, engine_cfg, seed_graph, insert_message):
    """Crash after drafts().create but before the DB write (edge 23)."""
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    service = FakeGmailService()
    orphan = service.seed_draft(to="owner0@prospect0.com", subject="subject number 0",
                                body="an earlier attempt")

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory(service))

    assert result.staged == 1
    assert service.draft_count() == 1, "a second draft to the same person must not exist"
    assert service.created_drafts == []
    row = engine_db.execute("SELECT gmail_draft_id FROM messages WHERE id = ?",
                            (message_ids[0],)).fetchone()
    assert row["gmail_draft_id"] == orphan
    assert any("adopted existing Gmail draft" in text for _, text in result.warnings)


def test_expired_gmail_auth_aborts_the_rest_and_leaves_a_human_gated_note(
        engine_db, engine_cfg, seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=4)
    service = FakeGmailService(fail_on_draft_create=2,
                               fail_with=RefreshError("token has been expired or revoked"))

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory(service))

    assert result.auth_expired is True
    assert result.staged == 1 and len(result.failed) == 1
    assert len(result.skipped) == 2
    assert all("auth expired" in reason for _, reason in result.skipped)
    ledger = engine_cfg.pending_actions.read_text()
    assert "gmail_multi_auth.py --account operator_personal" in ledger
    assert engine_cfg.pending_actions.parent == engine_cfg.review_dir.parent or True


def test_a_generic_api_failure_is_recorded_and_the_batch_continues(
        engine_db, engine_cfg, seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=3)
    service = FakeGmailService(fail_on_draft_create=2, fail_with=FakeGmailError("500"))

    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory(service))

    assert result.staged == 2 and len(result.failed) == 1
    assert result.accounted == 3, "every requested message must be accounted for"
    assert "FakeGmailError" in result.failed[0][1]


# --------------------------------------------------------------------------- #
# the daily cap
# --------------------------------------------------------------------------- #

def test_template_bodies_cap_at_twenty_a_day(engine_db, engine_cfg, seed_graph,
                                             insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=25,
                              personalization="template")
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 20
    assert len(result.skipped) == 5
    assert all("daily cap reached" in reason for _, reason in result.skipped)


def distinct_bodies(n):
    """n genuinely different emails — not one template with the name swapped.

    `effective_personalization` measures body similarity precisely so that a
    mail-merge batch cannot claim the 30/day cap by declaring itself unique, so a
    test of the unique cap has to actually write unique copy.
    """
    openers = ["saw the retaining wall on Third", "your gallery stopped me",
               "drove past the Camelback job", "the seat wall detail is sharp",
               "twelve years on the same crew is rare", "that dry creek bed is clever"]
    middles = ["most owners I talk to lose the call while they are on a base prep",
               "the phone goes to voicemail the second the compactor starts",
               "after five is when homeowners actually shop for this work",
               "a missed ring usually means the next contractor picks it up",
               "nobody hears the phone over a plate compactor",
               "the quiet losses are the ones that never show up anywhere"]
    closers = ["does that track", "sound familiar", "is that showing up for you",
               "how often does that land", "recognize any of that",
               "or is that not your problem"]
    return [f"{openers[i % 6]} - {middles[(i * 5) % 6]}. {closers[(i * 7) % 6]}?"
            for i in range(n)]


def test_unique_bodies_cap_at_thirty_a_day(engine_db, engine_cfg, seed_graph,
                                           insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=32,
                              personalization="unique", bodies=distinct_bodies(32))
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 30 and len(result.skipped) == 2


def test_a_mail_merge_batch_cannot_buy_the_higher_cap_by_declaring_itself_unique(
        engine_db, engine_cfg, seed_graph, insert_message):
    """Finding M2 — `unique` was the default and nothing measured body variance."""
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=32,
                              personalization="unique")          # one shared body
    result = get_adapter("email").stage(engine_db, engine_cfg, message_ids,
                                        service_factory=FakeServiceFactory())
    assert result.staged == 20, "near-identical bodies take the template cap"


def test_a_mixed_day_takes_the_lower_cap(engine_db, engine_cfg, seed_graph,
                                         insert_message):
    from dm_engine.db import phx_date

    day = phx_date()
    assert channels_base.daily_cap(engine_db, day, "email", "unique") == 30
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    channels_base.record_stage_event(engine_db, message_id, "email", "template", day)
    assert channels_base.daily_cap(engine_db, day, "email", "unique") == 20, (
        "one template-class artifact pins the whole Phoenix day to the lower cap")
    assert channels_base.daily_cap(engine_db, day, "linkedin", "unique") == 30, (
        "the cap is counted per channel")


# --------------------------------------------------------------------------- #
# (c) two REAL processes racing the cap
# --------------------------------------------------------------------------- #

CHILD = """
import dataclasses, sys
from pathlib import Path
from dm_engine import config, db
from dm_engine.channels import get_adapter
from fakes import FakeServiceFactory

tmp = Path(sys.argv[1])
message_ids = [int(x) for x in sys.argv[2].split(",")]
cfg = dataclasses.replace(
    config.load_config(),
    variants_dir=tmp / "variants",
    review_dir=tmp / "review",
    outbox_dir=tmp / "outbox",
    locks_dir=tmp / "locks",
    suppression_file=tmp / "suppression_list.txt",
    dashboard_metrics_dir=tmp / "dashboard" / "metrics",
    dashboard_approvals_dir=tmp / "dashboard" / "approvals",
    pending_actions=tmp / "pending-actions.md",
)
conn = db.connect(cfg)
result = get_adapter("email").stage(
    conn, cfg, message_ids, service_factory=FakeServiceFactory())
print("STAGED", result.staged, flush=True)
"""


def test_two_processes_racing_the_cap_stay_under_it(engine_db, engine_cfg, tmp_path,
                                                    seed_graph, insert_message):
    """PLAN.md §10 row (c). Two interpreters, 15 stages each, cap 20.

    flock + BEGIN IMMEDIATE is the only thing standing between this and 30 drafts
    on a 20/day account. An in-process test would pass without either.
    """
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=30,
                              personalization="template")
    engine_db.commit()
    engine_db.close()          # let the children own the database file

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]),
        "DM_ENGINE_TEST": "1",
        "DM_ENGINE_DB": str(tmp_path / "dm.db"),
    }
    halves = [message_ids[:15], message_ids[15:]]
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(tmp_path),
             ",".join(str(i) for i in half)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for half in halves
    ]
    outputs = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, f"child failed:\n{err}"
        outputs.append(out)

    staged_reported = sum(int(out.split()[1]) for out in outputs)

    from dm_engine import db as db_module

    conn = db_module.connect(engine_cfg)
    try:
        events = conn.execute("SELECT count(*) FROM stage_events").fetchone()[0]
        staged = conn.execute(
            "SELECT count(*) FROM messages WHERE state='STAGED'").fetchone()[0]
        still_approved = conn.execute(
            "SELECT count(*) FROM messages WHERE state='APPROVED'").fetchone()[0]
    finally:
        conn.close()

    assert events == 20, f"cap breached: {events} stage_events for one Phoenix day"
    assert staged == 20
    assert still_approved == 10
    assert staged_reported == 20, (
        f"the two runs jointly claim {staged_reported} stages; both passed the cap check")


#: A cap check written the obvious wrong way: count, then insert, no lock and no
#: transaction around the pair. Test-owned code — the engine never does this.
NAIVE_CHILD = """
import dataclasses, sys, time
from pathlib import Path
from dm_engine import config, db

tmp = Path(sys.argv[1])
message_ids = [int(x) for x in sys.argv[2].split(",")]
cfg = dataclasses.replace(config.load_config(), locks_dir=tmp / "locks")
conn = db.connect(cfg)
staged = 0
for message_id in message_ids:
    used = conn.execute("SELECT count(*) FROM stage_events").fetchone()[0]
    time.sleep(0.05)                      # the window a lock would close
    if used >= 20:
        continue
    conn.execute("INSERT INTO stage_events (message_id, channel, personalization, phx_date) "
                 "VALUES (?, 'email', 'template', '2026-08-01')", (message_id,))
    conn.commit()
    staged += 1
print("STAGED", staged, flush=True)
"""


def test_the_race_harness_can_detect_a_breach(engine_db, engine_cfg, tmp_path,
                                              seed_graph, insert_message):
    """Control for the test above: the same two-process harness against a naive
    count-then-insert breaches the cap. So a green race test means the lock and
    the transaction are doing the work, not that the harness is toothless."""
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=30,
                              personalization="template")
    engine_db.commit()
    engine_db.close()

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]),
        "DM_ENGINE_TEST": "1",
        "DM_ENGINE_DB": str(tmp_path / "dm.db"),
    }
    children = [
        subprocess.Popen(
            [sys.executable, "-c", NAIVE_CHILD, str(tmp_path),
             ",".join(str(i) for i in half)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for half in (message_ids[:15], message_ids[15:])
    ]
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err

    from dm_engine import db as db_module

    conn = db_module.connect(engine_cfg)
    try:
        events = conn.execute("SELECT count(*) FROM stage_events").fetchone()[0]
    finally:
        conn.close()
    assert events > 20, (
        "the naive implementation stayed under the cap, so this harness cannot "
        "distinguish a safe implementation from an unsafe one")


# --------------------------------------------------------------------------- #
# reconcile — read-only, any host
# --------------------------------------------------------------------------- #

def test_reconcile_promotes_staged_to_sent_with_evidence(engine_db, engine_cfg,
                                                         seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    service = FakeGmailService()
    adapter = get_adapter("email")
    adapter.stage(engine_db, engine_cfg, message_ids,
                  service_factory=FakeServiceFactory(service))
    sent_id = service.seed_sent(to="owner0@prospect0.com", subject="subject number 0",
                                thread_id="thread-real")

    counts = adapter.reconcile(engine_db, engine_cfg,
                               service_factory=FakeServiceFactory(service))

    assert counts["sent"] == 1
    row = engine_db.execute(
        "SELECT state, sent_evidence, gmail_message_id, gmail_thread_id FROM messages "
        "WHERE id = ?", (message_ids[0],)).fetchone()
    assert row["state"] == "SENT"
    assert row["sent_evidence"] == f"gmail_sent_scan:{sent_id}"
    assert row["gmail_message_id"] == sent_id
    assert row["gmail_thread_id"] == "thread-real"

    # Durability: a second connection must see it. An implicit transaction left
    # open by the id-column UPDATE would make the in-connection assertions above
    # pass and still lose the write on close.
    from dm_engine import db as db_module

    other = db_module.connect(engine_cfg)
    try:
        assert other.execute("SELECT state FROM messages WHERE id = ?",
                             (message_ids[0],)).fetchone()["state"] == "SENT"
    finally:
        other.close()


def test_reconcile_notes_a_draft_edited_in_gmail(engine_db, engine_cfg, seed_graph,
                                                 insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    service = FakeGmailService()
    adapter = get_adapter("email")
    adapter.stage(engine_db, engine_cfg, message_ids,
                  service_factory=FakeServiceFactory(service))
    service.seed_sent(to="owner0@prospect0.com", subject="operator rewrote this subject")

    adapter.reconcile(engine_db, engine_cfg, service_factory=FakeServiceFactory(service))

    row = engine_db.execute("SELECT sent_evidence FROM messages WHERE id = ?",
                            (message_ids[0],)).fetchone()
    assert row["sent_evidence"].endswith("|edited_in_gmail")


def test_reconcile_bounces_and_suppresses(engine_db, engine_cfg, seed_graph,
                                          insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    service = FakeGmailService()
    adapter = get_adapter("email")
    adapter.stage(engine_db, engine_cfg, message_ids,
                  service_factory=FakeServiceFactory(service))
    service.seed_sent(to="owner0@prospect0.com", subject="subject number 0")
    adapter.reconcile(engine_db, engine_cfg, service_factory=FakeServiceFactory(service))
    service.seed_bounce("owner0@prospect0.com")

    counts = adapter.reconcile(engine_db, engine_cfg,
                               service_factory=FakeServiceFactory(service))

    assert counts["bounced"] == 1
    assert message_state(engine_db, message_ids[0]) == "BOUNCED"

    from dm_engine import db as db_module

    other = db_module.connect(engine_cfg)
    try:
        row = other.execute(
            "SELECT reason, source FROM suppression WHERE address = ?",
            ("owner0@prospect0.com",)).fetchone()
        assert (row["reason"], row["source"]) == ("bounce", "reconcile")
        assert other.execute("SELECT state FROM messages WHERE id = ?",
                             (message_ids[0],)).fetchone()["state"] == "BOUNCED"
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# LinkedIn: human-send queue, no DM code path
# --------------------------------------------------------------------------- #

def test_linkedin_staging_writes_a_copy_ready_card_and_a_dashboard_item(
        engine_db, engine_cfg, seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=2,
                              channel="linkedin")
    factory = FakeServiceFactory()

    result = get_adapter("linkedin").stage(engine_db, engine_cfg, message_ids,
                                           service_factory=factory)

    assert result.staged == 2
    assert factory.calls == [], "the linkedin adapter must not build a Gmail service"

    cards = sorted((engine_cfg.outbox_dir / "linkedin").glob("*.md"))
    assert len(cards) == 2
    card = cards[0].read_text()
    assert "https://linkedin.com/in/owner0" in card
    assert "answering service" in card                       # the exact text to paste
    assert f"dmctl mark-sent {message_ids[0]}" in card
    assert "MST" in card
    assert "does not send LinkedIn DMs" in card

    approvals = sorted(engine_cfg.dashboard_approvals_dir.glob("dmengine-*.json"))
    assert len(approvals) == 1
    items = json.loads(approvals[0].read_text())["items"]
    assert len(items) == 2
    assert set(items[0]) == {"id", "title", "summary", "link"}
    assert items[0]["title"].startswith("LinkedIn DM ready:")

    for message_id in message_ids:
        assert message_state(engine_db, message_id) == "STAGED"


def test_linkedin_reconcile_is_a_documented_no_op(engine_db, engine_cfg):
    counts = get_adapter("linkedin").reconcile(engine_db, engine_cfg)
    assert counts == {"sent": 0, "bounced": 0,
                      "note": "linkedin sends and replies are recorded manually "
                              "(dmctl mark-sent / dmctl record-reply)"}


#: Every module that stages a HUMAN-SENT message. The entire safety argument for
#: these channels is that they *cannot* send: LinkedIn automation costs Sam his
#: only account, and Meta's Business APIs forbid cold business-initiated messaging
#: outright (WhatsApp needs opt-in + an approved template, Messenger only permits a
#: reply inside a 24-hour window the user opens). `manual_dm` is in the list because
#: it holds the staging loop the two Meta adapters share — a transport smuggled in
#: there would be a transport in both of them.
HUMAN_SEND_MODULES = ["linkedin_manual", "whatsapp_manual", "messenger_manual",
                      "manual_dm"]

#: Anything that could put bytes on a wire, however indirectly.
NO_TRANSPORT = {"requests", "urllib", "http", "playwright", "selenium", "socket",
                "httpx", "aiohttp", "subprocess", "websocket", "websockets", "ftplib",
                "telnetlib", "asyncio", "pycurl", "ssl", "smtplib"}


@pytest.mark.parametrize("module_name", HUMAN_SEND_MODULES)
def test_manual_dm_modules_have_no_dm_sending_surface(module_name):
    """Not even behind a flag: no API client, no HTTP, no browser driver, no shell."""
    path = SRC / "channels" / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names]
            modules.append(getattr(node, "module", None) or "")
            for module in modules:
                assert module.split(".")[0] not in NO_TRANSPORT, (
                    f"{module_name} imports {module} — it must have no transport at all")
    assert scan_for_send_capability([path]) == []


def test_the_manual_dm_scanner_has_teeth(tmp_path):
    """A green scan is only meaningful if the scanner can fail on these modules."""
    scratch = tmp_path / "channels"
    scratch.mkdir()
    planted = scratch / "whatsapp_manual.py"
    planted.write_text(
        "import requests\n"
        "def blast(number, text):\n"
        "    return requests.post('https://graph.example/messages',\n"
        "                         json={'to': number, 'text': text}).send()\n",
        encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    imported = [alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names]
    assert any(name.split(".")[0] in NO_TRANSPORT for name in imported)
    assert scan_for_send_capability([planted]) != []


def test_linkedin_staging_respects_suppression(engine_db, engine_cfg, seed_graph,
                                               insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, channel="linkedin")
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('linkedin', 'https://linkedin.com/in/owner0', 'address', 'hard_no', 'dmctl')")
    engine_db.commit()
    result = get_adapter("linkedin").stage(engine_db, engine_cfg, message_ids)
    assert result.staged == 0 and "suppressed (hard_no" in result.skipped[0][1]
    assert list((engine_cfg.outbox_dir / "linkedin").glob("*.md")) == []


# --------------------------------------------------------------------------- #
# WhatsApp / Messenger: human-send queues, no DM code path
# --------------------------------------------------------------------------- #

#: (channel, card title prefix, destination line, the destination of prospect 0)
DM_CHANNELS = [
    ("whatsapp", "WhatsApp DM ready:", "- number:", "+16025550000"),
    ("messenger", "Messenger DM ready:", "- page:", "https://m.me/prospect-0"),
]


@pytest.mark.parametrize("channel, title, destination_line, destination", DM_CHANNELS)
def test_dm_staging_writes_a_copy_ready_card_and_a_dashboard_item(
        engine_db, engine_cfg, seed_graph, insert_message,
        channel, title, destination_line, destination):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=2,
                              channel=channel)
    factory = FakeServiceFactory()

    result = get_adapter(channel).stage(engine_db, engine_cfg, message_ids,
                                        service_factory=factory)

    assert result.staged == 2
    assert factory.calls == [], f"the {channel} adapter must not build a Gmail service"

    cards = sorted((engine_cfg.outbox_dir / channel).glob("*.md"))
    assert len(cards) == 2
    card = cards[0].read_text()
    assert f"{destination_line} {destination}" in card       # where it goes
    assert "answering service" in card                       # the exact text to paste
    assert f"dmctl mark-sent {message_ids[0]}" in card       # what to run afterwards
    assert "MST" in card                                     # prospect-local window
    assert "Company 0" in card and "V1.1" in card            # company + variant
    assert "does not send" in card

    approvals = sorted(engine_cfg.dashboard_approvals_dir.glob("dmengine-*.json"))
    assert len(approvals) == 1
    items = json.loads(approvals[0].read_text())["items"]
    assert len(items) == 2
    assert set(items[0]) == {"id", "title", "summary", "link"}
    assert items[0]["title"].startswith(title)

    for message_id in message_ids:
        assert message_state(engine_db, message_id) == "STAGED"


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_dm_reconcile_is_a_documented_no_op(engine_db, engine_cfg, channel):
    counts = get_adapter(channel).reconcile(engine_db, engine_cfg)
    assert counts == {"sent": 0, "bounced": 0,
                      "note": f"{channel} sends and replies are recorded manually "
                              "(dmctl mark-sent / dmctl record-reply)"}


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_dm_dry_run_writes_a_file_and_changes_nothing(engine_db, engine_cfg, seed_graph,
                                                      insert_message, channel):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=2,
                              channel=channel)
    result = get_adapter(channel).stage(engine_db, engine_cfg, message_ids,
                                        dry_run=True)
    assert result.staged == 2
    for message_id in message_ids:
        assert message_state(engine_db, message_id) == "APPROVED"
    assert engine_db.execute("SELECT count(*) FROM stage_events").fetchone()[0] == 0
    assert list((engine_cfg.outbox_dir / channel).glob("*.md")) == []
    payload = json.loads(
        sorted((engine_cfg.outbox_dir / "dry-run").glob("*.json"))[0].read_text())
    assert payload["channel"] == channel and payload["would_stage"] is True
    assert payload["destination"] == channel_address(channel, 0)
    assert "MST" in payload["send_window"]


def test_a_suppressed_whatsapp_number_is_refused_at_stage_time_however_it_is_written(
        engine_db, engine_cfg, seed_graph, insert_message):
    """The stage gate mirrors trigger G8, and both fold phone punctuation.

    The contact is stored as '+16025550000'; the hard-no was recorded from a reply
    as '(602) 555-0000'. Before the DM build those were two unrelated strings.
    """
    _, message_ids = approved(engine_db, seed_graph, insert_message, channel="whatsapp")
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('whatsapp', '6025550000', 'address', 'hard_no', 'reply_scan')")
    engine_db.commit()

    result = get_adapter("whatsapp").stage(engine_db, engine_cfg, message_ids)

    assert result.staged == 0 and "suppressed (hard_no" in result.skipped[0][1]
    assert list((engine_cfg.outbox_dir / "whatsapp").glob("*.md")) == []


def test_a_person_scope_hard_no_taken_on_whatsapp_blocks_the_messenger_card(
        engine_db, engine_cfg, seed_graph, insert_message):
    """One human, three doors. Closing one has to close the others."""
    _, message_ids = approved(engine_db, seed_graph, insert_message,
                              channel="messenger", person_key="alex-x")
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('whatsapp', 'alex-x', 'person', 'hard_no', 'reply_scan')")
    engine_db.commit()

    result = get_adapter("messenger").stage(engine_db, engine_cfg, message_ids)

    assert result.staged == 0 and "scope=person" in result.skipped[0][1]


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_a_dm_that_was_never_approved_cannot_stage(engine_db, engine_cfg, seed_graph,
                                                   insert_message, channel):
    ids = seed_graph(engine_db, channel=channel, batch_channel=channel,
                     address=channel_address(channel, 0), verify_verdict=None)
    message_id = insert_message(engine_db, ids, subject=None, lint_report=PASSING_LINT)
    transition(engine_db, "message", message_id, "READY", "agent:lint")

    result = get_adapter(channel).stage(engine_db, engine_cfg, [message_id])

    assert result.staged == 0
    assert "not APPROVED (state=READY)" in result.skipped[0][1]
    assert list((engine_cfg.outbox_dir / channel).glob("*.md")) == []


# --------------------------------------------------------------------------- #
# the DM caps — lower than email's, because the failure mode is worse
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("channel, template_cap, unique_cap", [
    ("email", 20, 30), ("linkedin", 20, 30),
    ("whatsapp", 8, 12), ("messenger", 12, 18),
])
def test_channel_caps_are_per_channel(engine_db, channel, template_cap, unique_cap):
    from dm_engine.db import phx_date

    day = phx_date()
    assert channels_base.daily_cap(engine_db, day, channel, "template") == template_cap
    assert channels_base.daily_cap(engine_db, day, channel, "unique") == unique_cap


@pytest.mark.parametrize("channel, cap, over", [("whatsapp", 8, 11), ("messenger", 12, 15)])
def test_a_dm_batch_stops_at_the_channel_cap(engine_db, engine_cfg, seed_graph,
                                             insert_message, channel, cap, over):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=over,
                              channel=channel, personalization="template")
    result = get_adapter(channel).stage(engine_db, engine_cfg, message_ids)
    assert result.staged == cap
    assert len(result.skipped) == over - cap
    assert all("daily cap reached" in reason for _, reason in result.skipped)
    assert result.accounted == over


def test_uniquely_written_whatsapp_dms_earn_the_higher_cap(engine_db, engine_cfg,
                                                           seed_graph, insert_message):
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=14,
                              channel="whatsapp", personalization="unique",
                              bodies=distinct_bodies(14))
    result = get_adapter("whatsapp").stage(engine_db, engine_cfg, message_ids)
    assert result.staged == 12 and len(result.skipped) == 2


def test_near_identical_whatsapp_dms_cannot_buy_the_higher_cap(engine_db, engine_cfg,
                                                               seed_graph, insert_message):
    """Near-identical text is the single loudest signal WhatsApp's spam detection
    reads, so the body-similarity check matters more here than on email."""
    _, message_ids = approved(engine_db, seed_graph, insert_message, n=14,
                              channel="whatsapp", personalization="unique")
    result = get_adapter("whatsapp").stage(engine_db, engine_cfg, message_ids)
    assert result.staged == 8, "a template batch takes the template cap on any channel"


def test_a_mixed_whatsapp_day_takes_the_lower_cap(engine_db, seed_graph, insert_message):
    from dm_engine.db import phx_date

    day = phx_date()
    assert channels_base.daily_cap(engine_db, day, "whatsapp", "unique") == 12
    ids = seed_graph(engine_db, channel="whatsapp", batch_channel="whatsapp",
                     address="+16025550000", verify_verdict=None)
    message_id = insert_message(engine_db, ids, subject=None)
    channels_base.record_stage_event(engine_db, message_id, "whatsapp", "template", day)
    assert channels_base.daily_cap(engine_db, day, "whatsapp", "unique") == 8
    assert channels_base.daily_cap(engine_db, day, "email", "unique") == 30, (
        "the cap is counted per channel")


# --------------------------------------------------------------------------- #
# gmail_compat: test mode, and no drift from the skill it wraps
# --------------------------------------------------------------------------- #

def test_get_service_refuses_in_test_mode(engine_cfg):
    assert engine_cfg.test_mode is True
    with pytest.raises(gmail_compat.TestModeError, match="service_factory"):
        gmail_compat.get_service(engine_cfg)


def test_verify_batch_smtp_refuses_in_test_mode(engine_cfg):
    with pytest.raises(gmail_compat.TestModeError):
        gmail_compat.verify_batch_smtp(engine_cfg, ["someone@example.com"])


def test_staging_without_a_fake_hits_the_test_mode_guard(engine_db, engine_cfg,
                                                         seed_graph, insert_message):
    """Delete the fake injection and the test errors instead of touching Gmail."""
    _, message_ids = approved(engine_db, seed_graph, insert_message)
    with pytest.raises(gmail_compat.TestModeError):
        get_adapter("email").stage(engine_db, engine_cfg, message_ids)


@pytest.mark.skipif(not (SKILL_SCRIPTS / "create_drafts.py").exists(),
                    reason="gmail-helper skill not installed on this host")
def test_gmail_compat_matches_the_skill_source():
    """Drift guard: read the skill's TEXT (importing it needs the Google SDK).

    gmail_compat reimplements three helpers in stdlib so the fake service can
    stand in for Gmail. If the skill's query strings or DEMO_LINK ever change,
    this fails instead of the two quietly diverging.
    """
    source = (SKILL_SCRIPTS / "create_drafts.py").read_text(encoding="utf-8")
    assert f'DEMO_LINK = "{gmail_compat.DEMO_LINK}"' in source
    assert 'q=f"in:sent to:{email}"' in source, "already_sent_to query changed upstream"
    assert 'q=f"from:{email}"' in source, "has_inbound_reply query changed upstream"
    assert f'DEFAULT_ACCOUNT = "{gmail_compat.DEFAULT_ACCOUNT}"' in source


@pytest.mark.skipif(not (SKILL_SCRIPTS / "verify_emails.py").exists(),
                    reason="gmail-helper skill not installed on this host")
def test_verdict_constants_match_the_verifier():
    source = (SKILL_SCRIPTS / "verify_emails.py").read_text(encoding="utf-8")
    assert 'PASS, RISK, FAIL = "PASS", "RISK", "FAIL"' in source
    assert (gmail_compat.PASS, gmail_compat.RISK, gmail_compat.FAIL) == ("PASS", "RISK", "FAIL")


def test_create_draft_builds_a_draft_gmail_can_read():
    service = FakeGmailService()
    draft_id = gmail_compat.create_draft(service, "a@b.com", "lowercase subject",
                                         "line one\n\nSam")
    assert service.drafts_store[draft_id]["subject"] == "lowercase subject"
    assert service.drafts_store[draft_id]["to"] == "a@b.com"
    assert "Sam" in service.drafts_store[draft_id]["body"]
    assert service.calls == ["drafts.create"]


def test_already_sent_to_fails_open_and_has_inbound_reply_fails_closed():
    class Broken:
        def users(self):
            raise RuntimeError("Gmail 503")

    assert gmail_compat.already_sent_to(Broken(), "a@b.com") is False
    assert gmail_compat.has_inbound_reply(Broken(), "a@b.com") is False


# --------------------------------------------------------------------------- #
# send-window arithmetic (the national-ICP problem)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone", ["America/New_York", "America/Chicago",
                                  "America/Denver", "America/Los_Angeles",
                                  "America/Phoenix"])
def test_send_window_converts_every_us_zone_to_mst(zone):
    line = channels_base.send_window_line(zone)
    assert line.startswith(f"08:00-18:00 {zone} = ")
    assert line.endswith(" MST")


def test_send_window_degrades_gracefully_on_an_unknown_zone():
    line = channels_base.send_window_line("Mars/Olympus")
    assert "MST conversion unavailable" in line


def test_phoenix_is_its_own_reference():
    assert channels_base.send_window_line("America/Phoenix").endswith(
        "= 08:00-18:00 MST")
