"""Prospect intake, identity and dedup. PLAN.md §9 "Prospect intake / identity", §13.

The interesting tests here are the ugly ones: identity is what a variant assignment
hangs off, so a domain that normalizes two ways is an experiment that reports two
different variants for one company.
"""

import json
import sqlite3

import pytest
from conftest import FIXTURES

from dm_engine import prospects, suppression
from dm_engine.prospects import (
    ImportResult,
    ProspectFileError,
    import_prospects,
    messenger_handle,
    normalize_company_name,
    normalize_domain,
    normalize_email,
    normalize_whatsapp,
    parse_hook_fact,
    prospect_key,
    us_state_tz,
)

ALL_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()


# --------------------------------------------------------------------------- #
# normalize_domain — the ugly inputs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("acmehardscapeaz.com", "acmehardscapeaz.com"),
    ("https://www.acmehardscapeaz.com/", "acmehardscapeaz.com"),
    ("HTTP://WWW.Acmehardscapeaz.COM", "acmehardscapeaz.com"),
    ("https://www.acmehardscapeaz.com/services", "acmehardscapeaz.com"),
    ("https://acmehardscapeaz.com/?utm_source=gmaps&utm_medium=listing",
     "acmehardscapeaz.com"),
    ("  https://acmehardscapeaz.com  ", "acmehardscapeaz.com"),
    ("acmehardscapeaz.com/services?utm=1", "acmehardscapeaz.com"),
    ("acmehardscapeaz.com.", "acmehardscapeaz.com"),
    ("acmehardscapeaz.com:8080", "acmehardscapeaz.com"),
    ("//acmehardscapeaz.com", "acmehardscapeaz.com"),
    ("<https://acmehardscapeaz.com>", "acmehardscapeaz.com"),
    ("alex@acmehardscapeaz.com", "acmehardscapeaz.com"),
    ("mailto:alex@acmehardscapeaz.com", "acmehardscapeaz.com"),
    ("https://shop.acmehardscapeaz.co.uk/x", "shop.acmehardscapeaz.co.uk"),
])
def test_normalize_domain_collapses_every_shape(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "Pinecrest Patio Co", "localhost", "not a domain at all",
    "https://", "192.168.0.1", "..", "x.1",
])
def test_normalize_domain_rejects_non_domains(raw):
    assert normalize_domain(raw) is None


def test_normalize_domain_is_idempotent():
    once = normalize_domain("https://WWW.Acmehardscapeaz.com/services/?utm_source=x")
    assert normalize_domain(once) == once


def test_normalize_email_lowercases_without_provider_guessing():
    assert normalize_email("  Alex@Acmehardscapeaz.com ") == "alex@acmehardscapeaz.com"
    # dots and plus tags are provider-specific; folding them would suppress the
    # wrong mailbox, so they survive verbatim.
    assert normalize_email("a.d+tag@acmehardscapeaz.com") == "a.d+tag@acmehardscapeaz.com"
    assert normalize_email("not-an-email") is None
    assert normalize_email("two@@at.com") is None


# --------------------------------------------------------------------------- #
# prospect_key
# --------------------------------------------------------------------------- #

def test_prospect_key_prefers_the_normalized_domain():
    assert prospect_key("Acme Hardscape", "https://www.acmehardscapeaz.com/",
                        "Glendale AZ") == "acmehardscapeaz.com"


def test_prospect_key_is_stable_across_url_forms_and_markets():
    forms = ["https://www.acmehardscapeaz.com/", "acmehardscapeaz.com",
             "http://acmehardscapeaz.com/services?utm_source=x"]
    keys = {prospect_key("Acme Hardscape", form, market)
            for form in forms for market in ("Glendale AZ", "Scottsdale AZ")}
    assert keys == {"acmehardscapeaz.com"}


def test_prospect_key_falls_back_to_name_slug_when_there_is_no_website():
    assert prospect_key("Pinecrest Patio Co", None, "Glendale AZ") == "name:pinecrest-patio:glendale-az"


def test_name_slug_key_ignores_legal_suffix_noise():
    assert (prospect_key("Pinecrest Patio Co LLC", None, "Glendale AZ")
            == prospect_key("Pinecrest Patio, Co.", None, "Glendale AZ"))


def test_name_slug_key_separates_markets():
    assert (prospect_key("Pinecrest Patio", None, "Glendale AZ")
            != prospect_key("Pinecrest Patio", None, "Boise ID"))


