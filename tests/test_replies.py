"""replies.py — an auto-responder is not a reply.

The thing being defended here is the numerator. Every test below is a way the reply
count could be quietly inflated (or a real reply quietly lost) if the classifier were
sloppy.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from dm_engine import replies

from conftest import CLEAN_BODY, PASSING_LINT


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)

# --------------------------------------------------------------------------- #
# a Gmail stub with exactly the call shapes replies.scan() uses
# (Owner D's tests/fakes.py is checked separately, at the bottom of this file)
# --------------------------------------------------------------------------- #


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(msg_id, *, sender, subject="", body="", thread_id=None,
                  headers=None, labels=None, internal_ms=None):
    """One Gmail message dict.

    `internal_ms` defaults to NOW — i.e. after the message it is answering. The old
    default was a fixed epoch that happened to sit a year BEFORE the seeded send,
    which is finding C2's exact shape; every scan test was silently asserting that a
    reply predating the send still counts. Tests that want that case now say so.
    """
    if internal_ms is None:
        internal_ms = _ms(_now())
    all_headers = [{"name": "From", "value": sender},
                   {"name": "Subject", "value": subject}]
    for name, value in (headers or {}).items():
        all_headers.append({"name": name, "value": value})
    return {
        "id": msg_id,
        "threadId": thread_id or f"t-{msg_id}",
        "labelIds": labels or ["INBOX"],
        "internalDate": str(internal_ms),
        "snippet": body[:120],
        "payload": {
            "mimeType": "text/plain",
            "headers": all_headers,
            "body": {"data": _b64(body)},
        },
    }


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class StubGmail:
    """Minimal `users().messages().list/get` over a list of message dicts."""

    def __init__(self, messages):
        self.messages_by_id = {m["id"]: m for m in messages}
        self.queries = []

    # googleapiclient's fluent shape
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId=None, q="", maxResults=None):  # noqa: N803 — API shape
        self.queries.append(q)
        froms = [token[len("from:"):].strip("()").lower()
                 for token in q.replace("(", " ").replace(")", " ").split()
                 if token.lower().startswith("from:")]
        hits = []
        for message in self.messages_by_id.values():
            sender = next(h["value"] for h in message["payload"]["headers"]
                          if h["name"] == "From").lower()
            if any(token in sender for token in froms):
                hits.append({"id": message["id"], "threadId": message["threadId"]})
        return _Exec({"messages": hits[:maxResults or len(hits)]})

    def get(self, userId=None, id=None, format=None):  # noqa: A002, N803 — API shape
        return _Exec(self.messages_by_id[id])


def advance_to_sent(conn, message_id, thread_id="t-out"):
    """COMPOSED -> SENT the legal way (the triggers refuse any shortcut)."""
    conn.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED', staged_at = datetime('now'), "
                 "gmail_thread_id = ? WHERE id = ?", (thread_id, message_id))
    conn.execute("UPDATE messages SET state = 'SENT', sent_at = datetime('now'), "
                 "sent_evidence = 'test' WHERE id = ?", (message_id,))
    conn.commit()


@pytest.fixture
def sent_message(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    advance_to_sent(engine_db, message_id)
    ids["message_id"] = message_id
    return ids


# --------------------------------------------------------------------------- #
# classification — the whole ballgame
# --------------------------------------------------------------------------- #

def test_a_human_reply_is_human():
    verdict = replies.classify(
        "Alex <alex@acmehardscapeaz.com>",
        "Re: the compactor doesn't take messages",
        "yeah we tried an answering service last year, it was useless. what do you charge?")
    assert verdict.classification == "unsure"
    assert verdict.is_human is True
    # L2: a human row names the rule that judged it human, exactly like a machine row
    assert verdict.classified_by == "heuristic:no_machine_marker"


def test_mailer_daemon_hard_bounce_is_a_bounce_not_a_reply():
    verdict = replies.classify(
        "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "Delivery Status Notification (Failure)",
        "Address not found. The email account that you tried to reach does not exist. 550 5.1.1")
    assert verdict.classification == "bounce"
    assert verdict.is_human is False
    assert verdict.classified_by.startswith("heuristic:")


def test_delivery_delay_is_machine_mail_but_not_a_bounce():
    """A real triage file filed a DELAY notice as a prospect `no` — that would have
    booked a fake reply AND suppressed a live address."""
    verdict = replies.classify(
        "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "Delivery Status Notification (Delay)",
        "Your message wasn't delivered yet. Gmail will retry for 47 more hours.")
    assert verdict.classification == "auto_reply"
    assert verdict.is_human is False
    assert "delivery_delay" in verdict.marker


def test_out_of_office_is_a_vacation_responder():
    verdict = replies.classify(
        "Alex <alex@acmehardscapeaz.com>",
        "Automatic reply: the compactor doesn't take messages",
        "I am out of the office until August 12 with limited access to email.")
    assert verdict.classification == "vacation"
    assert verdict.is_human is False


def test_we_received_your_message_autoresponder_is_not_a_reply():
    verdict = replies.classify(
        "Acme Hardscape <info@acmehardscapeaz.com>",
        "Thanks!",
        "Thank you for contacting Acme Hardscape. We have received your message "
        "and someone will get back to you within one business day.")
    assert verdict.is_human is False
    assert verdict.classification == "auto_reply"


def test_auto_submitted_header_beats_a_human_looking_body():
    verdict = replies.classify(
        "Alex <alex@acmehardscapeaz.com>", "Re: hey",
        "sure, give me a call tomorrow",
        headers={"Auto-Submitted": "auto-replied"})
    assert verdict.is_human is False
    assert verdict.marker == "auto_submitted_header"


def test_noreply_sender_is_never_a_human_reply():
    verdict = replies.classify("no-reply@acmehardscapeaz.com", "Re: hey", "thanks")
    assert verdict.is_human is False
    assert verdict.marker == "noreply_sender"


def test_precedence_bulk_alone_does_not_discard_a_real_reply():
    """Some CRMs stamp Precedence: bulk on genuine human outbound — dropping those
    would understate the reply rate, which is the same crime in the other direction."""
    verdict = replies.classify(
        "Alex <alex@acmehardscapeaz.com>", "Re: hey", "what does it cost?",
        headers={"Precedence": "bulk"})
    assert verdict.is_human is True
    verdict_list = replies.classify(
        "Alex <alex@acmehardscapeaz.com>", "Re: hey", "newsletter",
        headers={"Precedence": "bulk", "List-Id": "<news.example.com>"})
    assert verdict_list.is_human is False


def test_zero_width_and_homoglyph_smuggling_does_not_dodge_the_classifier():
    verdict = replies.classify(
        "alex@acmehardscapeaz.com", "Auto​matic reply",
        "I am out‌ of the office until Monday")
    assert verdict.is_human is False


def test_is_human_class_is_the_single_source_of_truth():
    for name in ("hot", "question", "objection", "no", "unsure"):
        assert replies.is_human_class(name) is True
    for name in ("bounce", "auto_reply", "vacation"):
        assert replies.is_human_class(name) is False
    with pytest.raises(replies.ClassificationError):
        replies.is_human_class("replied")


# --------------------------------------------------------------------------- #
# scan()
# --------------------------------------------------------------------------- #

def test_scan_records_a_human_reply_and_moves_the_message(engine_db, engine_cfg,
                                                          sent_message):
    service = StubGmail([gmail_message(
        "m1", sender=f"Alex <{sent_message['address']}>", subject="Re: hey",
        body="we tried a chatbot, hated it. what are you selling?",
        thread_id="t-out")])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert counts["new_replies"] == 1
    assert counts["bounces"] == 0

    row = engine_db.execute("SELECT * FROM replies").fetchone()
    assert row["is_human"] == 1
    assert row["message_id"] == sent_message["message_id"]  # bound by threadId
    state = engine_db.execute("SELECT state FROM messages WHERE id = ?",
                              (sent_message["message_id"],)).fetchone()[0]
    assert state == "REPLIED"


def test_scan_does_not_count_an_autoresponder(engine_db, engine_cfg, sent_message):
    service = StubGmail([gmail_message(
        "m1", sender=f"Alex <{sent_message['address']}>",
        subject="Automatic reply: hey", body="I'm out of the office until Monday.")])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert counts["new_replies"] == 0
    assert counts["auto"] == 1
    assert engine_db.execute("SELECT is_human FROM replies").fetchone()[0] == 0
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "SENT"
    # and the sheet must not show them as having replied
    assert engine_db.execute("SELECT replied FROM sheet").fetchone()[0] == "n"


def test_scan_bounce_suppresses_and_marks_bounced(engine_db, engine_cfg, sent_message):
    service = StubGmail([gmail_message(
        "m1", sender=f"Acme <{sent_message['address']}>",
        subject="Delivery Status Notification (Failure)",
        body="550 5.1.1 the email account that you tried to reach does not exist")])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert counts["bounces"] == 1
    assert counts["new_replies"] == 0
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "BOUNCED"
    suppressed = engine_db.execute(
        "SELECT reason FROM suppression WHERE address = ?",
        (sent_message["address"],)).fetchone()
    assert suppressed["reason"] == "bounce"


def test_scan_finds_daemon_dsns_that_never_come_from_the_prospect(
        engine_db, engine_cfg, sent_message):
    """Without this pass a dead address stays in the delivered denominator forever."""
    service = StubGmail([gmail_message(
        "d1", sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        subject="Delivery Status Notification (Failure)",
        body=f"Your message wasn't delivered to {sent_message['address']} because "
             "the address couldn't be found. 550 5.1.1",
        headers={"X-Failed-Recipients": sent_message["address"]})])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert counts["bounces"] == 1
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "BOUNCED"


def test_scan_is_idempotent(engine_db, engine_cfg, sent_message):
    service = StubGmail([gmail_message(
        "m1", sender=f"Alex <{sent_message['address']}>", subject="Re: hey",
        body="what does it cost?", thread_id="t-out")])
    first = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    second = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert first["new_replies"] == 1
    assert second["new_replies"] == 0
    assert engine_db.execute("SELECT count(*) FROM replies").fetchone()[0] == 1


def test_scan_ignores_our_own_sent_copy(engine_db, engine_cfg, sent_message):
    service = StubGmail([gmail_message(
        "m1", sender=f"Sam <{sent_message['address']}>", subject="hey",
        body="our outbound copy", labels=["SENT"])])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service)
    assert counts["new_replies"] == 0
    assert engine_db.execute("SELECT count(*) FROM replies").fetchone()[0] == 0


def test_scan_accepts_a_prebuilt_service_object(engine_db, engine_cfg, sent_message):
    service = StubGmail([])
    assert replies.build_service(engine_cfg, service) is service


def test_scan_with_no_sent_messages_touches_nothing(engine_db, engine_cfg):
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: StubGmail([]))
    assert counts == {"scanned": 0, "new_replies": 0, "bounces": 0, "auto": 0,
                      "carryover": 0}


# --------------------------------------------------------------------------- #
# triage ingest
# --------------------------------------------------------------------------- #

def _triage_file(tmp_path, items, generated=None):
    """A dashboard-reply-triage output file. `generatedAt` defaults to now: a triage run
    that predates the message it reports on is finding C2's shape, not the norm."""
    generated = generated or _now().isoformat(timespec="seconds").replace("+00:00", "Z")
    path = tmp_path / "triage-2026-07-13-0150.json"
    path.write_text(json.dumps({"generatedAt": generated, "items": items}),
                    encoding="utf-8")
    return path


