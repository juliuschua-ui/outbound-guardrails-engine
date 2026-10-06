"""Suppression — the safety system. PLAN.md §9 edges 33-34, §10 row (e), §12.3.

Every test here is really one question: can a human who said "stop" be messaged
again? The answers must be no through the DB check, no through the G8 trigger, and
no even when the lookup itself breaks.
"""

import sqlite3

import pytest

from dm_engine import suppression
from dm_engine.attribution import normalize_phone
from dm_engine.channels import base
from dm_engine.suppression import (
    MARKER_TOKEN,
    add,
    append_to_file,
    canonical_address,
    canonical_messenger,
    canonical_phone,
    file_addresses,
    is_suppressed,
    sync_from_file,
)

REAL_FILE_SHAPE = """\
# Outreach suppression list — confirmed-dead / hard-bounced addresses.
# One address per line, lowercase. Blank lines and # comments ignored.

# --- dead domains / NXDOMAIN ---
vince@northstarturfandpavers.com
info@eastvalleyhardscapes.com

# --- address not found / no mailbox ---
pj@wedopavers.com
"""


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

def test_exact_address_match(engine_db):
    add(engine_db, "email", "Alex@Acme.com", "address", "bounce", "dmctl")
    assert is_suppressed(engine_db, "email", "alex@acme.com") == "bounce"
    assert is_suppressed(engine_db, "email", "  ALEX@ACME.COM ") == "bounce"
    # A bounce is a fact about ONE mailbox, so it stops there.
    assert is_suppressed(engine_db, "email", "someone-else@acme.com") is None


def test_a_withdrawal_covers_the_company_but_a_bounce_does_not(engine_db):
    """Finding H4 — 'stop emailing me' is a fact about the company, not a mailbox.

    The twin-row path (same company re-imported under a second market, later scraped
    at `info@`) is exactly how a company that unsubscribed used to get a fresh
    Message 1. A domain-scope row is what closes it.
    """
    add(engine_db, "email", "owner@pinecrest.example", "address", "bounce", "reply_scan")
    assert is_suppressed(engine_db, "email", "info@pinecrest.example") is None

    add(engine_db, "email", "owner@acme.example", "address", "unsubscribe", "reply_scan")
    assert is_suppressed(engine_db, "email", "info@acme.example") == "unsubscribe"
    assert is_suppressed(engine_db, "email", "sales@mail.acme.example") == "unsubscribe"


def test_a_withdrawal_from_a_freemail_box_never_suppresses_the_provider(engine_db):
    """The one case where domain propagation would be catastrophic.

    Contractors routinely use consumer mailboxes. Writing a domain row for gmail.com
    would suppress every Gmail-hosted prospect in the database from one unsubscribe.
    """
    add(engine_db, "email", "someone@gmail.com", "address", "unsubscribe", "reply_scan")
    assert is_suppressed(engine_db, "email", "someone@gmail.com") == "unsubscribe"
    assert is_suppressed(engine_db, "email", "unrelated-owner@gmail.com") is None
    assert engine_db.execute(
        "SELECT count(*) FROM suppression WHERE scope = 'domain'").fetchone()[0] == 0


def test_clear_contact_returns_none(engine_db):
    assert is_suppressed(engine_db, "email", "fresh@example.com") is None


def test_channel_scoping(engine_db):
    add(engine_db, "email", "alex@acme.com", "address", "hard_no", "dmctl")
    assert is_suppressed(engine_db, "linkedin", "https://linkedin.com/in/alex-x") is None

    add(engine_db, "all", "alex@acme.com", "address", "legal", "dmctl")
    assert is_suppressed(engine_db, "linkedin", "alex@acme.com") == "legal"
    assert is_suppressed(engine_db, "instagram", "alex@acme.com") == "legal"


def test_channel_all_query_sees_every_row(engine_db):
    add(engine_db, "linkedin", "https://linkedin.com/in/alex-x", "address",
        "hard_no", "dmctl")
    assert is_suppressed(engine_db, "all", "https://linkedin.com/in/alex-x") == "hard_no"


def test_the_same_human_at_a_second_company_is_still_suppressed(engine_db):
    """§9 edge 34 — they said no; their next company does not reset consent."""
    add(engine_db, "linkedin", "alex-x", "person", "hard_no", "reply_scan")
    # different company, different profile URL, same person key
    assert is_suppressed(engine_db, "linkedin",
                         "https://linkedin.com/company/valley-pavers/alex",
                         person_key="alex-x") == "hard_no"