def test_prospect_key_refuses_an_unusable_company_name():
    with pytest.raises(ValueError):
        prospect_key("   ", None, "Glendale AZ")


def test_normalize_company_name_survives_being_only_a_suffix():
    assert normalize_company_name("LLC") == "llc"


# --------------------------------------------------------------------------- #
# timezone map
# --------------------------------------------------------------------------- #

def test_us_state_tz_covers_all_fifty_states_and_dc():
    assert len(prospects.US_STATE_TZ) == 51
    missing = [code for code in [*ALL_STATES, "DC"] if code not in prospects.US_STATE_TZ]
    assert missing == []


def test_us_state_tz_known_values():
    assert us_state_tz("AZ") == "America/Phoenix"
    assert us_state_tz("id") == "America/Boise"
    assert us_state_tz(" tx ") == "America/Chicago"
    assert us_state_tz("HI") == "Pacific/Honolulu"


@pytest.mark.parametrize("bad", ["AB", "ZZ", "", None, "Arizona", "USA"])
def test_us_state_tz_fails_loudly(bad):
    # A silent default here queues a message for 4am prospect-local (§9 edge 31).
    with pytest.raises(KeyError):
        us_state_tz(bad)


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #

def test_import_one_clean_record(engine_db):
    result = import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    assert (result.added, result.dupes, result.skips, result.errors) == (1, 0, [], [])

    row = engine_db.execute("SELECT * FROM prospects").fetchone()
    assert row["prospect_key"] == "bluevalleyhardscapes.com"
    assert row["timezone"] == "America/Boise"
    assert row["market"] == "Boise ID"
    assert row["state"] == "NEW"
    assert row["company_casual"] == "blue valley hardscapes"
    assert parse_hook_fact(row["icp_fit_note"]) == "148 five-star reviews on Google"

    contacts = engine_db.execute(
        "SELECT channel, address, person_key, address_source FROM contacts ORDER BY channel"
    ).fetchall()
    # The fixture reaches this prospect on all four channels the engine stages to.
    assert [c["channel"] for c in contacts] == ["email", "linkedin", "messenger",
                                                "whatsapp"]
    by_channel = {c["channel"]: c for c in contacts}
    assert by_channel["email"]["address"] == "morgan@bluevalleyhardscapes.com"
    assert by_channel["email"]["person_key"] == "morgan@bluevalleyhardscapes.com"
    assert by_channel["linkedin"]["person_key"] == "morgan-bluevalley"
    # A phone number is STORED canonicalized — unlike an email, it has one written
    # form, and storing it is what makes UNIQUE(channel, address) dedup two spellings.
    assert by_channel["whatsapp"]["address"] == "+12085550142"
    assert by_channel["whatsapp"]["person_key"] == "2085550142"
    # A page URL is stored as found (the card is a link a human clicks); the handle
    # is the identity.
    assert by_channel["messenger"]["address"] == "https://m.me/bluevalleyhardscapes"
    assert by_channel["messenger"]["person_key"] == "bluevalleyhardscapes"


def test_import_mixed_accounts_for_every_row(engine_db):
    path = FIXTURES / "prospects" / "mixed.json"
    records = json.loads(path.read_text())
    result = import_prospects(engine_db, path, "Glendale AZ")

    assert result.total == len(records)            # §10 (i): nothing silently dropped
    assert result.added == 4
    assert result.dupes == 1
    assert result.errors == []


def test_import_mixed_identity_details(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "mixed.json", "Glendale AZ")
    keys = {r["prospect_key"] for r in engine_db.execute("SELECT prospect_key FROM prospects")}
    assert "acmehardscapeaz.com" in keys                  # both URL forms collapsed
    assert "name:pinecrest-patio:glendale-az" in keys          # no website -> name slug

    # timezone follows us_state, not the import market (national territory)
    texan = engine_db.execute(
        "SELECT timezone, us_state FROM prospects WHERE prospect_key = ?",
        ("hillcountryoutdoorliving.com",),
    ).fetchone()
    assert (texan["us_state"], texan["timezone"]) == ("TX", "America/Chicago")

    # the record with no owner name still imports; compose handles the name-free open
    nameless = engine_db.execute(
        "SELECT owner_first_name FROM prospects WHERE prospect_key = ?",
        ("granitestoneworks.com",),
    ).fetchone()
    assert nameless["owner_first_name"] is None


