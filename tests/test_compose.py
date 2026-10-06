"""Compose-job emit and draft ingest (PLAN.md §5.1, §5.3, §12.4, §12.8).

The ingest half is written against the failure modes a copywriting agent actually
produces: an unknown prospect_key, the same key twice, a draft with no body, extra
keys, half a batch. PLAN.md §10 row (i) forbids silent truncation, so the central
assertion of this file is the accounting invariant:

    ready + failed + len(unknown) == number of draft rows in the file
"""

import json
import sqlite3

import pytest

from conftest import FIXTURES
from dm_engine import compose
from dm_engine.compose import (ComposeError, IngestResult, dry_run_lint,
                               emit_compose_job, hook_fact, ingest_drafts,
                               market_casual)
from dm_engine.lint import LintReport, normalize

MESSAGES = FIXTURES / "messages"
HOOK = "212 five-star reviews on Google"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def seed(conn, seed_graph, key, casual, *, hook=HOOK, state="QUALIFIED",
         channel="email", batch_id="B-TEST-1", batch_channel="email", **kwargs):
    ids = seed_graph(
        conn,
        prospect_key=key,
        company_name=casual.title() if casual else key,
        company_casual=casual,
        domain=key,
        address=(f"owner@{key}" if channel == "email"
                 else f"https://linkedin.com/in/{key.split('.')[0]}"),
        channel=channel,
        prospect_state=state,
        batch_id=batch_id,
        batch_channel=batch_channel,
        **kwargs,
    )
    conn.execute("UPDATE prospects SET icp_fit_note = ? WHERE id = ?",
                 (hook, ids["prospect_id"]))
    conn.commit()
    return ids