def test_person_key_also_matches_an_address_scoped_row(engine_db):
    add(engine_db, "email", "alex@acme.com", "address", "hard_no", "reply_scan")
    assert is_suppressed(engine_db, "email", "alex@meadowpavers.com",
                         person_key="alex@acme.com") == "hard_no"


def test_domain_scope_matches_the_same_way_the_g8_trigger_does(engine_db):
    add(engine_db, "email", "@deadco.com", "domain", "bounce", "reply_scan")
    assert is_suppressed(engine_db, "email", "anyone@deadco.com") == "bounce"
    assert is_suppressed(engine_db, "email", "anyone@liveco.com") is None


def test_address_scope_wins_over_domain_scope_for_the_reported_reason(engine_db):
    add(engine_db, "email", "@deadco.com", "domain", "bounce", "reply_scan")
    add(engine_db, "email", "alex@deadco.com", "address", "hard_no", "reply_scan")
    assert is_suppressed(engine_db, "email", "alex@deadco.com") == "hard_no"


# --------------------------------------------------------------------------- #
# the DM channels: a phone number and a page handle are not email addresses
# --------------------------------------------------------------------------- #

#: Every spelling of one obviously-fake 555 number. `canonical_email` folded none of
#: these onto each other, so before the DM build a hard-no taken on one of them left
#: the other two contactable — which is messaging somebody who said stop.
PHONE_SPELLINGS = ["+1 (602) 555-0134", "602-555-0134", "16025550134",
                   "+16025550134", "602.555.0134", "(602) 555 0134"]

#: Every spelling of one Page. m.me, fb.me and facebook.com are three front doors to
#: the same inbox.
PAGE_SPELLINGS = ["https://m.me/example-hardscape",
                  "https://www.facebook.com/example-hardscape/",
                  "http://facebook.com/Example-Hardscape",
                  "https://fb.me/example-hardscape",
                  "example-hardscape",
                  # Found by running the real thing: a profile URL copied out of a
                  # browser address bar carries a trailing '#', and a scraper hands
                  # back '?ref=...'. Both used to canonicalize to a DIFFERENT
                  # identity than the bare URL, so a hard-no recorded against one
                  # spelling silently failed to block the other — the same failure
                  # class as an unfolded '+tag' on an email.
                  "https://www.facebook.com/example-hardscape#",
                  "https://www.facebook.com/example-hardscape/#",
                  "https://www.facebook.com/example-hardscape?ref=bookmarks",
                  "https://m.me/example-hardscape?ref=page_internal"]


@pytest.mark.parametrize("written", PHONE_SPELLINGS)
def test_a_whatsapp_hard_no_matches_every_spelling_of_the_number(engine_db, written):
    """Punctuation is not identity. Suppress one spelling, block all six."""
    add(engine_db, "whatsapp", written, "address", "hard_no", "reply_scan")
    for spelling in PHONE_SPELLINGS:
        assert is_suppressed(engine_db, "whatsapp", spelling) == "hard_no", spelling
    # a different number is untouched
    assert is_suppressed(engine_db, "whatsapp", "602-555-0199") is None


@pytest.mark.parametrize("written", PAGE_SPELLINGS)
def test_a_messenger_hard_no_matches_every_spelling_of_the_page(engine_db, written):
    add(engine_db, "messenger", written, "address", "hard_no", "reply_scan")
    for spelling in PAGE_SPELLINGS:
        assert is_suppressed(engine_db, "messenger", spelling) == "hard_no", spelling
    assert is_suppressed(engine_db, "messenger",
                         "https://m.me/some-other-company") is None


def test_canonical_phone_is_the_same_convention_as_attribution(engine_db):
    """Not a third convention: digits, US country code dropped — attribution.py's.

    `canonical_phone` is restated rather than imported because the identical
    operation has to be expressible in SQLite (no regex) for `contacts.addr_phone`.
    This is the lockstep guard that keeps the restatement honest.
    """
    for number in PHONE_SPELLINGS + ["+44 20 7946 0958", "1-800-555-0199"]:
        assert canonical_phone(number) == normalize_phone(number), number