# --------------------------------------------------------------------------- #
# the DM channels in the import contract (SPEC §6)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("+1 (602) 555-0134", "+16025550134"),
    ("602-555-0134", "+16025550134"),
    ("602.555.0134", "+16025550134"),
    ("(602) 555 0134", "+16025550134"),
    ("16025550134", "+16025550134"),
    ("+16025550134", "+16025550134"),
    ("+44 20 7946 0958", "+442079460958"),      # already carries a country code
    ("  +1 602 555 0134  ", "+16025550134"),
])
def test_normalize_whatsapp_stores_one_written_form(raw, expected):
    assert normalize_whatsapp(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "call us!", "n/a", "555", "ext 12"])
def test_normalize_whatsapp_rejects_what_is_not_a_number(raw):
    assert normalize_whatsapp(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("https://m.me/example-hardscape", "example-hardscape"),
    ("https://www.facebook.com/example-hardscape/", "example-hardscape"),
    ("http://facebook.com/Example-Hardscape", "example-hardscape"),
    ("https://fb.me/example-hardscape", "example-hardscape"),
    ("https://www.facebook.com/messages/t/12345/", "12345"),
    ("example-hardscape", "example-hardscape"),
])
def test_messenger_handle_collapses_every_front_door_to_one_page(raw, expected):
    assert messenger_handle(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "/"])
def test_messenger_handle_rejects_an_empty_url(raw):
    assert messenger_handle(raw) is None


def test_import_records_dm_contacts_and_accounts_for_the_unusable_ones(engine_db):
    """§10 row (i): a number the import cannot use is NOTED, never silently dropped."""
    result = import_prospects(engine_db, FIXTURES / "prospects" / "mixed.json",
                              "Glendale AZ")
    assert result.errors == []

    rows = engine_db.execute(
        "SELECT p.prospect_key, c.channel, c.address, c.person_key, c.address_source "
        "FROM contacts c JOIN prospects p ON p.id = c.prospect_id "
        "WHERE c.channel IN ('whatsapp','messenger') ORDER BY p.prospect_key, c.channel"
    ).fetchall()
    got = {(r["prospect_key"], r["channel"]): (r["address"], r["person_key"]) for r in rows}
    assert got[("acmehardscapeaz.com", "whatsapp")] == ("+16025550134", "6025550134")
    assert got[("acmehardscapeaz.com", "messenger")] == (
        "https://www.facebook.com/acmehardscapeaz", "acmehardscapeaz")
    assert got[("granitestoneworks.com", "messenger")] == (
        "https://m.me/granitestoneworks", "granitestoneworks")
    # the website-less company: the number is the only reliable identity it has
    assert got[("name:pinecrest-patio:glendale-az", "whatsapp")] == (
        "+16025550177", "6025550177")

    assert any("unusable whatsapp number" in note for _, note in result.notes), result.notes


def test_an_absent_dm_source_falls_back_to_the_most_distrusted_class(engine_db, tmp_path):
    """Never default a DM address to a verified source: `source` is what decides
    whether a RISK verdict may draft, so the safe direction is `format_guess`."""
    path = tmp_path / "dm.json"
    path.write_text(json.dumps([{
        "company_name": "Example Hardscape",
        "website_url": "https://example-hardscape.example",
        "us_state": "AZ",
        "whatsapp": [{"address": "+1 602 555 0134"}],
        "messenger": ["https://m.me/example-hardscape"],
        "source": "manual",
    }]))
    result = import_prospects(engine_db, path, "Glendale AZ")
    assert result.added == 1
    sources = {r["channel"]: r["address_source"] for r in engine_db.execute(
        "SELECT channel, address_source FROM contacts")}
    assert sources == {"whatsapp": "format_guess", "messenger": "format_guess"}


def test_a_suppressed_whatsapp_number_blocks_the_whole_company_at_import(engine_db,
                                                                        tmp_path):
    """Fail closed: ONE suppressed address blocks the record, on any channel, and
    punctuation is not a way around it."""
    suppression.add(engine_db, "whatsapp", "602-555-0134", "address", "hard_no", "dmctl")
    path = tmp_path / "dm.json"
    path.write_text(json.dumps([{
        "company_name": "Example Hardscape",
        "website_url": "https://example-hardscape.example",
        "us_state": "AZ",
        "emails": [{"address": "owner@example-hardscape.example", "source": "website"}],
        "whatsapp": [{"address": "+1 (602) 555-0134", "source": "website"}],
        "source": "manual",
    }]))
    result = import_prospects(engine_db, path, "Glendale AZ")
    assert result.added == 0 and len(result.skips) == 1
    assert "suppressed" in result.skips[0][1]