def write_drafts(tmp_path, batch_id, drafts, name="drafts.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"batch_id": batch_id, "drafts": drafts}), encoding="utf-8")
    return path


def fixture_path(name):
    return MESSAGES / name


def load_fixture(name):
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


def clean_draft(key, subject, first_name="Alex"):
    return {
        "prospect_key": key,
        "channel": "email",
        "sequence_step": 1,
        "subject": subject,
        "body": (f"Hey {first_name},\n\nmost hardscape owners I talk to already tried an "
                 "answering service for the phones and it made things worse instead of "
                 "better.\n\nyou're on the compactor and the homeowner calls the next "
                 "crew on the list.\n\nhow many calls do you figure slip by in a week?"
                 "\n\nSam"),
    }


def state_of(conn, message_id):
    return conn.execute("SELECT state FROM messages WHERE id = ?",
                        (message_id,)).fetchone()["state"]


def only_message(conn, key):
    return conn.execute(
        "SELECT m.* FROM messages m JOIN prospects p ON p.id = m.prospect_id "
        "WHERE p.prospect_key = ?", (key,)).fetchone()


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #


def test_market_casual_drops_the_state_code():
    assert market_casual("Boise ID") == "Boise"
    assert market_casual("Scottsdale AZ") == "Scottsdale"
    assert market_casual("") == ""
    assert market_casual(None) == ""


def test_hook_fact_reads_plain_text_or_a_json_note():
    assert hook_fact(HOOK) == HOOK
    assert hook_fact(json.dumps({"hook_fact": HOOK, "other": 1})) == HOOK
    assert hook_fact(json.dumps({"other": 1})) is None
    assert hook_fact("  ") is None
    assert hook_fact(None) is None


def test_ingest_result_serializes_every_bucket():
    result = IngestResult(ready=1, failed=2, missing=["a"], unknown=["b: why"])
    payload = json.loads(result.to_json())
    assert payload == {"ready": 1, "failed": 2, "missing": ["a"],
                       "unknown": ["b: why"], "details": []}
    assert result.handled == 4


# --------------------------------------------------------------------------- #
# emit_compose_job
# --------------------------------------------------------------------------- #


def test_emit_writes_the_job_into_the_review_dir(engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    assert path == engine_cfg.review_dir / "B-TEST-1-composejob.json"
    assert path.exists()
    assert json.loads(path.read_text())["schema"] == compose.COMPOSE_JOB_SCHEMA


def test_job_carries_everything_the_writer_needs(engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text())

    assert job["batch"]["id"] == "B-TEST-1"
    assert job["batch"]["market_casual"] == "Boise"
    assert job["channel_constraints"] == {
        "max_words": 120, "max_sentences": None, "max_chars": None,
        "has_subject": True, "max_subject_chars": 60, "requires_signature": True}
    entry = job["prospects"][0]
    assert entry["prospect_key"] == "acmehardscapeaz.com"
    assert entry["company_casual"] == "acme hardscape"
    assert entry["first_name"] == "Alex"
    assert entry["hook_fact"] == HOOK
    assert entry["market_casual"] == "Boise"
    assert entry["variant"]["id"] == "V1.1"
    assert entry["variant"]["angle"] == "burned before"
    assert entry["variant"]["spec"]["structure"] == ["pain_opener", "soft_question"]
    assert job["rules"]["signature"] == "Sam"
    assert "no_links" in job["rules"] and "no_product_name" in job["rules"]
    assert "i'd love to connect" in job["rules"]["banned_phrases"]
    assert any("de-ai-ify" in step for step in job["agent_sequence"])
    assert job["output_contract"]["batch_id"] == "B-TEST-1"


def test_job_carries_no_addresses_and_no_secrets(engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    text = emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text()
    assert "owner@acmehardscapeaz.com" not in text
    assert "@" not in text.replace("demo-ai-voice", "")
    # the key-signature list from the secrets-hygiene standing rule
    for marker in ("pit-", "sk-", "AKIA", "ghp_", "xox", "AIza", "sk_live_",
                   "whsec_", "li_at=", "token_operator"):
        assert marker not in text
    assert str(engine_cfg.gmail_skill_root) not in text


@pytest.mark.parametrize("field,value,expected", [
    ("company_casual", None, "company_casual"),
    ("hook", "", "hook_fact"),
    ("state", "NEW", "prospect_state=NEW"),
])
def test_job_blocks_prospects_that_cannot_be_personalized(
        engine_db, engine_cfg, seed_graph, field, value, expected):
    kwargs = {"casual": "acme hardscape"}
    if field == "company_casual":
        kwargs["casual"] = value
    else:
        kwargs[field] = value
    seed(engine_db, seed_graph, "acmehardscapeaz.com", **kwargs)
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text())
    assert job["prospects"] == []
    assert job["blocked"][0]["prospect_key"] == "acmehardscapeaz.com"
    assert expected in job["blocked"][0]["missing"]


def test_job_blocks_a_prospect_with_no_contact_on_the_batch_channel(
        engine_db, engine_cfg, seed_graph):
    ids = seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    engine_db.execute("DELETE FROM contacts WHERE id = ?", (ids["contact_id"],))
    engine_db.commit()
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text())
    assert job["prospects"] == []
    assert "email_contact" in job["blocked"][0]["missing"]


def test_job_constraints_come_from_the_parameter(engine_db, engine_cfg, seed_graph):
    class ForeignConstraints:
        max_words = 60
        max_sentences = 2
        max_chars = 400
        has_subject = True
        max_subject_chars = 45
        requires_signature = True

    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1",
                                      constraints=ForeignConstraints()).read_text())
    assert job["channel_constraints"]["max_words"] == 60
    assert job["channel_constraints"]["max_subject_chars"] == 45


def test_job_for_a_linkedin_batch_declares_dm_constraints(
        engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks",
         channel="linkedin", batch_id="B-TEST-DM", batch_channel="linkedin")
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-DM").read_text())
    assert job["channel_constraints"] == {
        "max_words": None, "max_sentences": 3, "max_chars": 300,
        "has_subject": False, "max_subject_chars": None, "requires_signature": False}
    assert job["output_contract"]["drafts"][0]["subject"] is None


def test_job_for_step_2_swaps_the_link_rule(engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1",
                                      sequence_step=2).read_text())
    assert "no_links" not in job["rules"]
    assert "demo-ai-voice" in job["rules"]["demo_link"]


def test_job_lists_recent_subjects_to_avoid(engine_db, engine_cfg, seed_graph,
                                            insert_message):
    ids = seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    message_id = insert_message(engine_db, ids, subject="an old subject")
    engine_db.execute("UPDATE messages SET staged_at = datetime('now') WHERE id = ?",
                      (message_id,))
    engine_db.commit()
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text())
    assert "an old subject" in job["avoid_subjects"]