def test_the_sql_canon_column_agrees_with_the_python_canonicalizer(engine_db):
    """The Python pre-check and trigger G8 must never disagree about who is suppressed.

    Migration 005 reimplements all three canonical forms as generated columns so the
    SQL belt holds against a raw `sqlite3` write. Two implementations of one rule is
    exactly the shape that drifts, so this runs both over the same table.

    KNOWN DRIFT, deliberately not asserted here: `facebook.com/profile.php?id=<N>`.
    `canonical_messenger` now reads the `id=` (see PROFILE_SPELLINGS above);
    migration 005's `addr_handle` still folds every such URL onto 'profile.php'.
    Closing it needs a migration 006 restating that CASE over `addr_lc` (not
    `addr_nofrag`, which has already dropped the query) — until then a profile.php
    row belongs in this table's cases and would fail.
    """
    engine_db.execute(
        "INSERT INTO prospects (prospect_key, company_name, market, us_state, timezone, "
        "source) VALUES ('x.example', 'X', 'Boise ID', 'ID', 'America/Boise', 'manual')")
    cases = ([("whatsapp", value) for value in PHONE_SPELLINGS
              + ["+44 20 7946 0958",
                 # not a number at all: a person key must survive untouched
                 "alex-x"]]
             + [("messenger", value) for value in PAGE_SPELLINGS
                + ["https://www.facebook.com/messages/t/12345/",
                   "https://messenger.com/t/12345",
                   # a page whose handle ENDS in 't' must survive the 't/' strip
                   "https://www.facebook.com/pinelandscapemgmt/"]]
             + [("email", "Owner+Quote@example.com"), ("email", "o.wner@googlemail.com"),
                ("linkedin", "https://linkedin.com/in/alex-x")])
    for channel, address in cases:
        engine_db.execute(
            "INSERT INTO contacts (prospect_id, channel, address, address_source) "
            "VALUES (1, ?, ?, 'website')", (channel, address))
        sql_canon = engine_db.execute(
            "SELECT address_canon FROM contacts WHERE channel = ? AND address = ?",
            (channel, address)).fetchone()[0]
        assert sql_canon == canonical_address(channel, address), (channel, address)


#: One Facebook profile with no vanity handle, written every way a browser or a
#: scraper hands it back. The identity lives in the QUERY here and nowhere else, so
#: the query strip that is right for '?ref=...' folded all of these — and every
#: OTHER person's profile.php URL — onto the literal path 'profile.php'.
PROFILE_SPELLINGS = ["https://www.facebook.com/profile.php?id=61550000000001",
                     "https://facebook.com/profile.php?id=61550000000001",
                     "https://www.facebook.com/profile.php?id=61550000000001#",
                     "https://www.facebook.com/profile.php/?id=61550000000001",
                     "https://www.facebook.com/profile.php?id=61550000000001&ref=bookmarks",
                     "http://facebook.com/profile.php?ref=bookmarks&id=61550000000001"]


@pytest.mark.parametrize("written", PROFILE_SPELLINGS)
def test_a_messenger_hard_no_matches_every_spelling_of_one_profile_url(engine_db, written):
    add(engine_db, "messenger", written, "address", "hard_no", "reply_scan")
    for spelling in PROFILE_SPELLINGS:
        assert is_suppressed(engine_db, "messenger", spelling) == "hard_no", spelling


def test_two_profile_php_urls_are_two_different_people(engine_db):
    """The `id=` IS the identity: keying on the path suppressed everybody at once.

    `facebook.com/profile.php?id=<N>` is what a profile with no vanity handle looks
    like, and the canonicalizer dropped the query before deriving the handle — so
    every such URL became the string 'profile.php'. One hard-no then blocked every
    unrelated prospect whose page is a profile.php URL, and moved each of them to
    SUPPRESSED, which is permanent by design.
    """
    add(engine_db, "messenger", "https://www.facebook.com/profile.php?id=61550000000001",
        "address", "hard_no", "reply_scan")

    assert canonical_messenger(
        "https://www.facebook.com/profile.php?id=61550000000001") == "61550000000001"
    assert is_suppressed(
        engine_db, "messenger",
        "https://www.facebook.com/profile.php?id=61550000000002") is None
    assert is_suppressed(engine_db, "messenger",
                         "https://m.me/example-hardscape") is None


def test_a_profile_url_with_no_id_names_nobody(engine_db):
    """It identifies no one, so it must not become a suppression row that matches.

    An empty canonical address would sit in the table matching nothing — a silent
    no-op where a human thinks they recorded a hard-no. `prospects.messenger_handle`
    treats the same value as an "unusable messenger url" and skips the contact.
    """
    assert canonical_messenger("https://www.facebook.com/profile.php") == ""
    assert canonical_messenger("https://www.facebook.com/profile.php?ref=bookmarks") == ""
    with pytest.raises(ValueError, match="carries no identity"):
        add(engine_db, "messenger", "https://www.facebook.com/profile.php",
            "address", "hard_no", "dmctl")