def test_ingest_triage_overrides_a_daemon_item_the_llm_called_a_reply(
        engine_db, engine_cfg, sent_message, tmp_path):
    """Verbatim shape from ~/work/dashboard/data/approvals/triage-2026-07-13-0150.json,
    where a delivery notice was labelled `no`. Trusting that would create a human
    reply that never happened and suppress a live address as a hard no."""
    path = _triage_file(tmp_path, [{
        "id": "19f2bd752e57cb64",
        "kind": "reply-draft",
        "from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "company": "acmehardscapeaz.com",
        "classification": "no",
        "summary": "System delivery-DELAY notice (not bounce) for "
                   f"{sent_message['address']}. Gmail retrying 47h. No prospect reply.",
        "draftId": None,
        "link": "https://mail.google.com/mail/u/0/#inbox/19f2bd752e57cb64",
        "suggested": "No action.",
    }])
    # the daemon address is not one of our contacts, so nothing is recorded at all
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 0
    assert engine_db.execute("SELECT count(*) FROM replies").fetchone()[0] == 0
    assert engine_db.execute("SELECT count(*) FROM suppression").fetchone()[0] == 0


def test_ingest_triage_reclassifies_an_autoresponder_sent_from_the_prospect_address(
        engine_db, engine_cfg, sent_message, tmp_path):
    path = _triage_file(tmp_path, [{
        "id": "thread-1",
        "kind": "reply-draft",
        "from": f"Acme Hardscape <{sent_message['address']}>",
        "classification": "hot",
        "summary": "Thank you for contacting Acme Hardscape. We have received "
                   "your message and someone will get back to you.",
        "draftId": "d1",
    }])
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 1
    row = engine_db.execute("SELECT * FROM replies").fetchone()
    assert row["is_human"] == 0
    assert row["classification"] == "auto_reply"
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "SENT"