def test_emit_on_an_unknown_batch_is_loud(engine_db, engine_cfg):
    with pytest.raises(ComposeError, match="unknown batch"):
        emit_compose_job(engine_db, engine_cfg, "B-NOPE")


# --------------------------------------------------------------------------- #
# ingest_drafts — the happy path
# --------------------------------------------------------------------------- #


def test_clean_draft_becomes_ready_with_a_passing_report(engine_db, engine_cfg,
                                                         seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = write_drafts(tmp_path, "B-TEST-1",
                        [clean_draft("acmehardscapeaz.com", "the compactor waits")])
    result = ingest_drafts(engine_db, engine_cfg, path)

    assert (result.ready, result.failed, result.unknown, result.missing) == (1, 0, [], [])
    row = only_message(engine_db, "acmehardscapeaz.com")
    assert row["state"] == "READY"
    report = LintReport.from_json(row["lint_report"])
    assert report.ok and report.violations == []


def test_the_phase_zero_fixture_ingests_clean(engine_db, engine_cfg, seed_graph):
    # Keyed to the prospect tests/fixtures/prospects/one.json seeds, so the §11
    # Phase-0 command block runs start to finish without a missing/unknown split.
    seed(engine_db, seed_graph, "bluevalleyhardscapes.com",
         "blue valley hardscapes")
    result = ingest_drafts(engine_db, engine_cfg, fixture_path("one_ok.json"))
    assert result.ready == 1 and result.failed == 0


def test_a_linkedin_dm_ingests_clean(engine_db, engine_cfg, seed_graph):
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks",
         channel="linkedin", batch_id="B-TEST-DM", batch_channel="linkedin")
    result = ingest_drafts(engine_db, engine_cfg, fixture_path("clean_linkedin_dm.json"))
    assert (result.ready, result.failed) == (1, 0)
    assert only_message(engine_db, "graniteridgestoneworks.com")["subject"] is None