def test_reimport_is_update_not_insert(engine_db):
    path = FIXTURES / "prospects" / "mixed.json"
    import_prospects(engine_db, path, "Glendale AZ")
    second = import_prospects(engine_db, path, "Scottsdale AZ")

    # Every domain-keyed company re-imports as an update, whatever the market.
    assert second.dupes == 4

    row = engine_db.execute(
        "SELECT market, source_detail FROM prospects WHERE prospect_key = ?",
        ("acmehardscapeaz.com",),
    ).fetchone()
    assert row["market"] == "Glendale AZ"                    # original market kept
    assert "also seen in Scottsdale AZ" in row["source_detail"]


def test_website_less_company_reimported_under_a_new_market_is_flagged(engine_db):
    """The name-slug key is market-scoped by design (§3.2) — say so, don't guess."""
    path = FIXTURES / "prospects" / "mixed.json"
    import_prospects(engine_db, path, "Glendale AZ")
    second = import_prospects(engine_db, path, "Scottsdale AZ")

    assert second.added == 1                                 # the website-less record
    keys = {r["prospect_key"] for r in engine_db.execute("SELECT prospect_key FROM prospects")}
    assert {"name:pinecrest-patio:glendale-az", "name:pinecrest-patio:scottsdale-az"} <= keys
    assert any("shares the company slug" in note for _, note in second.notes)


def test_reimport_enriches_only_empty_fields(engine_db, tmp_path):
    thin = tmp_path / "thin.json"
    thin.write_text(json.dumps([{
        "company_name": "Acme Hardscape", "website_url": "acmehardscapeaz.com",
        "us_state": "AZ", "source": "gmaps-leads",
    }]))
    rich = tmp_path / "rich.json"
    rich.write_text(json.dumps([{
        "company_name": "Acme Hardscape", "website_url": "https://www.acmehardscapeaz.com",
        "us_state": "AZ", "owner_first_name": "Alex", "company_casual": "acme hardscape",
        "hook_fact": "212 five-star reviews", "source": "lead-scout",
    }]))
    import_prospects(engine_db, thin, "Glendale AZ")
    import_prospects(engine_db, rich, "Glendale AZ")

    row = engine_db.execute("SELECT * FROM prospects").fetchone()
    assert row["owner_first_name"] == "Alex"
    assert row["company_casual"] == "acme hardscape"
    assert parse_hook_fact(row["icp_fit_note"]) == "212 five-star reviews"
    assert row["source"] == "gmaps-leads"                    # first sighting wins


def test_malformed_records_all_land_in_errors(engine_db):
    path = FIXTURES / "prospects" / "malformed.json"
    result = import_prospects(engine_db, path, "Glendale AZ")

    assert result.added == 0
    assert len(result.errors) == 4
    assert result.total == len(json.loads(path.read_text()))
    reasons = " ".join(reason for _, reason in result.errors)
    assert "company_name" in reasons and "us_state" in reasons
    assert "not a US state" in reasons                       # §9 edge 7


def test_unparseable_file_raises_rather_than_reporting_zero(engine_db):
    with pytest.raises(ProspectFileError):
        import_prospects(engine_db, FIXTURES / "prospects" / "broken_not_json.json", "Glendale AZ")


def test_file_that_is_not_a_list_raises(engine_db, tmp_path):
    path = tmp_path / "obj.json"
    path.write_text('{"company_name": "Solo"}')
    with pytest.raises(ProspectFileError):
        import_prospects(engine_db, path, "Glendale AZ")


def test_suppressed_address_skips_the_whole_company(engine_db):
    suppression.add(engine_db, "email", "morgan@bluevalleyhardscapes.com",
                    "address", "hard_no", "test")
    result = import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")

    assert result.added == 0
    assert len(result.skips) == 1
    assert "hard_no" in result.skips[0][1]
    assert engine_db.execute("SELECT count(*) FROM prospects").fetchone()[0] == 0