def test_a_page_handle_ending_in_t_is_not_truncated():
    """The trailing-slash trim has to happen before the 't/' strip, or a Page called
    '…mgmt/' silently becomes '…mgm' and never matches its own suppression row."""
    assert canonical_messenger(
        "https://www.facebook.com/pinelandscapemgmt/") == "pinelandscapemgmt"
    assert canonical_messenger(
        "https://www.facebook.com/messages/t/12345/") == "12345"


def test_a_person_scope_hard_no_reaches_every_channel(engine_db):
    """§9 edge 34, widened for DM-first outreach.

    Sam reaches the same owner on WhatsApp, Messenger and email. A person-scope
    suppression is a fact about that human, not about one app, so a hard-no taken in
    WhatsApp has to close the other two doors as well — a channel-scoped person row
    would leave a hole nobody would guess was there.
    """
    add(engine_db, "whatsapp", "alex-x", "person", "hard_no", "reply_scan")
    for channel, address in (("whatsapp", "+1 602 555 0134"),
                             ("messenger", "https://m.me/example-hardscape"),
                             ("email", "alex@example.com"),
                             ("linkedin", "https://linkedin.com/in/alex-x")):
        assert is_suppressed(engine_db, channel, address,
                             person_key="alex-x") == "hard_no", channel


def test_address_scope_still_stays_on_its_own_channel(engine_db):
    """Only `person` crosses. A dead mailbox must not mute the company's Page."""
    add(engine_db, "email", "alex@example.com", "address", "bounce", "reply_scan")
    assert is_suppressed(engine_db, "messenger",
                         "https://m.me/example-hardscape") is None
    assert is_suppressed(engine_db, "whatsapp", "602-555-0134") is None


def test_a_dm_withdrawal_writes_no_domain_row(engine_db):
    """Domain scope is email-shaped and deliberately does nothing here.

    There is no domain in a phone number, and a Messenger handle's "domain" is
    facebook.com — a domain row for it would suppress every Page-reachable prospect
    in the database from a single hard-no.
    """
    add(engine_db, "whatsapp", "+1 602 555 0134", "address", "hard_no", "reply_scan")
    add(engine_db, "messenger", "https://m.me/example-hardscape", "address",
        "unsubscribe", "reply_scan")
    assert engine_db.execute(
        "SELECT count(*) FROM suppression WHERE scope = 'domain'").fetchone()[0] == 0
    # ... and an unrelated Page is still reachable
    assert is_suppressed(engine_db, "messenger", "https://m.me/other-company") is None


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_domain_scope_is_refused_outright_on_a_dm_channel(engine_db, channel):
    with pytest.raises(ValueError, match="meaningless"):
        add(engine_db, channel, "example.com", "domain", "hard_no", "dmctl")


def test_a_domain_row_cannot_match_a_phone_number_by_its_tail(engine_db):
    """The suffix fallback that exists for LinkedIn URLs must not reach a number.

    `_domain_hit` falls back to `endswith` for an address with no host, which is how
    a LinkedIn profile URL is matched. Applied to '+16025550134' a domain row of
    '0134' would suppress every number ending in those digits.
    """
    add(engine_db, "all", "0134", "domain", "hard_no", "dmctl")
    assert is_suppressed(engine_db, "whatsapp", "+1 602 555 0134") is None
    assert is_suppressed(engine_db, "all", "+16025550134") is None


def test_dm_channels_are_valid_everywhere_a_channel_is_named(engine_db):
    assert {"whatsapp", "messenger"} <= suppression.CHANNELS
    add(engine_db, "whatsapp", "+1 602 555 0134", "address", "manual", "dmctl")
    add(engine_db, "messenger", "https://m.me/example-hardscape", "address",
        "manual", "dmctl")
    assert engine_db.execute(
        "SELECT count(*) FROM suppression WHERE channel IN ('whatsapp','messenger')"
    ).fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# fail closed
# --------------------------------------------------------------------------- #

def test_unknown_channel_fails_closed(engine_db):
    verdict = is_suppressed(engine_db, "carrier-pigeon", "alex@acme.com")
    assert verdict is not None and verdict.startswith("fail_closed")


def test_no_identifier_at_all_fails_closed(engine_db):
    assert is_suppressed(engine_db, "email", "").startswith("fail_closed")
    assert is_suppressed(engine_db, "email", None).startswith("fail_closed")