def test_ready_rows_store_normalized_text_so_the_sql_belt_sees_ascii(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft["body"] = draft["body"].replace("answering service", "answering serv​ice")
    path = write_drafts(tmp_path, "B-TEST-1", [draft])
    ingest_drafts(engine_db, engine_cfg, path)
    stored = only_message(engine_db, "acmehardscapeaz.com")["body"]
    assert "​" not in stored
    assert stored == normalize(draft["body"])


def test_ingest_writes_a_state_log_row_for_every_transition(engine_db, engine_cfg,
                                                            seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = write_drafts(tmp_path, "B-TEST-1",
                        [clean_draft("acmehardscapeaz.com", "the compactor waits")])
    ingest_drafts(engine_db, engine_cfg, path)
    states = [row["to_state"] for row in engine_db.execute(
        "SELECT to_state FROM state_log WHERE entity = 'message' ORDER BY id")]
    assert states == ["COMPOSED", "READY"]


# --------------------------------------------------------------------------- #
# ingest_drafts — lint is the error surface, not the database
# --------------------------------------------------------------------------- #


def test_a_dirty_draft_becomes_lint_failed_and_keeps_its_report(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft["body"] = draft["body"].replace("the phones", "the phones — see example.com/x")
    path = write_drafts(tmp_path, "B-TEST-1", [draft])

    result = ingest_drafts(engine_db, engine_cfg, path)
    assert (result.ready, result.failed) == (0, 1)
    row = only_message(engine_db, "acmehardscapeaz.com")
    assert row["state"] == "LINT_FAILED"
    report = LintReport.from_json(row["lint_report"])
    assert "L1" in report.rules()


def test_lint_catches_the_link_first_so_g6_never_becomes_the_error_surface(
        engine_db, engine_cfg, seed_graph):
    """PLAN.md §10 row (b): two layers, but lint must produce the readable message."""
    for key, casual, name in [("acmehardscapeaz.com", "acme hardscape", "Alex"),
                              ("graniteridgestoneworks.com", "granite ridge stoneworks", "Marco"),
                              ("pinebluffoutdoor.com", "pine bluff", "Dana"),
                              ("oakhollowpatios.com", "oak hollow", "Rick"),
                              ("alderfieldhardscape.com", "alderfield hardscape", "Tom"),
                              ("mesagrandepavers.com", "mesa grande", "Luis"),
                              ("sunvalleystoneaz.com", "sun valley stone", "Pete")]:
        seed(engine_db, seed_graph, key, casual)
    result = ingest_drafts(engine_db, engine_cfg, fixture_path("violation_L1_url.json"))
    assert result.ready == 0 and result.failed == 7
    for row in engine_db.execute("SELECT lint_report FROM messages"):
        rules = LintReport.from_json(row["lint_report"]).rules()
        assert "L1" in rules
        assert not any(rule.startswith("G") or rule == "DB" for rule in rules), \
            "the database abort, not the linter, produced the message"


def test_unicode_smuggled_product_names_are_caught_at_ingest(
        engine_db, engine_cfg, seed_graph):
    for key, casual in [("acmehardscapeaz.com", "acme hardscape"),
                        ("graniteridgestoneworks.com", "granite ridge stoneworks"),
                        ("pinebluffoutdoor.com", "pine bluff"),
                        ("oakhollowpatios.com", "oak hollow"),
                        ("alderfieldhardscape.com", "alderfield hardscape")]:
        seed(engine_db, seed_graph, key, casual)
    result = ingest_drafts(engine_db, engine_cfg, fixture_path("unicode_smuggling.json"))
    assert result.ready == 0 and result.failed == 5


# --------------------------------------------------------------------------- #
# ingest_drafts — nothing is silently dropped
# --------------------------------------------------------------------------- #


def test_partial_batch_accounts_for_every_row(engine_db, engine_cfg, seed_graph):
    for key, casual in [("acmehardscapeaz.com", "acme hardscape"),
                        ("graniteridgestoneworks.com", "granite ridge stoneworks"),
                        ("pinebluffoutdoor.com", "pine bluff")]:
        seed(engine_db, seed_graph, key, casual)
    payload = load_fixture("partial_batch.json")
    result = ingest_drafts(engine_db, engine_cfg, fixture_path("partial_batch.json"))

    assert result.handled == len(payload["drafts"]), "a row vanished without a bucket"
    assert result.ready == 2
    assert result.failed == 0
    assert len(result.unknown) == 5
    assert result.missing == ["pinebluffoutdoor.com"]
    reasons = " | ".join(result.unknown)
    for expected in ("not assigned", "duplicate", "no body", "not a JSON object",
                     "no prospect_key"):
        assert expected in reasons
    assert len(result.details) == len(payload["drafts"])


def test_a_prospect_key_outside_the_batch_never_reaches_the_database(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = write_drafts(tmp_path, "B-TEST-1",
                        [clean_draft("notinthisbatch.com", "a subject for a stranger")])
    result = ingest_drafts(engine_db, engine_cfg, path)
    assert result.unknown and "not assigned" in result.unknown[0]
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_duplicate_prospect_key_does_not_overwrite_the_first_draft(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    first = clean_draft("acmehardscapeaz.com", "the compactor waits")
    second = clean_draft("acmehardscapeaz.com", "a second bite")
    path = write_drafts(tmp_path, "B-TEST-1", [first, second])
    result = ingest_drafts(engine_db, engine_cfg, path)

    assert result.ready == 1
    assert "duplicate" in result.unknown[0]
    assert only_message(engine_db, "acmehardscapeaz.com")["subject"] == "the compactor waits"


def test_a_draft_with_no_body_is_reported_not_skipped(engine_db, engine_cfg,
                                                      seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = write_drafts(tmp_path, "B-TEST-1", [{"prospect_key": "acmehardscapeaz.com",
                                               "channel": "email", "subject": "s"}])
    result = ingest_drafts(engine_db, engine_cfg, path)
    assert result.unknown == ["acmehardscapeaz.com: draft has no body"]
    assert result.missing == ["acmehardscapeaz.com"]


def test_extra_unknown_keys_on_a_draft_are_tolerated(engine_db, engine_cfg,
                                                     seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft.update({"variant_used": "V1.1", "confidence": 0.9, "nested": {"a": [1, 2]}})
    path = write_drafts(tmp_path, "B-TEST-1", [draft])
    assert ingest_drafts(engine_db, engine_cfg, path).ready == 1


def test_missing_lists_batch_prospects_with_no_live_message(engine_db, engine_cfg,
                                                            seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    path = write_drafts(tmp_path, "B-TEST-1",
                        [clean_draft("acmehardscapeaz.com", "the compactor waits")])
    result = ingest_drafts(engine_db, engine_cfg, path)
    assert result.missing == ["graniteridgestoneworks.com"]


def test_a_lint_failed_prospect_stays_in_missing_until_it_is_recomposed(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    bad = clean_draft("acmehardscapeaz.com", "the compactor waits")
    bad["body"] = bad["body"].replace("the phones", "the phones at example.com")
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, "B-TEST-1", [bad]))
    assert result.failed == 1 and result.missing == ["acmehardscapeaz.com"]


# --------------------------------------------------------------------------- #
# ingest_drafts — re-ingest and overwrite policy
# --------------------------------------------------------------------------- #


def test_a_lint_failed_message_can_be_reingested_into_ready(engine_db, engine_cfg,
                                                            seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    bad = clean_draft("acmehardscapeaz.com", "the compactor waits")
    bad["body"] = bad["body"].replace("the phones", "the phones at example.com")
    ingest_drafts(engine_db, engine_cfg, write_drafts(tmp_path, "B-TEST-1", [bad]))
    first_id = only_message(engine_db, "acmehardscapeaz.com")["id"]

    good = clean_draft("acmehardscapeaz.com", "the compactor waits")
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, "B-TEST-1", [good], name="retry.json"))
    row = only_message(engine_db, "acmehardscapeaz.com")
    assert result.ready == 1 and result.missing == []
    assert row["id"] == first_id and row["state"] == "READY"


def test_ingest_refuses_to_overwrite_a_message_that_is_past_composed(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = write_drafts(tmp_path, "B-TEST-1",
                        [clean_draft("acmehardscapeaz.com", "the compactor waits")])
    ingest_drafts(engine_db, engine_cfg, path)
    result = ingest_drafts(engine_db, engine_cfg, path)
    assert result.ready == 0
    assert "already READY" in result.unknown[0]


# --------------------------------------------------------------------------- #
# ingest_drafts — the gates hold
# --------------------------------------------------------------------------- #


def test_subject_uniqueness_is_enforced_across_the_file(engine_db, engine_cfg,
                                                        seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    drafts = [clean_draft("acmehardscapeaz.com", "the compactor waits"),
              clean_draft("graniteridgestoneworks.com", "The Compactor Waits", "Marco")]
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, "B-TEST-1", drafts))
    assert (result.ready, result.failed) == (1, 1)
    row = only_message(engine_db, "graniteridgestoneworks.com")
    assert "L9" in LintReport.from_json(row["lint_report"]).rules()


def test_subject_uniqueness_is_enforced_against_staged_history(
        engine_db, engine_cfg, seed_graph, insert_message, tmp_path):
    ids = seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    old = insert_message(engine_db, ids, subject="the compactor waits")
    engine_db.execute("UPDATE messages SET staged_at = datetime('now'), batch_id = ? "
                      "WHERE id = ?", ("B-TEST-1", old))
    engine_db.commit()
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    result = ingest_drafts(engine_db, engine_cfg, write_drafts(
        tmp_path, "B-TEST-1",
        [clean_draft("graniteridgestoneworks.com", "the compactor waits", "Marco")]))
    assert result.failed == 1
    row = only_message(engine_db, "graniteridgestoneworks.com")
    assert "L9" in LintReport.from_json(row["lint_report"]).rules()


def test_a_suppressed_contact_is_refused_by_g8_and_reported(engine_db, engine_cfg,
                                                            seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('email', 'owner@acmehardscapeaz.com', 'address', 'hard_no', 'test')")
    engine_db.commit()
    result = ingest_drafts(engine_db, engine_cfg, write_drafts(
        tmp_path, "B-TEST-1", [clean_draft("acmehardscapeaz.com", "the compactor waits")]))
    assert result.ready == 0 and result.failed == 0
    assert "G8" in result.unknown[0]
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_second_message_one_for_the_same_prospect_is_refused_by_g11(
        engine_db, engine_cfg, seed_graph, insert_message, tmp_path):
    ids = seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    engine_db.execute(
        "INSERT INTO batches (id, experiment_id, market, channel, personalization, batch_seq) "
        "VALUES ('B-TEST-2', ?, 'Boise ID', 'email', 'unique', 2)",
        (ids["experiment_id"],))
    insert_message(engine_db, ids)                       # the existing active Message 1
    engine_db.execute("UPDATE assignments SET batch_id = 'B-TEST-2' WHERE id = ?",
                      (ids["assignment_id"],))
    engine_db.commit()
    result = ingest_drafts(engine_db, engine_cfg, write_drafts(
        tmp_path, "B-TEST-2", [clean_draft("acmehardscapeaz.com", "a second touch")]))
    assert "G11" in result.unknown[0]


def test_channel_mismatch_is_rejected_as_cross_batch_contamination(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft["channel"] = "linkedin"
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, "B-TEST-1", [draft]))
    assert "does not match batch channel" in result.unknown[0]


@pytest.mark.parametrize("step", [0, 3, "1", None, True])
def test_a_bad_sequence_step_is_rejected(engine_db, engine_cfg, seed_graph, tmp_path, step):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft["sequence_step"] = step
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, "B-TEST-1", [draft]))
    assert "sequence_step" in result.unknown[0]


def test_a_ready_message_body_also_satisfies_the_sql_belt(engine_db, engine_cfg,
                                                          seed_graph, tmp_path):
    """A READY row exists, which means G5 and G6 both accepted the stored content."""
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    ingest_drafts(engine_db, engine_cfg, write_drafts(
        tmp_path, "B-TEST-1", [clean_draft("acmehardscapeaz.com", "the compactor waits")]))
    row = only_message(engine_db, "acmehardscapeaz.com")
    assert row["state"] == "READY"
    assert json.loads(row["lint_report"])["ok"] is True
    body = row["body"].lower()
    assert not any(marker in body for marker in
                   ("http://", "https://", "www.", "brightflowcure", "bdai", "bright desk ai"))


# --------------------------------------------------------------------------- #
# ingest_drafts — malformed files are loud, not silent
# --------------------------------------------------------------------------- #


def test_unknown_batch_in_the_file_is_loud(engine_db, engine_cfg, tmp_path):
    with pytest.raises(ComposeError, match="unknown batch"):
        ingest_drafts(engine_db, engine_cfg, write_drafts(tmp_path, "B-NOPE", []))


def test_a_missing_batch_id_is_loud(engine_db, engine_cfg, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"drafts": []}), encoding="utf-8")
    with pytest.raises(ComposeError, match="batch_id"):
        ingest_drafts(engine_db, engine_cfg, path)


def test_drafts_must_be_a_list(engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"batch_id": "B-TEST-1", "drafts": {}}), encoding="utf-8")
    with pytest.raises(ComposeError, match="must be a list"):
        ingest_drafts(engine_db, engine_cfg, path)