def test_two_companies_on_one_domain_is_an_error_not_a_merge(engine_db, tmp_path):
    """§9 edge 4 — a parent brand collision must never silently overwrite."""
    first = tmp_path / "a.json"
    first.write_text(json.dumps([{"company_name": "Acme Hardscape",
                                  "website_url": "acmehardscapeaz.com", "us_state": "AZ"}]))
    second = tmp_path / "b.json"
    second.write_text(json.dumps([{"company_name": "Meadow Paver Group",
                                   "website_url": "acmehardscapeaz.com", "us_state": "AZ"}]))
    import_prospects(engine_db, first, "Glendale AZ")
    result = import_prospects(engine_db, second, "Glendale AZ")

    assert result.added == 0 and len(result.errors) == 1
    detail = result.errors[0][1]
    assert "Acme Hardscape" in detail and "Meadow Paver Group" in detail
    kept = engine_db.execute("SELECT company_name FROM prospects").fetchone()
    assert kept["company_name"] == "Acme Hardscape"

    forced = import_prospects(engine_db, second, "Glendale AZ",
                              force_keys={"acmehardscapeaz.com"})
    assert forced.dupes == 1


def test_unknown_address_source_degrades_to_the_distrusted_class(engine_db, tmp_path):
    path = tmp_path / "src.json"
    path.write_text(json.dumps([{
        "company_name": "Acme Hardscape", "website_url": "acmehardscapeaz.com",
        "us_state": "AZ",
        "emails": [{"address": "alex@acmehardscapeaz.com", "source": "some-scraper"}],
    }]))
    result = import_prospects(engine_db, path, "Glendale AZ")

    verdict = engine_db.execute("SELECT address_source FROM contacts").fetchone()
    # format_guess is the class G9 refuses to stage on a RISK verdict — fail closed.
    assert verdict["address_source"] == "format_guess"
    assert any("format_guess" in note for _, note in result.notes)


def test_prospect_without_a_usable_address_still_imports_with_a_note(engine_db, tmp_path):
    path = tmp_path / "noaddr.json"
    path.write_text(json.dumps([{
        "company_name": "Pinecrest Patio Co", "us_state": "AZ",
        "emails": [{"address": "not-an-email", "source": "directory"}],
    }]))
    result = import_prospects(engine_db, path, "Glendale AZ")

    assert result.added == 1
    assert engine_db.execute("SELECT count(*) FROM contacts").fetchone()[0] == 0
    assert any("unusable email" in note for _, note in result.notes)


def test_import_result_accounting_identity_is_a_property():
    result = ImportResult(added=2, dupes=1, skips=[("a", "x")], errors=[("b", "y")])
    assert result.total == 5                                 # notes are not an outcome


# --------------------------------------------------------------------------- #
# verification verdicts (the DB side of `dmctl verify`)
# --------------------------------------------------------------------------- #