def test_a_broken_lookup_fails_closed(engine_db, tmp_path):
    """A database that cannot answer must not be read as 'go ahead and send'."""
    broken = sqlite3.connect(tmp_path / "empty.db")     # no suppression table at all
    broken.row_factory = sqlite3.Row
    verdict = is_suppressed(broken, "email", "alex@acme.com")
    broken.close()
    assert verdict is not None and verdict.startswith("fail_closed")


@pytest.mark.parametrize("channel,address,scope,reason", [
    ("carrier-pigeon", "a@b.com", "address", "hard_no"),
    ("email", "a@b.com", "planet", "hard_no"),
    ("email", "a@b.com", "address", "because-i-said-so"),
    ("email", "   ", "address", "hard_no"),
])
def test_add_validates_before_the_database_does(engine_db, channel, address, scope, reason):
    with pytest.raises(ValueError):
        add(engine_db, channel, address, scope, reason, "test")


def test_add_is_idempotent(engine_db):
    add(engine_db, "email", "alex@acme.com", "address", "bounce", "reply_scan")
    add(engine_db, "email", "alex@acme.com", "address", "hard_no", "dmctl")
    rows = engine_db.execute(
        "SELECT reason FROM suppression WHERE scope = 'address'").fetchall()
    assert [r["reason"] for r in rows] == ["bounce"]          # first record stands
    # The second call was a consent withdrawal, so it still widened the block to the
    # company even though the address row it tried to write was already there.
    assert engine_db.execute(
        "SELECT reason FROM suppression WHERE scope = 'domain'"
    ).fetchone()["reason"] == "hard_no"


# --------------------------------------------------------------------------- #
# the shared file
# --------------------------------------------------------------------------- #

def test_sync_from_file_matches_the_skill_parser(engine_cfg, engine_db):
    engine_cfg.suppression_file.write_text(REAL_FILE_SHAPE)
    added = sync_from_file(engine_db, engine_cfg.suppression_file)

    assert added == 3                                        # comments and blanks ignored
    assert is_suppressed(engine_db, "email", "pj@wedopavers.com") == "dead_mailbox"
    assert sync_from_file(engine_db, engine_cfg.suppression_file) == 0   # idempotent


def test_sync_from_a_missing_file_is_a_no_op(engine_cfg, engine_db):
    assert sync_from_file(engine_db, engine_cfg.suppression_file) == 0


def test_append_to_file_writes_bare_addresses_under_the_marker(engine_cfg):
    path = engine_cfg.suppression_file
    path.write_text(REAL_FILE_SHAPE)
    append_to_file(path, "Bo@Pinecrest.com", "bounce")

    text = path.read_text()
    assert REAL_FILE_SHAPE in text                            # nothing rewritten
    assert f"{MARKER_TOKEN}: bounce" in text
    # create_drafts.load_suppression() reads a line verbatim — a trailing comment on
    # the address line would silently break the skill's own suppression check.
    assert "bo@pinecrest.com" in file_addresses(path)
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            assert "#" not in line


def test_append_to_file_is_idempotent(engine_cfg):
    path = engine_cfg.suppression_file
    path.write_text(REAL_FILE_SHAPE)
    append_to_file(path, "bo@pinecrest.com", "bounce")
    once = path.read_text()
    append_to_file(path, "BO@pinecrest.com", "bounce")
    append_to_file(path, "bo@pinecrest.com", "hard_no")
    assert path.read_text() == once


def test_append_reuses_an_open_header_but_opens_a_new_one_per_reason(engine_cfg):
    path = engine_cfg.suppression_file
    path.write_text(REAL_FILE_SHAPE)
    append_to_file(path, "a@x.com", "bounce")
    append_to_file(path, "b@x.com", "bounce")
    append_to_file(path, "c@x.com", "hard_no")

    headers = [ln for ln in path.read_text().splitlines() if MARKER_TOKEN in ln]
    assert len(headers) == 2
    assert file_addresses(path) >= {"a@x.com", "b@x.com", "c@x.com"}


def test_append_creates_the_file_when_it_does_not_exist(engine_cfg):
    path = engine_cfg.suppression_file
    assert not path.exists()
    append_to_file(path, "a@x.com", "unsubscribe")
    assert file_addresses(path) == {"a@x.com"}


def test_append_handles_a_file_with_no_trailing_newline(engine_cfg):
    path = engine_cfg.suppression_file
    path.write_text("existing@x.com")
    append_to_file(path, "new@x.com", "bounce")
    assert file_addresses(path) == {"existing@x.com", "new@x.com"}