def test_ingest_triage_records_a_real_human_reply(engine_db, engine_cfg,
                                                  sent_message, tmp_path):
    path = _triage_file(tmp_path, [{
        "id": "thread-2",
        "kind": "reply-draft",
        "from": f"Alex <{sent_message['address']}>",
        "classification": "hot",
        "summary": "asked what it costs and whether it books jobs",
        "draftId": "d2",
    }])
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 1
    row = engine_db.execute("SELECT * FROM replies").fetchone()
    assert (row["is_human"], row["classification"]) == (1, "hot")
    assert row["classified_by"].startswith("triage_file:")
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "REPLIED"


def test_ingest_triage_never_invents_a_prospect(engine_db, engine_cfg, sent_message,
                                                tmp_path):
    path = _triage_file(tmp_path, [{
        "id": "thread-3", "from": "Someone <stranger@example.com>",
        "classification": "hot", "summary": "interested!"}])
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 0


def test_ingest_triage_is_idempotent(engine_db, engine_cfg, sent_message, tmp_path):
    path = _triage_file(tmp_path, [{
        "id": "thread-4", "from": f"Alex <{sent_message['address']}>",
        "classification": "question", "summary": "how does it work?"}])
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 1
    assert replies.ingest_triage(engine_db, engine_cfg, path) == 0