def test_invalid_json_is_loud(engine_db, engine_cfg, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ComposeError, match="not valid JSON"):
        ingest_drafts(engine_db, engine_cfg, path)


def test_a_missing_file_is_loud(engine_db, engine_cfg, tmp_path):
    with pytest.raises(ComposeError, match="cannot read"):
        ingest_drafts(engine_db, engine_cfg, tmp_path / "nope.json")


def test_a_non_object_top_level_is_loud(engine_db, engine_cfg, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ComposeError, match="JSON object"):
        ingest_drafts(engine_db, engine_cfg, path)


# --------------------------------------------------------------------------- #
# round trip: the job the engine emits is answerable by the file it ingests
# --------------------------------------------------------------------------- #


def test_compose_job_and_ingest_round_trip(engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    job = json.loads(emit_compose_job(engine_db, engine_cfg, "B-TEST-1").read_text())

    answer = [clean_draft(entry["prospect_key"], f"the compactor and {entry['company_casual']}",
                          entry["first_name"])
              for entry in job["prospects"]]
    assert len(answer) == 2
    result = ingest_drafts(engine_db, engine_cfg,
                           write_drafts(tmp_path, job["batch"]["id"], answer))
    assert (result.ready, result.failed, result.missing, result.unknown) == (2, 0, [], [])


# --------------------------------------------------------------------------- #
# dry_run_lint — the same rules, against the same two files, with no database
# --------------------------------------------------------------------------- #


def test_dry_run_matches_a_real_ingest_on_the_same_inputs(engine_db, engine_cfg,
                                                           seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    drafts_path = write_drafts(
        tmp_path, "B-TEST-1", [clean_draft("acmehardscapeaz.com", "the compactor waits")])

    dry = dry_run_lint(job_path, drafts_path)
    live = ingest_drafts(engine_db, engine_cfg, drafts_path)

    assert (dry.ready, dry.failed, dry.missing, dry.unknown) == \
           (live.ready, live.failed, live.missing, live.unknown)


def test_dry_run_touches_no_database(engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    drafts_path = write_drafts(
        tmp_path, "B-TEST-1", [clean_draft("acmehardscapeaz.com", "the compactor waits")])

    dry_run_lint(job_path, drafts_path)
    assert engine_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0] == 0


def test_dry_run_catches_the_same_violation_a_real_ingest_would(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    draft = clean_draft("acmehardscapeaz.com", "the compactor waits")
    draft["body"] = draft["body"].replace("the phones", "the phones — see example.com/x")
    drafts_path = write_drafts(tmp_path, "B-TEST-1", [draft])

    result = dry_run_lint(job_path, drafts_path)
    assert (result.ready, result.failed) == (0, 1)
    assert result.details[0]["rules"] == ["L1"]


def test_dry_run_reports_missing_unknown_and_duplicate_like_a_real_ingest(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    drafts = [clean_draft("acmehardscapeaz.com", "the compactor waits"),
             clean_draft("acmehardscapeaz.com", "a second bite"),
             clean_draft("notinthisbatch.com", "a subject for a stranger")]
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    drafts_path = write_drafts(tmp_path, "B-TEST-1", drafts)

    result = dry_run_lint(job_path, drafts_path)
    assert result.ready == 1
    assert result.missing == ["graniteridgestoneworks.com"]
    assert len(result.unknown) == 2
    reasons = " | ".join(result.unknown)
    assert "duplicate" in reasons
    assert "not in this compose job's prospects" in reasons


def test_dry_run_enforces_subject_uniqueness_within_the_candidate_file(
        engine_db, engine_cfg, seed_graph, tmp_path):
    seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    drafts = [clean_draft("acmehardscapeaz.com", "the compactor waits"),
             clean_draft("graniteridgestoneworks.com", "The Compactor Waits", "Marco")]
    drafts_path = write_drafts(tmp_path, "B-TEST-1", drafts)

    result = dry_run_lint(job_path, drafts_path)
    assert (result.ready, result.failed) == (1, 1)
    failed = next(d for d in result.details if d["outcome"] == "lint_failed")
    assert "L9" in failed["rules"]


def test_dry_run_uses_the_compose_jobs_recent_subject_history(
        engine_db, engine_cfg, seed_graph, insert_message, tmp_path):
    ids = seed(engine_db, seed_graph, "acmehardscapeaz.com", "acme hardscape")
    old = insert_message(engine_db, ids, subject="the compactor waits")
    engine_db.execute("UPDATE messages SET staged_at = datetime('now'), batch_id = ? "
                      "WHERE id = ?", ("B-TEST-1", old))
    engine_db.commit()
    seed(engine_db, seed_graph, "graniteridgestoneworks.com", "granite ridge stoneworks")
    job_path = emit_compose_job(engine_db, engine_cfg, "B-TEST-1")
    assert "the compactor waits" in json.loads(job_path.read_text())["avoid_subjects"]
    drafts_path = write_drafts(
        tmp_path, "B-TEST-1",
        [clean_draft("graniteridgestoneworks.com", "the compactor waits", "Marco")])

    result = dry_run_lint(job_path, drafts_path)
    assert result.failed == 1
    assert "L9" in result.details[0]["rules"]


def test_dry_run_missing_files_are_loud(engine_db, engine_cfg, tmp_path):
    with pytest.raises(ComposeError, match="cannot read"):
        dry_run_lint(tmp_path / "nope-job.json", tmp_path / "nope-drafts.json")