def test_append_rejects_an_unknown_reason(engine_cfg):
    with pytest.raises(ValueError):
        append_to_file(engine_cfg.suppression_file, "a@x.com", "vibes")


def test_file_round_trips_the_reason_back_into_the_database(engine_cfg, engine_db):
    """Phase 2 DoD: two-way sync against a fixture copy of the shared list."""
    path = engine_cfg.suppression_file
    path.write_text(REAL_FILE_SHAPE)
    append_to_file(path, "alex@acme.com", "hard_no")

    sync_from_file(engine_db, path)
    assert is_suppressed(engine_db, "email", "alex@acme.com") == "hard_no"
    assert is_suppressed(engine_db, "email", "vince@northstarturfandpavers.com") == "dead_mailbox"


# --------------------------------------------------------------------------- #
# sync() / require_sync() — the gate that refuses to treat a lost list as empty
# --------------------------------------------------------------------------- #

def test_sync_with_no_file_and_no_history_warns_but_continues(engine_cfg, engine_db):
    """A fresh machine has nothing to import. Loud, not fatal — and recorded, so
    the NEXT sync can tell 'never had one' from 'the list disappeared'."""
    report = suppression.sync(engine_db, engine_cfg)

    assert report.present is False
    assert any("no shared suppression list" in warning for warning in report.warnings)
    assert suppression.last_sync(engine_db)["status"] == "absent"


def test_sync_refuses_once_the_shared_list_has_vanished(engine_cfg, engine_db):
    """A missing list is not an empty one — that is the failure that re-emails
    everyone who was on it. Once a sync has imported from the file, its absence
    stops the run instead of quietly importing nothing."""
    engine_cfg.suppression_file.write_text(REAL_FILE_SHAPE)
    first = suppression.sync(engine_db, engine_cfg)
    assert (first.present, first.file_addresses, first.added) == (True, 3, 3)

    engine_cfg.suppression_file.unlink()

    with pytest.raises(suppression.SuppressionSyncError, match="GONE"):
        suppression.sync(engine_db, engine_cfg)


def test_sync_refuses_a_line_carrying_a_control_character(engine_cfg, engine_db):
    """create_drafts.py reads a line verbatim, so a NUL can smuggle a second
    address line into the shared file and suppress somebody unrelated."""
    engine_cfg.suppression_file.write_text(
        "vince@northstarturfandpavers.com\nbad\x00line@example.com\n")
    with pytest.raises(suppression.SuppressionSyncError, match="control character"):
        suppression.sync(engine_db, engine_cfg)


def test_sync_refuses_a_list_it_cannot_read(engine_cfg, engine_db):
    """Present but unreadable is the same answer as unreadable: refuse."""
    engine_cfg.suppression_file.mkdir()          # exists, but is not a file
    with pytest.raises(suppression.SuppressionSyncError, match="cannot read"):
        suppression.sync(engine_db, engine_cfg)


def test_sync_refuses_a_list_that_is_not_utf8(engine_cfg, engine_db):
    engine_cfg.suppression_file.write_bytes(b"vince@northstarturfandpavers.com\n\xff\xfe\n")
    with pytest.raises(suppression.SuppressionSyncError, match="not valid UTF-8"):
        suppression.sync(engine_db, engine_cfg)


def test_sync_warns_when_the_list_shrinks_but_keeps_running(engine_cfg, engine_db):
    """Suppression is append-only by design, so a shrink is a hand edit worth
    seeing — and nobody already imported is released by it."""
    engine_cfg.suppression_file.write_text(REAL_FILE_SHAPE)
    suppression.sync(engine_db, engine_cfg)
    engine_cfg.suppression_file.write_text("pj@wedopavers.com\n")

    report = suppression.sync(engine_db, engine_cfg)

    assert report.present and report.file_addresses == 1
    assert any("shrank" in warning for warning in report.warnings)
    assert is_suppressed(engine_db, "email", "vince@northstarturfandpavers.com")