# --------------------------------------------------------------------------- #
# record_reply()
# --------------------------------------------------------------------------- #

def test_record_reply_hard_no_suppresses_and_closes(engine_db, seed_graph,
                                                    insert_message):
    ids = seed_graph(engine_db, person_key="alex-x")  # distinct from the address
    advance_to_sent(engine_db, insert_message(engine_db, ids))
    replies.record_reply(engine_db, ids["prospect_id"], "email", "no",
                         "not interested, we're all set", "operator")
    reasons = {(r["scope"], r["reason"], r["address"]) for r in engine_db.execute(
        "SELECT scope, reason, address FROM suppression")}
    assert ("address", "hard_no", ids["address"]) in reasons
    # a no follows the human, not the company (§9 edge 34)
    assert ("person", "hard_no", "alex-x") in reasons
    outcome = engine_db.execute("SELECT outcome FROM outcomes").fetchone()[0]
    assert outcome == "replied_no"


def test_record_reply_unsubscribe_is_its_own_reason(engine_db, sent_message):
    replies.record_reply(engine_db, sent_message["prospect_id"], "email", "no",
                         "please unsubscribe me from this list", "operator")
    reason = engine_db.execute(
        "SELECT reason FROM suppression WHERE scope = 'address'").fetchone()[0]
    assert reason == "unsubscribe"
    assert engine_db.execute("SELECT outcome FROM outcomes").fetchone()[0] == "unsubscribed"


def test_record_reply_rejects_an_unknown_classification(engine_db, sent_message):
    with pytest.raises(replies.ClassificationError):
        replies.record_reply(engine_db, sent_message["prospect_id"], "email",
                             "interested", "yes!", "operator")


def test_record_reply_counts_as_a_reply_in_the_sheet(engine_db, sent_message):
    replies.record_reply(engine_db, sent_message["prospect_id"], "email", "hot",
                         "yes let's talk", "operator")
    assert engine_db.execute("SELECT replied FROM sheet").fetchone()[0] == "y"


def test_record_reply_machine_class_never_reaches_the_sheet(engine_db, sent_message):
    replies.record_reply(engine_db, sent_message["prospect_id"], "email", "vacation",
                         "out of the office", "operator")
    assert engine_db.execute("SELECT replied FROM sheet").fetchone()[0] == "n"


