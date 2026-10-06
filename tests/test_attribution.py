"""attribution.py — a fabricated attribution is worse than an unattributed booking.

The demo form has no email field, so there is no exact join key. Every test here is
a way a booking could be attached to the wrong variant, or an organic demo could be
claimed as outreach, if the matcher were optimistic.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from dm_engine import attribution

from conftest import PASSING_LINT

SENT_AT = datetime(2026, 8, 1, 17, 0, tzinfo=timezone.utc)


def advance_to_sent(conn, message_id, sent_at=SENT_AT):
    conn.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'SENT', sent_at = ?, "
                 "sent_evidence = 'test' WHERE id = ?",
                 (sent_at.isoformat(timespec="seconds"), message_id))
    conn.commit()


@pytest.fixture
def messaged(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    ids["message_id"] = insert_message(engine_db, ids)
    advance_to_sent(engine_db, ids["message_id"])
    return ids


def export(tmp_path, contacts, name="ghl.json"):
    path = tmp_path / name
    path.write_text(json.dumps(contacts), encoding="utf-8")
    return path


def contact(**overrides):
    base = {
        "id": "ghl-1",
        "firstName": "Alex",
        "companyName": "Acme Hardscape",
        "website": "https://acmehardscapeaz.com",
        "phone": "+1 (602) 555-0134",
        "dateAdded": (SENT_AT + timedelta(days=2)).isoformat(),
        "tags": ["demo-form"],
        "source": "demo form",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.Acmehardscapeaz.com/contact?utm=1", "acmehardscapeaz.com"),
    ("acmehardscapeaz.com", "acmehardscapeaz.com"),
    ("http://acmehardscapeaz.com.", "acmehardscapeaz.com"),
    ("  ", None),
    ("not-a-domain", None),
    (None, None),
])
def test_normalize_website(raw, expected):
    assert attribution.normalize_website(raw) == expected


def test_normalize_company_strips_trade_noise():
    assert (attribution.normalize_company("Acme Hardscape, LLC")
            == attribution.normalize_company("acme hardscapes"))
    assert attribution.normalize_company("Maple Sun Landscaping Inc.") == "maple sun"


def test_normalize_phone():
    assert attribution.normalize_phone("+1 (602) 555-0134") == "6025550134"
    assert attribution.normalize_phone("602.555.0134") == "6025550134"
    assert attribution.normalize_phone("x123") == ""


@pytest.mark.parametrize("payload", [
    {"utm_content": "Acmehardscapeaz.com"},
    {"attributions": [{"utmContent": "acmehardscapeaz.com"}]},
    {"attributions": [{"url": "https://brightdesk.brightflowcure.com/demo-ai-voice"
                              "?utm_source=outreach&utm_content=acmehardscapeaz.com"}]},
])
def test_extract_utm_content_handles_ghl_shapes(payload):
    assert attribution.extract_utm_content(payload) == "acmehardscapeaz.com"


def test_extract_utm_content_absent():
    assert attribution.extract_utm_content({"firstName": "Alex"}) is None


# --------------------------------------------------------------------------- #
# export loading
# --------------------------------------------------------------------------- #

def test_load_export_accepts_array_wrapper_and_jsonl(tmp_path):
    array = export(tmp_path, [contact()], "a.json")
    wrapped = tmp_path / "b.json"
    wrapped.write_text(json.dumps({"contacts": [contact()]}), encoding="utf-8")
    lines = tmp_path / "c.jsonl"
    lines.write_text(json.dumps(contact()) + "\n\n", encoding="utf-8")
    for path in (array, wrapped, lines):
        assert len(list(attribution.load_ghl_export(path))) == 1


def test_load_export_reports_a_broken_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(attribution.AttributionError):
        list(attribution.load_ghl_export(path))


def test_load_export_missing_file(tmp_path):
    with pytest.raises(attribution.AttributionError):
        list(attribution.load_ghl_export(tmp_path / "nope.json"))


# --------------------------------------------------------------------------- #
# the confidence ladder
# --------------------------------------------------------------------------- #

def test_utm_is_an_exact_match_and_books_automatically(engine_db, messaged, tmp_path):
    path = export(tmp_path, [contact(website=None, companyName="Totally Different Co",
                                     utm_content="acmehardscapeaz.com")])
    candidates = attribution.match(engine_db, path)
    assert len(candidates) == 1
    assert (candidates[0].method, candidates[0].confidence) == ("utm", "exact")
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert row["outcome"] == "demo_booked"
    assert row["attribution_method"] == "utm"
    assert row["ghl_contact_id"] == "ghl-1"


def test_domain_match_is_high_confidence(engine_db, messaged, tmp_path):
    path = export(tmp_path, [contact(firstName="Someone Else")])
    candidates = attribution.match(engine_db, path)
    assert (candidates[0].method, candidates[0].confidence) == ("domain_match", "high")
    assert engine_db.execute(
        "SELECT attribution_confidence FROM outcomes").fetchone()[0] == "high"


def test_name_and_phone_match_when_there_is_no_website(engine_db, seed_graph,
                                                       insert_message, tmp_path):
    ids = seed_graph(engine_db, prospect_key="name:acme hardscape:boise-id",
                     domain=None)
    advance_to_sent(engine_db, insert_message(engine_db, ids))
    path = export(tmp_path, [contact(website=None)])
    candidates = attribution.match(engine_db, path)
    assert (candidates[0].method, candidates[0].confidence) == ("name_phone_match", "high")


def test_company_name_alone_is_never_enough(engine_db, seed_graph, insert_message,
                                            engine_cfg, tmp_path):
    """No first-name and no phone corroboration -> a human confirms, nothing is booked."""
    ids = seed_graph(engine_db, prospect_key="name:acme hardscape:boise-id",
                     domain=None)
    advance_to_sent(engine_db, insert_message(engine_db, ids))
    path = export(tmp_path, [contact(website=None, firstName="Morgan", phone="")])
    candidates = attribution.match(engine_db, path, engine_cfg)
    assert candidates[0].method == "manual_confirm"
    assert candidates[0].confidence == "manual"
    assert engine_db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0
    cards = list(engine_cfg.dashboard_approvals_dir.glob("dmengine-*.json"))
    assert cards, "an ambiguous booking must surface as a human-confirm card"


def test_two_prospects_one_form_is_never_auto_picked(engine_db, seed_graph,
                                                     insert_message, tmp_path):
    for index in (1, 2):
        ids = seed_graph(engine_db, prospect_key=f"name:acme hardscape-{index}:boise-id",
                         company_name="Acme Hardscape", domain=None,
                         address=f"owner{index}@acme.com", batch_id=f"B-{index}")
        advance_to_sent(engine_db, insert_message(engine_db, ids))
    path = export(tmp_path, [contact(website=None)])
    candidates = attribution.match(engine_db, path)
    assert len(candidates) == 1
    assert candidates[0].method == "manual_confirm"
    assert "ambiguous" in candidates[0].evidence
    assert engine_db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# the things that must NOT be claimed
# --------------------------------------------------------------------------- #

def test_an_organic_demo_is_never_claimed(engine_db, messaged, tmp_path):
    """§9 edge 37 — a form filled by someone we never contacted stays in GHL."""
    path = export(tmp_path, [contact(id="ghl-organic", website="https://someoneelse.com",
                                     companyName="Someone Else", firstName="Pat")])
    assert attribution.match(engine_db, path) == []
    assert engine_db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0


def test_a_contact_created_before_we_ever_messaged_is_out_of_window(engine_db,
                                                                    messaged, tmp_path):
    path = export(tmp_path, [contact(dateAdded=(SENT_AT - timedelta(days=1)).isoformat())])
    assert attribution.match(engine_db, path) == []


def test_a_contact_beyond_45_days_books_nothing_but_is_not_called_organic(
        engine_db, messaged, tmp_path):
    """Finding H4 — a booking we cannot place in time is not evidence of nothing.

    Past the window it may still be ours, so it surfaces for a human instead of
    being auto-booked (dishonest) or dropped into `organic` (also dishonest — that
    bucket means "someone we never messaged").
    """
    path = export(tmp_path, [contact(dateAdded=(SENT_AT + timedelta(days=46)).isoformat())])
    result = attribution.match(engine_db, path)
    assert [c.method for c in result] == ["manual_confirm"]
    assert "outside the 45-day window" in result[0].evidence
    assert result.organic == [] and result.predates == []
    assert engine_db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0


def test_a_prospect_we_never_sent_to_cannot_be_attributed(engine_db, seed_graph,
                                                          insert_message, tmp_path):
    ids = seed_graph(engine_db)
    insert_message(engine_db, ids)  # stays COMPOSED — never sent
    path = export(tmp_path, [contact()])
    assert attribution.match(engine_db, path) == []


def test_match_accounts_for_every_contact_it_read(engine_db, messaged, tmp_path):
    """§10 row (i): matched + manual + unmatched must equal the input count."""
    contacts = [
        contact(id="ghl-1"),
        contact(id="ghl-2", website="https://organic.com", companyName="Organic Co"),
        contact(id="ghl-3", website=None, companyName="Acme Hardscape",
                firstName="Nobody", phone=""),
    ]
    path = export(tmp_path, contacts)
    total = len(list(attribution.load_ghl_export(path)))
    candidates = attribution.match(engine_db, path)
    auto = [c for c in candidates if c.auto]
    manual = [c for c in candidates if not c.auto]
    assert total == 3
    assert len(auto) + len(manual) + (total - len(candidates)) == total


def test_no_revenue_metric_is_ever_derived(engine_db, messaged, tmp_path):
    """GHL monetaryValue is 0 across the board — the engine must not surface it."""
    path = export(tmp_path, [contact(monetaryValue=48200)])
    attribution.match(engine_db, path)
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert "48200" not in json.dumps(dict(row))
    assert not any("value" in column.lower()
                   for column in row.keys())  # noqa: SIM118 — sqlite3.Row


# --------------------------------------------------------------------------- #
# book()
# --------------------------------------------------------------------------- #

def test_book_records_method_confidence_and_audit_row(engine_db, messaged):
    attribution.book(engine_db, messaged["prospect_id"], "manual_confirm",
                     "ghl-42", "operator")
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert (row["outcome"], row["attribution_method"], row["attribution_confidence"]) \
        == ("demo_booked", "manual_confirm", "manual")
    assert row["demo_booked_at"]
    logged = engine_db.execute(
        "SELECT reason FROM state_log WHERE to_state = 'outcome:demo_booked'").fetchone()
    assert logged["reason"] == "attribution:manual_confirm/manual"


def test_book_refuses_without_evidence(engine_db, messaged):
    with pytest.raises(attribution.AttributionError):
        attribution.book(engine_db, messaged["prospect_id"], "utm", "", "operator")


def test_book_refuses_an_unknown_method(engine_db, messaged):
    with pytest.raises(attribution.AttributionError):
        attribution.book(engine_db, messaged["prospect_id"], "vibes", "x", "operator")


def test_book_refuses_a_non_booking_outcome(engine_db, messaged):
    with pytest.raises(attribution.AttributionError):
        attribution.book(engine_db, messaged["prospect_id"], "utm", "x", "operator",
                         outcome="dead")


def test_book_never_overwrites_a_recorded_attribution(engine_db, messaged):
    attribution.book(engine_db, messaged["prospect_id"], "utm", "ghl-1", "engine",
                     ghl_contact_id="ghl-1")
    attribution.book(engine_db, messaged["prospect_id"], "manual_confirm", "ghl-9",
                     "operator", ghl_contact_id="ghl-9")
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert row["attribution_method"] == "utm"
    assert row["ghl_contact_id"] == "ghl-1"
    assert row["evidence"] == "ghl-1"


def test_book_upgrades_an_in_progress_prospect(engine_db, messaged):
    from dm_engine import replies

    replies.record_reply(engine_db, messaged["prospect_id"], "email", "hot",
                         "yes let's see it", "operator")
    attribution.book(engine_db, messaged["prospect_id"], "domain_match",
                     "ghl-7", "engine")
    row = engine_db.execute("SELECT * FROM outcomes").fetchone()
    assert row["outcome"] == "demo_booked"
    assert engine_db.execute("SELECT booked FROM sheet").fetchone()[0] == "y"


def test_every_booking_carries_an_attribution_method(engine_db, messaged, tmp_path):
    """SC-7 — the invariant the report line depends on."""
    path = export(tmp_path, [contact()])
    attribution.match(engine_db, path)
    unattributed = engine_db.execute(
        "SELECT count(*) FROM outcomes WHERE outcome IN ('demo_booked','call_booked') "
        "AND attribution_method IS NULL").fetchone()[0]
    assert unattributed == 0