def test_sync_imports_the_file_and_propagates_a_withdrawal(engine_cfg, engine_db,
                                                            seed_graph):
    """The happy path nothing exercised: file present, rows imported, a
    consent-withdrawal reason carried through to the prospect, run recorded 'ok'."""
    ids = seed_graph(engine_db, address="owner@pinecrest.example")
    engine_cfg.suppression_file.write_text(
        f"# --- {MARKER_TOKEN}: unsubscribe ---\nowner@pinecrest.example\n")

    report = suppression.sync(engine_db, engine_cfg)

    assert (report.present, report.file_addresses, report.added) == (True, 1, 1)
    assert report.prospects_suppressed == 1
    assert engine_db.execute(
        "SELECT state FROM prospects WHERE id = ?",
        (ids["prospect_id"],)).fetchone()["state"] == "SUPPRESSED"
    assert suppression.last_sync(engine_db)["status"] == "ok"


def test_require_sync_names_the_gate_it_refused(engine_cfg, engine_db):
    """`stage: the shared suppression list ... is GONE` — the operator needs to
    know WHICH gate stopped, not just that something did."""
    engine_cfg.suppression_file.write_text(REAL_FILE_SHAPE)
    suppression.sync(engine_db, engine_cfg)
    engine_cfg.suppression_file.unlink()

    with pytest.raises(suppression.SuppressionSyncError) as caught:
        suppression.require_sync(engine_db, engine_cfg, "stage")
    assert str(caught.value).startswith("stage: ")


# --------------------------------------------------------------------------- #
# the shared file stores the CANONICAL address, exactly as add() does
# --------------------------------------------------------------------------- #

def test_a_tagged_file_line_reaches_the_sql_belt_not_just_the_python_check(
        engine_cfg, engine_db, seed_graph, insert_message):
    """The file sync used to store the raw lowercased line, `add()` the canonical
    one — so the two disagreed about who is suppressed.

    Every SQL-side matcher (trigger G8 and its mirror `channels.base`
    `suppression_hit`) canonicalizes the CONTACT and compares it against
    `suppression.address` verbatim, so a '+tag' spelling on the shared list matched
    nothing there. The STAGE gate calls `suppression_hit` and never
    `is_suppressed`, which is how a message already APPROVED when the unsubscribe
    landed could still be staged into a Gmail draft.

    A gmail.com box on purpose: it is a freemail domain, so no `scope='domain'` row
    is written and the address row is the only thing that can match — which is what
    makes this an assertion about the stored spelling rather than about the
    company-wide fallback.
    """
    ids = seed_graph(engine_db, address="owner@gmail.com")
    engine_cfg.suppression_file.write_text(
        f"# --- {MARKER_TOKEN}: unsubscribe ---\nO.wner+Quote@Gmail.com\n")

    suppression.sync(engine_db, engine_cfg)

    assert engine_db.execute(
        "SELECT count(*) FROM suppression WHERE scope = 'domain'").fetchone()[0] == 0
    assert is_suppressed(engine_db, "email", "owner@gmail.com") == "unsubscribe"
    assert base.suppression_hit(engine_db, ids["contact_id"])
    assert engine_db.execute(
        "SELECT state FROM prospects WHERE id = ?",
        (ids["prospect_id"],)).fetchone()["state"] == "SUPPRESSED"
    with pytest.raises(sqlite3.DatabaseError, match="G12"):
        insert_message(engine_db, ids)


def test_a_tagged_file_line_arms_g8_without_a_withdrawal(engine_cfg, engine_db,
                                                          seed_graph, insert_message):
    """`dead_mailbox` (the file's default reason) does not move the prospect, so
    this pins trigger G8 itself rather than the prospect gate in front of it."""
    ids = seed_graph(engine_db, address="pj@wedopavers.example")
    engine_cfg.suppression_file.write_text("PJ+Bounce@Wedopavers.example\n")

    assert sync_from_file(engine_db, engine_cfg.suppression_file) == 1

    assert engine_db.execute(
        "SELECT state FROM prospects WHERE id = ?",
        (ids["prospect_id"],)).fetchone()["state"] != "SUPPRESSED"
    with pytest.raises(sqlite3.DatabaseError, match="G8"):
        insert_message(engine_db, ids)


# --------------------------------------------------------------------------- #
# the database-level belt (G8)
# --------------------------------------------------------------------------- #

def test_a_withdrawal_refuses_a_message_at_the_prospect_gate(engine_db, seed_graph,
                                                             insert_message):
    """A consent withdrawal now trips G12 (prospect SUPPRESSED) before G8 (contact).

    G8 is still the backstop for an address suppressed without a withdrawal — see
    `test_file_sync_alone_is_enough_to_arm_g8`. This asserts the stricter, earlier
    gate rather than the one it superseded.
    """
    ids = seed_graph(engine_db, address="alex@acme.com")
    add(engine_db, "email", "alex@acme.com", "address", "hard_no", "reply_scan")

    assert engine_db.execute(
        "SELECT state FROM prospects WHERE id = ?",
        (ids["prospect_id"],)).fetchone()["state"] == "SUPPRESSED"
    with pytest.raises(sqlite3.DatabaseError, match="G12"):
        insert_message(engine_db, ids)