def test_snippet_is_capped(engine_db, sent_message):
    replies.record_reply(engine_db, sent_message["prospect_id"], "email", "hot",
                         "x" * 1000, "operator")
    stored = engine_db.execute("SELECT snippet FROM replies").fetchone()[0]
    assert len(stored) == replies.SNIPPET_MAX


# --------------------------------------------------------------------------- #
# integration with Owner D's fakes, once they exist
# --------------------------------------------------------------------------- #

def test_scan_works_against_owner_d_fake_gmail(engine_db, engine_cfg, sent_message):
    fakes = pytest.importorskip("fakes", reason="Owner D's tests/fakes.py not built yet")
    if not hasattr(fakes, "FakeGmailService"):
        pytest.skip("fakes.FakeGmailService not built yet")
    service = fakes.FakeGmailService()
    service.seed_inbound(sent_message["address"], subject="Re: hey",
                         snippet="what would this cost me?")
    service.seed_inbound(sent_message["address"], subject="Automatic reply",
                         snippet="I am out of the office until Monday")
    # newer_than: is a Gmail server-side filter the fake does not model
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service,
                          newer_than_days=0)
    assert counts["new_replies"] == 1, "the autoresponder must not count"
    assert counts["auto"] == 1
    assert engine_db.execute(
        "SELECT count(*) FROM replies WHERE is_human = 1").fetchone()[0] == 1


def test_scan_marks_bounces_from_owner_d_fake(engine_db, engine_cfg, sent_message):
    fakes = pytest.importorskip("fakes", reason="Owner D's tests/fakes.py not built yet")
    service = fakes.FakeGmailService()
    service.seed_bounce(sent_message["address"])
    counts = replies.scan(engine_db, engine_cfg, service_factory=lambda: service,
                          newer_than_days=0)
    assert counts["bounces"] == 1
    assert engine_db.execute("SELECT state FROM messages WHERE id = ?",
                             (sent_message["message_id"],)).fetchone()[0] == "BOUNCED"


# --------------------------------------------------------------------------- #
# the DM channels: a suppressed prospect stays dead
# --------------------------------------------------------------------------- #

def test_a_dead_prospect_suppressed_on_whatsapp_is_not_reopened_by_a_reply(
        engine_db, seed_graph):
    """`_is_suppressed_prospect` mirrors trigger G8, so it has to fold the same way.

    Comparing the RAW address answered "not suppressed" for a number stored as
    '+1602…' against a suppression row holding the folded digits — and the prospect
    who said stop was reopened by their own next reply.
    """
    from dm_engine import replies as replies_mod
    from dm_engine import suppression

    ids = seed_graph(engine_db, channel="whatsapp", batch_channel="whatsapp",
                     address="+16025550134", verify_verdict=None)
    replies_mod.set_outcome(engine_db, ids["prospect_id"], "dead", "operator")
    suppression.add(engine_db, "whatsapp", "(602) 555-0134", "address", "manual", "dmctl")

    assert replies_mod._is_suppressed_prospect(engine_db, ids["prospect_id"]) is True
    replies_mod._apply_side_effects(engine_db, ids["prospect_id"], "whatsapp",
                                    "question", "any chance you do pergolas?", "operator")
    assert engine_db.execute(
        "SELECT outcome FROM outcomes WHERE prospect_id = ?",
        (ids["prospect_id"],)).fetchone()["outcome"] == "dead"


def test_a_dead_prospect_who_is_clear_is_still_reopened_by_a_reply(engine_db, seed_graph):
    """The control: the guard must not have become "always suppressed"."""
    from dm_engine import replies as replies_mod

    ids = seed_graph(engine_db, channel="whatsapp", batch_channel="whatsapp",
                     address="+16025550134", verify_verdict=None)
    replies_mod.set_outcome(engine_db, ids["prospect_id"], "dead", "operator")

    replies_mod._apply_side_effects(engine_db, ids["prospect_id"], "whatsapp",
                                    "question", "any chance you do pergolas?", "operator")
    assert engine_db.execute(
        "SELECT outcome FROM outcomes WHERE prospect_id = ?",
        (ids["prospect_id"],)).fetchone()["outcome"] == "in_progress"