def test_record_verify_verdicts_writes_onto_the_contact_row(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    result = prospects.record_verify_verdicts(engine_db, [
        {"email": "morgan@bluevalleyhardscapes.com", "verdict": "PASS",
         "reason": "mailbox accepted"}])

    assert (result.recorded, result.unknown, result.blocklisted) == (1, [], 0)
    row = engine_db.execute(
        "SELECT * FROM contacts WHERE channel = 'email'").fetchone()
    assert (row["verify_verdict"], row["verify_reason"]) == ("PASS", "mailbox accepted")
    assert row["verified_at"] is not None


def test_record_verify_verdicts_matches_address_case_insensitively(engine_db):
    """A verifier may echo back a different case than the one we stored."""
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    result = prospects.record_verify_verdicts(engine_db, [
        {"address": "MORGAN@Bluevalleyhardscapes.COM", "verdict": "FAIL",
         "reason": "no such mailbox"}])

    assert result.recorded == 1 and result.unknown == []
    assert engine_db.execute(
        "SELECT verify_verdict FROM contacts").fetchone()["verify_verdict"] == "FAIL"


def test_record_verify_verdicts_reports_addresses_it_does_not_know(engine_db):
    """§10 row (i): a stale verification file must not look like a clean run."""
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    result = prospects.record_verify_verdicts(engine_db, [
        {"email": "morgan@bluevalleyhardscapes.com", "verdict": "PASS"},
        {"email": "ghost@nowhere.example", "verdict": "PASS"}])

    assert result.recorded == 1
    assert result.unknown == ["ghost@nowhere.example"]


def test_record_verify_verdicts_counts_blocklist_mentions(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    result = prospects.record_verify_verdicts(engine_db, [
        {"email": "morgan@bluevalleyhardscapes.com", "verdict": "RISK",
         "reason": "rejected: Spamhaus BLOCKLIST (PBL)"}])
    assert result.blocklisted == 1


def test_record_verify_verdicts_refuses_a_verdict_the_schema_forbids(engine_db):
    """CHECK-constrained column: catch it here with a name, not as an opaque IntegrityError."""
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    with pytest.raises(ValueError, match="unknown verdict"):
        prospects.record_verify_verdicts(engine_db, [
            {"email": "morgan@bluevalleyhardscapes.com", "verdict": "GOOD"}])
    assert engine_db.execute(
        "SELECT verify_verdict FROM contacts").fetchone()["verify_verdict"] is None


def test_record_verify_verdicts_skips_rows_missing_an_address_or_verdict(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    result = prospects.record_verify_verdicts(engine_db, [
        {"verdict": "PASS"}, {"email": "morgan@bluevalleyhardscapes.com"}, {}])
    assert (result.recorded, result.unknown) == (0, [])


def test_record_verify_verdicts_tolerates_an_empty_batch(engine_db):
    assert prospects.record_verify_verdicts(engine_db, []).recorded == 0
    assert prospects.record_verify_verdicts(engine_db, None).recorded == 0


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

def test_qualify_moves_new_to_qualified_and_logs(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    prospect_id = engine_db.execute("SELECT id FROM prospects").fetchone()["id"]

    prospects.qualify(engine_db, prospect_id, actor="operator")
    assert engine_db.execute("SELECT state FROM prospects").fetchone()["state"] == "QUALIFIED"

    log = engine_db.execute(
        "SELECT * FROM state_log WHERE entity = 'prospect' AND entity_id = ?",
        (str(prospect_id),),
    ).fetchone()
    assert (log["from_state"], log["to_state"], log["actor"]) == ("NEW", "QUALIFIED", "operator")


def test_qualify_is_idempotent_and_refuses_terminal_states(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    prospect_id = engine_db.execute("SELECT id FROM prospects").fetchone()["id"]

    prospects.qualify(engine_db, prospect_id, actor="operator")
    prospects.qualify(engine_db, prospect_id, actor="operator")        # no-op, no raise
    logs = engine_db.execute("SELECT count(*) FROM state_log").fetchone()[0]
    assert logs == 1

    prospects.exclude(engine_db, prospect_id, "franchise chain", actor="operator")
    with pytest.raises(ValueError):
        prospects.qualify(engine_db, prospect_id, actor="operator")


def test_exclude_is_terminal_and_demands_a_reason(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    prospect_id = engine_db.execute("SELECT id FROM prospects").fetchone()["id"]

    with pytest.raises(ValueError):
        prospects.exclude(engine_db, prospect_id, "  ", actor="operator")

    prospects.exclude(engine_db, prospect_id, "dominant regional player", actor="operator")
    assert engine_db.execute("SELECT state FROM prospects").fetchone()["state"] == "EXCLUDED"

    log = engine_db.execute("SELECT reason FROM state_log").fetchone()
    assert log["reason"] == "dominant regional player"


def test_set_casual_fixes_a_bad_casualization(engine_db):
    """§9 edge 6 — 'phx pvrs' is caught by a human, fixed here, then re-composed."""
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    prospect_id = engine_db.execute("SELECT id FROM prospects").fetchone()["id"]

    prospects.set_casual(engine_db, prospect_id, "  blue valley  ")
    assert engine_db.execute(
        "SELECT company_casual FROM prospects").fetchone()["company_casual"] == "blue valley"

    with pytest.raises(ValueError):
        prospects.set_casual(engine_db, prospect_id, "   ")


def test_lifecycle_helpers_reject_an_unknown_prospect(engine_db):
    with pytest.raises(KeyError):
        prospects.qualify(engine_db, 999, actor="operator")
    with pytest.raises(KeyError):
        prospects.exclude(engine_db, 999, "gone", actor="operator")
    with pytest.raises(KeyError):
        prospects.set_casual(engine_db, 999, "nope")


def test_state_log_rows_survive_the_append_only_trigger(engine_db):
    import_prospects(engine_db, FIXTURES / "prospects" / "one.json", "Boise ID")
    prospect_id = engine_db.execute("SELECT id FROM prospects").fetchone()["id"]
    prospects.qualify(engine_db, prospect_id, actor="operator")

    with pytest.raises(sqlite3.DatabaseError):
        engine_db.execute("DELETE FROM state_log")