def test_file_sync_alone_is_enough_to_arm_g8(engine_cfg, engine_db, seed_graph, insert_message):
    """§10 row (e): an address added to the shared FILE must block a message once synced."""
    ids = seed_graph(engine_db, address="alex@acme.com")
    engine_cfg.suppression_file.write_text("alex@acme.com\n")
    sync_from_file(engine_db, engine_cfg.suppression_file)

    with pytest.raises(sqlite3.DatabaseError, match="G8"):
        insert_message(engine_db, ids)


def test_g8_blocks_a_whatsapp_number_written_a_different_way(engine_db, seed_graph,
                                                             insert_message):
    """The SQL belt, not just the Python pre-check (migration 005's whole point).

    `manual` is deliberately not a consent-withdrawal reason, so the prospect is NOT
    moved to SUPPRESSED and G12 stays out of the way — this asserts G8 itself folds
    the punctuation.
    """
    ids = seed_graph(engine_db, channel="whatsapp", batch_channel="whatsapp",
                     address="+16025550134", verify_verdict=None,
                     address_source="website")
    add(engine_db, "whatsapp", "(602) 555-0134", "address", "manual", "dmctl")

    assert engine_db.execute(
        "SELECT state FROM prospects WHERE id = ?",
        (ids["prospect_id"],)).fetchone()["state"] != "SUPPRESSED"
    with pytest.raises(sqlite3.DatabaseError, match="G8"):
        insert_message(engine_db, ids, subject=None)


def test_g8_blocks_a_messenger_page_written_a_different_way(engine_db, seed_graph,
                                                            insert_message):
    ids = seed_graph(engine_db, channel="messenger", batch_channel="messenger",
                     address="https://www.facebook.com/example-hardscape/",
                     verify_verdict=None, address_source="website")
    add(engine_db, "messenger", "https://m.me/example-hardscape", "address",
        "manual", "dmctl")
    with pytest.raises(sqlite3.DatabaseError, match="G8"):
        insert_message(engine_db, ids, subject=None)


def test_g8_lets_a_person_row_cross_from_whatsapp_to_email(engine_db, seed_graph,
                                                           insert_message):
    """The trigger has to agree with `is_suppressed` about cross-channel person scope.

    `manual` again, so this is G8 and not the prospect gate.
    """
    ids = seed_graph(engine_db, address="alex@example.com", person_key="alex-x")
    add(engine_db, "whatsapp", "alex-x", "person", "manual", "reply_scan")
    with pytest.raises(sqlite3.DatabaseError, match="G8"):
        insert_message(engine_db, ids)


def test_g8_does_not_let_a_domain_row_reach_a_whatsapp_contact(engine_db, seed_graph,
                                                               insert_message):
    ids = seed_graph(engine_db, channel="whatsapp", batch_channel="whatsapp",
                     address="+16025550134", verify_verdict=None,
                     address_source="website")
    engine_db.execute(
        "INSERT INTO suppression (channel, address, scope, reason, source) "
        "VALUES ('all', '0134', 'domain', 'manual', 'dmctl')")
    engine_db.commit()
    message_id = insert_message(engine_db, ids, subject=None)
    assert message_id


def test_person_scoped_suppression_reaches_the_second_company(engine_db, seed_graph,
                                                              insert_message):
    seed_graph(engine_db, address="alex@acme.com", person_key="alex-x")
    second = seed_graph(engine_db, prospect_key="meadowpavers.com",
                        company_name="Meadow Pavers", domain="meadowpavers.com",
                        address="alex@meadowpavers.com", person_key="alex-x",
                        batch_id="B-TEST-2")
    add(engine_db, "email", "alex-x", "person", "hard_no", "reply_scan")

    assert is_suppressed(engine_db, "email", "alex@meadowpavers.com",
                         person_key="alex-x") == "hard_no"
    # G12 (prospect SUPPRESSED) now fires ahead of G8 (contact suppressed): the
    # withdrawal propagates to every prospect the person is reachable at, so the
    # second company is blocked at the prospect gate rather than the address one.
    with pytest.raises(sqlite3.DatabaseError, match="G12"):
        insert_message(engine_db, second)
