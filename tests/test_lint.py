"""Lint rules L1-L12 (PLAN.md §5.4) and the normalization that makes them unbypassable.

Two obligations shape this file:

1. **Every rule has a test proving it fires.** A rule that silently never fires is
   worse than no rule, because it looks like coverage.
2. **Every rule has a test proving it does NOT fire on clean copy.** A linter that
   rejects good copy trains its owner to bypass it, which costs more than the miss.

The fixtures in `tests/fixtures/messages/` carry their own `_expect_rules`, so a
future reviewer can add a message file and see exactly which rules it trips.
"""

import json

import pytest

from conftest import CLEAN_BODY, FIXTURES, PASSING_LINT
from dm_engine import lint
from dm_engine.lint import (DEMO_LINK, Constraints, LintError, LintReport,
                            MessageDraft, RULES, lint_message, normalize,
                            resolve_constraints, split_sentences, subject_key)

MESSAGES = FIXTURES / "messages"
ALL_RULES = list(RULES)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def load(name: str) -> dict:
    return json.loads((MESSAGES / name).read_text(encoding="utf-8"))


def fixture_drafts(name: str) -> list[dict]:
    """Draft rows that declare an expectation (skips ingest-robustness rows)."""
    return [row for row in load(name)["drafts"]
            if isinstance(row, dict) and "_expect_rules" in row]


def every_expecting_draft() -> list[tuple[str, int, dict]]:
    out = []
    for path in sorted(MESSAGES.glob("*.json")):
        for index, row in enumerate(fixture_drafts(path.name)):
            out.append((path.name, index, row))
    return out


CLEAN = fixture_drafts("one_ok.json")[0]
CLEAN_DM = fixture_drafts("clean_linkedin_dm.json")[0]
CLEAN_STEP2 = fixture_drafts("clean_email_step2.json")[0]


def make_draft(**overrides) -> MessageDraft:
    base = {"prospect_key": "acmehardscapeaz.com", "channel": "email",
            "sequence_step": 1, "subject": CLEAN["subject"], "body": CLEAN["body"]}
    base.update(overrides)
    return MessageDraft(**base)


def body_with(middle: str) -> str:
    """The clean email with its pain paragraph swapped out."""
    return ("Hey Alex,\n\n"
            f"{middle}\n\n"
            "how many of those do you figure slip by in a week?\n\n"
            "Sam")


def run(draft, *, batch_subjects=(), recent_subjects=(), company_casual="acme hardscape",
        constraints=None, first_name="Alex") -> LintReport:
    return lint_message(draft, list(batch_subjects), list(recent_subjects), company_casual,
                        constraints=constraints, first_name=first_name)


def run_row(row: dict, **kwargs) -> LintReport:
    draft = MessageDraft(prospect_key=row["prospect_key"], channel=row["channel"],
                         sequence_step=row.get("sequence_step", 1),
                         subject=row.get("subject"), body=row["body"])
    kwargs.setdefault("company_casual", row.get("_company_casual"))
    kwargs.setdefault("first_name", row.get("_first_name"))
    return run(draft, **kwargs)


# --------------------------------------------------------------------------- #
# normalization — the single most bypassable part of the module
# --------------------------------------------------------------------------- #


def test_normalize_strips_zero_width_characters():
    assert normalize("bright​flow‍cure") == "brightflowcure"
    assert normalize("b﻿d⁠a­i") == "bdai"


def test_normalize_strips_invisible_tag_characters():
    smuggled = "bdai" + "".join(chr(cp) for cp in range(0xE0041, 0xE0045))
    assert normalize(smuggled) == "bdai"


def test_normalize_folds_fullwidth_forms():
    assert normalize("ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ") == "www.example.com"
    assert normalize("ＢＤＡＩ") == "BDAI"


def test_normalize_folds_mathematical_alphanumerics():
    assert normalize("\U0001d41b\U0001d41d\U0001d41a\U0001d422") == "bdai"


def test_normalize_folds_cyrillic_homoglyphs():
    # 'а' U+0430, 'е' U+0435, 'о' U+043E, 'с' U+0441, 'р' U+0440
    assert normalize("bdаi") == "bdai"
    assert normalize("brіghtflоwсurе") == "brightflowcure"


def test_normalize_folds_greek_homoglyphs():
    assert normalize("ΟΚ") == "OK"          # Greek Omicron + Kappa
    assert normalize("brightflοw") == "brightflow"


def test_normalize_folds_curly_quotes_and_dashes():
    assert normalize("don’t “ring” — twice") == 'don\'t "ring" - twice'


def test_normalize_folds_nbsp_and_normalizes_newlines():
    assert normalize("a b") == "a b"
    assert normalize("a\r\nb\rc") == "a\nb\nc"


def test_normalize_handles_none_and_rejects_non_strings():
    assert normalize(None) == ""
    with pytest.raises(LintError):
        normalize(17)


@pytest.mark.parametrize("name,index,row", every_expecting_draft(),
                         ids=lambda value: value if isinstance(value, str) else None)
def test_normalize_is_idempotent(name, index, row):
    once = normalize(row["body"])
    assert normalize(once) == once


def test_normalize_leaves_legitimate_accents_alone():
    assert normalize("José's patio") == "José's patio"


# --------------------------------------------------------------------------- #
# the unicode bypass is closed: rules still fire after folding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("smuggled,rule", [
    ("bdаi answers those calls in about two rings.", "L2"),      # Cyrillic а
    ("bright​flow‍cure was built for this exact thing.", "L2"),  # zero-width
    ("\U0001d41b\U0001d41d\U0001d41a\U0001d422 picks up the phone every time.", "L2"),
    ("the write-up lives at ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ for later.", "L1"),
    ("I was looking at oakhollowpatios．com and the work is sharp.", "L1"),
    ("brightdеsk answers the calls you never hear ring.", "L2"),   # Cyrillic о
])
def test_unicode_smuggling_is_caught_after_normalization(smuggled, rule):
    report = run(make_draft(body=body_with(smuggled)))
    assert rule in report.rules(), report.summary()


def test_normalized_body_is_ascii_so_the_sql_belt_also_sees_it():
    """Ingest stores normalized text, so trigger G6's ASCII LIKE patterns match too."""
    folded = normalize("bdаi")
    assert folded == "bdai"
    assert folded.upper() == "BDAI"          # what G6 matches with LIKE '%BDAI%'
    assert normalize("ｗｗｗ．x．ｃｏｍ").startswith("www.")   # G6 LIKE '%www.%'


# --------------------------------------------------------------------------- #
# clean copy passes — the false-positive half of the contract
# --------------------------------------------------------------------------- #


def test_clean_email_passes_every_rule():
    report = run_row(CLEAN)
    assert report.ok, report.summary()
    assert report.violations == []


def test_clean_linkedin_dm_passes_every_rule():
    report = run_row(CLEAN_DM, company_casual="granite ridge stoneworks")
    assert report.ok, report.summary()


def test_clean_step2_passes_every_rule():
    report = run_row(CLEAN_STEP2)
    assert report.ok, report.summary()


@pytest.mark.parametrize("rule", ALL_RULES)
def test_no_rule_false_positives_on_clean_email(rule):
    assert rule not in run_row(CLEAN).rules()


@pytest.mark.parametrize("rule", ALL_RULES)
def test_no_rule_false_positives_on_clean_dm(rule):
    assert rule not in run_row(CLEAN_DM, company_casual="granite ridge stoneworks").rules()


def test_owner_a_clean_body_passes_lint():
    """conftest.CLEAN_BODY is seeded with PASSING_LINT — the two must agree."""
    report = run(make_draft(body=CLEAN_BODY))
    assert report.ok, report.summary()
    assert json.loads(PASSING_LINT)["ok"] is True


@pytest.mark.parametrize("middle", [
    "your bright flow is the first thing that suffers when nobody picks up the phone.",
    "the front desk is you, which is the whole problem in one sentence.",
    "they tried a chatbot and gave up. ai was never the issue, the handoff was.",
    "the crew rolls at 6 a.m. and the calls start at 7 a.m. like clockwork.",
    "a 3.5 hour install turns into a 5.5 hour day the second the phone wins.",
    "you know the difference between e.g. a paver base and a slab. mr. reyes does not.",
    "the compactor is loud. the voicemail is quiet. neither one closes the job.",
    "one crew, twelve bids, and a phone that rings while your hands are full.",
    # sentence boundaries followed by a word that starts with a TLD — the exact
    # regression risk from matching attached domains case-insensitively.
    "the crew is out. Company policy is voicemail, which nobody loves.",
    "eleven years in. Construction is not the hard part, the phone is.",
    "call it what you want. Info like that never reaches you at all.",
    "one crew, twelve bids. Co-owners end up splitting the phone duty.",
    "they tried a chatbot and quit. AI was never really the issue here.",
])
def test_legitimate_copy_is_not_flagged(middle):
    report = run(make_draft(body=body_with(middle)))
    assert report.ok, f"false positive on legitimate copy: {report.summary()}"


def test_company_name_containing_a_banned_word_is_not_a_violation():
    body = body_with("the sunset solutions crew has been at this for eleven years.")
    assert run(make_draft(body=body), company_casual="sunset solutions").ok


def test_the_same_banned_word_outside_the_company_name_still_fires():
    body = body_with("there is a simple solution to the whole missed-call thing.")
    assert "L3" in run(make_draft(body=body), company_casual="sunset stoneworks").rules()


# --------------------------------------------------------------------------- #
# every rule fires — targeted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("snippet", [
    "have a look at https://example.com/post when you get a minute.",
    "the gallery at www.acmehardscape.com does the selling for you.",
    "I was reading through acmehardscapeaz.com last night and the work is sharp.",
    "the write-up is [right here](https://example.com/post) if you want it.",
    "the link is <https://example.com/post> for later.",
    "look at alderfieldhardscape [dot] com and you can see the backlog.",
    "someone sent me mesagrandepavers dot com last week.",
    "someone sent me mesagrandepavers . com last week.",
    "your site sunvalleystoneaz.com. the work sells itself.",
    "mail me at alex@acmehardscapeaz.com if that is easier.",
    "go to AZPavers.COM/gallery when you get a minute.",
    "read https://bit.ly/2xKq9 for the whole write-up.",
    "our own site BrightDeskPros.Net has the same problem.",
])
def test_L1_fires_on_every_url_shape(snippet):
    assert "L1" in run(make_draft(body=body_with(snippet))).rules()


@pytest.mark.parametrize("snippet", [
    "I built BrightFlow Cure for exactly this.",
    "brightflowcure was built for exactly this.",
    "BDAI picks up every one of those.",
    "b.d.a.i picks up every one of those.",
    "a bright desk ai never takes a lunch break.",
    "brightdesk answers the ones you never hear.",
])
def test_L2_fires_on_product_identity(snippet):
    assert "L2" in run(make_draft(body=body_with(snippet))).rules()


def test_L2_does_not_apply_to_step_2():
    draft = make_draft(sequence_step=2, subject="the demo from earlier",
                       body=CLEAN_STEP2["body"])
    assert "L2" not in run(draft).rules()


@pytest.mark.parametrize("snippet", [
    "I'd love to connect and hear how you handle it.",
    "there is real synergy between what you do and what I do.",
    "wanted to circle back on this one.",
    "figured I would touch base before the season turns.",
    "just checking in on the last note.",
    "hope this finds you well.",
    "a cutting-edge way to handle the phones.",
    "we can streamline the whole intake.",
    "this would revolutionize your intake.",
    "a real game-changer for a crew your size.",
    "there is a simple solution to all of it.",
])
def test_L3_fires_on_banned_phrases(snippet):
    assert "L3" in run(make_draft(body=body_with(snippet))).rules()


def test_L3_fires_on_hey_there_greeting():
    body = "Hey there,\n\n" + body_with("the phone still runs you.").split("\n\n", 1)[1]
    assert "L3" in run(make_draft(body=body)).rules()


def test_L3_quick_question_is_a_subject_only_rule():
    assert "L3" in run(make_draft(subject="quick question")).rules()
    body = body_with("that was a quick question about the compactor.")
    assert "L3" not in run(make_draft(body=body)).rules()


@pytest.mark.parametrize("char", ["\U0001f680", "☎", "✅", "⭐", "\U0001f44b"])
def test_L4_fires_on_emoji(char):
    assert "L4" in run(make_draft(body=body_with(f"the phone buzzes twice {char}"))).rules()


def test_L4_fires_on_emoji_in_the_subject():
    assert "L4" in run(make_draft(subject="the compactor \U0001f4de")).rules()


@pytest.mark.parametrize("body,label", [
    ("Hey {first_name},\n\n" + CLEAN["body"].split("\n\n", 1)[1], "curly brace"),
    ("Hey Alex,\n\n" + body_with("most owners at [Company Name] gave up.").split("\n\n", 1)[1],
     "square bracket"),
    (body_with("most owners at <company> already gave up on this."), "angle"),
    (body_with("most owners at %%COMPANY%% already gave up on this."), "percent"),
    (body_with("most owners at $company already gave up on this."), "dollar"),
    (body_with("the hook_fact goes right here in the opener line."), "literal token"),
    ("Hi ,\n\n" + CLEAN["body"].split("\n\n", 1)[1], "greeting with no name"),
    ("\n\nHey Alex,\n\n" + CLEAN["body"].split("\n\n", 1)[1], "empty first line"),
    (body_with("nice work on  last month, that wall is no joke."), "double space"),
    (body_with("saw the work at ."), "space before punctuation"),
    (body_with("and the phone still runs you every single day of the week."),
     "hook starts mid-clause"),
    ("Hey Alex,\n\ngreat work.\n\nhow many of those slip by in a week?\n\nSam",
     "hook too short"),
])
def test_L5_fires_on_unresolved_placeholders_and_holes(body, label):
    assert "L5" in run(make_draft(body=body)).rules(), label


def test_L6_fires_on_a_capitalized_subject():
    assert "L6" in run(make_draft(subject="The Compactor Doesn't Take Messages")).rules()


def test_L6_fires_on_a_missing_subject():
    assert "L6" in run(make_draft(subject=None)).rules()
    assert "L6" in run(make_draft(subject="   ")).rules()


def test_L6_fires_on_an_overlong_subject():
    assert "L6" in run(make_draft(subject="the compactor does not take messages and "
                                          "neither does the voicemail box")).rules()


def test_L6_fires_over_the_word_cap():
    long_body = body_with(" ".join(["the phone rings while your hands are full"] * 20))
    report = run(make_draft(body=long_body))
    assert "L6" in report.rules()
    assert "words" in report.summary()


@pytest.mark.parametrize("signature", ["Sam Taylor, Founder", "Sam\n602-555-0100",
                                       "Thanks,\nJ", ""])
def test_L6_fires_on_a_bad_signature(signature):
    body = ("Hey Alex,\n\nmost hardscape owners I talk to already tried an answering "
            "service and it made things worse.\n\nhow many of those slip by in a week?"
            + (f"\n\n{signature}" if signature else ""))
    assert "L6" in run(make_draft(body=body)).rules()


def test_L7_fires_over_the_dm_sentence_cap():
    body = ("most owners tried an answering service. it made things worse. you end up "
            "back at voicemail. the homeowner calls the next crew. did you land "
            "somewhere better?")
    report = run(make_draft(channel="linkedin", subject=None, body=body))
    assert "L7" in report.rules()
    assert "sentences" in report.summary()


def test_L7_fires_over_the_dm_character_cap():
    body = fixture_drafts("violation_L7_dm_shape.json")[2]["body"]
    report = run(make_draft(channel="linkedin", subject=None, body=body),
                 company_casual="oak hollow")
    assert "L7" in report.rules()
    assert "characters" in report.summary()


def test_L7_fires_when_a_dm_carries_a_subject():
    report = run(make_draft(channel="linkedin", subject="a subject", body=CLEAN_DM["body"]))
    assert "L7" in report.rules()


# --------------------------------------------------------------------------- #
# L7 on the two Meta DM channels (L6 must NOT apply to either)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("channel, sentences", [("whatsapp", 3), ("messenger", 4)])
def test_L7_fires_over_the_dm_sentence_cap_on_the_meta_channels(channel, sentences):
    body = ("most owners tried an answering service. it made things worse. you end up "
            "back at voicemail. the homeowner calls the next crew. did you land "
            "somewhere better?")
    report = run(make_draft(channel=channel, subject=None, body=body))
    assert "L7" in report.rules()
    assert f"limit {sentences}" in report.summary()


@pytest.mark.parametrize("channel, cap", [("whatsapp", 400), ("messenger", 500)])
def test_L7_fires_over_the_dm_character_cap_on_the_meta_channels(channel, cap):
    """One long sentence: under the sentence cap, over the character cap."""
    filler = "the crew is still on the paver base when the phone starts going and "
    body = f"most owners I talk to say {filler * 12}does that land?"
    assert len(body) > cap
    report = run(make_draft(channel=channel, subject=None, body=body))
    assert "L7" in report.rules()
    assert f"characters, limit {cap}" in report.summary()


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_L7_fires_when_a_meta_dm_carries_a_subject(channel):
    report = run(make_draft(channel=channel, subject="a subject", body=CLEAN_DM["body"]))
    assert "L7" in report.rules()


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_L6_never_applies_to_a_meta_dm(channel):
    """L6 is the EMAIL long-form rule: 120-word cap, lowercase subject, a `Sam`
    signature on its own last line. A DM has none of those and must not be judged
    against them — a linter that rejects good copy trains its operator to bypass it."""
    report = run(make_draft(channel=channel, subject=None, body=CLEAN_DM["body"]))
    assert report.ok, report.summary()
    assert "L6" not in report.rules()


@pytest.mark.parametrize("channel", ["whatsapp", "messenger"])
def test_a_clean_dm_body_passes_on_the_meta_channels(channel):
    assert run(make_draft(channel=channel, subject=None, body=CLEAN_DM["body"])).ok


def test_whatsapp_is_the_tightest_channel_the_engine_has():
    whatsapp = resolve_constraints("whatsapp")
    messenger = resolve_constraints("messenger")
    linkedin = resolve_constraints("linkedin")
    assert whatsapp.max_sentences <= linkedin.max_sentences < messenger.max_sentences
    assert whatsapp.max_chars < messenger.max_chars
    for limits in (whatsapp, messenger):
        assert limits.has_subject is False
        assert limits.requires_signature is False
        assert limits.max_words is None


@pytest.mark.parametrize("body,why", [
    (body_with("did you end up back at voicemail too?"), "two question marks"),
    ("Hey Alex,\n\nmost owners already tried an answering service and it made things "
     "worse.\n\nlet me know either way.\n\nSam", "no question at all"),
    ("Hey Alex,\n\nhow many of those do you figure slip by in a week?\n\nanyway, that "
     "is the whole reason I reached out today.\n\nSam", "question is not the close"),
])
def test_L8_fires_when_there_is_not_exactly_one_closing_cta(body, why):
    assert "L8" in run(make_draft(body=body)).rules(), why


def test_L8_does_not_apply_to_step_2():
    draft = make_draft(sequence_step=2, subject="the demo from earlier",
                       body=CLEAN_STEP2["body"])
    assert "L8" not in run(draft).rules()


def test_L9_fires_on_a_subject_reused_inside_the_batch():
    report = run(make_draft(), batch_subjects=["The Skid Steer Doesn't  Take Messages"])
    assert "L9" in report.rules()


def test_L9_fires_on_a_subject_used_in_the_last_30_days():
    report = run(make_draft(), recent_subjects=[CLEAN["subject"]])
    assert "L9" in report.rules()


def test_L9_does_not_fire_on_a_distinct_subject():
    report = run(make_draft(), batch_subjects=["two hours into a paver base"],
                 recent_subjects=["the phone that runs you"])
    assert "L9" not in report.rules()


def test_subject_key_folds_case_whitespace_and_unicode():
    assert subject_key("The  Compactor") == subject_key("the compactor")
    assert subject_key("the compactor") == subject_key("the compactor")


@pytest.mark.parametrize("casual", ["ACME HARDSCAPE LLC", "acme hardscape inc",
                                    "acme hardscape and outdoor living co.",
                                    "acme hardscape.", None, "  "])
def test_L10_fires_on_bad_casualization(casual):
    assert "L10" in run(make_draft(), company_casual=casual).rules()


def test_L10_accepts_a_normal_casual_name():
    assert "L10" not in run(make_draft(), company_casual="acme hardscape").rules()


def test_L11_fires_when_step_2_has_no_demo_link():
    body = ("Hey Alex,\n\nglad it resonated - I built something for exactly this "
            "problem and it answers the calls you can't get to.\n\nSam")
    report = run(make_draft(sequence_step=2, subject="the demo from earlier", body=body))
    assert "L11" in report.rules()


def test_L11_fires_when_step_2_repeats_the_demo_link():
    body = (f"Hey Alex,\n\nglad it resonated. here it is: https://{DEMO_LINK} and "
            f"again https://{DEMO_LINK} in case.\n\nSam")
    report = run(make_draft(sequence_step=2, subject="the demo from earlier", body=body))
    assert "L11" in report.rules()


def test_L11_fires_when_step_2_does_not_use_the_real_first_name():
    body = (f"Hey friend,\n\nglad it resonated. here it is: https://{DEMO_LINK} whenever "
            "you have two minutes.\n\nSam")
    report = run(make_draft(sequence_step=2, subject="the demo from earlier", body=body),
                 first_name="Alex")
    assert "L11" in report.rules()


@pytest.mark.parametrize("snippet", [
    "act now before the season turns.",
    "this is a limited time thing.",
    "last chance before spring books up.",
    "don't miss another one of those.",
])
def test_L12_fires_on_fake_urgency(snippet):
    assert "L12" in run(make_draft(body=body_with(snippet))).rules()


# --------------------------------------------------------------------------- #
# fixtures are the reviewer's ledger
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,index,row", every_expecting_draft(),
                         ids=[f"{name}:{index}" for name, index, _ in every_expecting_draft()])
def test_fixture_expectations_match(name, index, row):
    report = run_row(row, batch_subjects=_peer_subjects(name, index))
    assert set(report.rules()) == set(row["_expect_rules"]), report.summary()


def _peer_subjects(name: str, index: int) -> list[str]:
    """Subjects of earlier drafts in the same fixture — mirrors what ingest feeds L9."""
    return [row.get("subject") for row in fixture_drafts(name)[:index] if row.get("subject")]


def test_every_rule_has_at_least_one_firing_fixture():
    fired: set[str] = set()
    for name, _index, row in every_expecting_draft():
        fired.update(row["_expect_rules"])
    assert set(ALL_RULES) - fired == set(), "rules with no fixture proving they fire"


def test_multi_violation_fixture_reports_all_of_them_not_just_the_first():
    row = fixture_drafts("multi_violation.json")[0]
    report = run_row(row)
    assert len(report.rules()) >= 8
    assert not report.ok


# --------------------------------------------------------------------------- #
# constraints arrive as a parameter, never as an import from dm_engine.channels
# --------------------------------------------------------------------------- #


def test_lint_never_imports_the_channels_package():
    source = (lint.__file__)
    text = open(source, encoding="utf-8").read()
    assert "dm_engine.channels" not in text.replace("`dm_engine.channels`", "")
    assert "from .channels" not in text


def test_constraints_parameter_overrides_the_channel_default():
    class ForeignConstraints:           # duck-typed stand-in for Owner D's dataclass
        max_words = 10
        max_sentences = None
        max_chars = None
        has_subject = True

    report = run(make_draft(), constraints=ForeignConstraints())
    assert "L6" in report.rules()
    assert "limit 10" in report.summary()


def test_constraints_partial_object_falls_back_to_the_channel_default():
    class OnlySentences:
        max_sentences = 1

    limits = resolve_constraints("email", OnlySentences())
    assert limits.max_sentences == 1
    assert limits.max_words == 120          # from DEFAULT_CONSTRAINTS
    assert limits.has_subject is True


def test_resolve_constraints_defaults_per_channel():
    assert resolve_constraints("email").max_words == 120
    assert resolve_constraints("linkedin") == Constraints(
        max_words=None, max_sentences=3, max_chars=300, has_subject=False,
        max_subject_chars=None, requires_signature=False)
    assert resolve_constraints("whatsapp") == Constraints(
        max_words=None, max_sentences=3, max_chars=400, has_subject=False,
        max_subject_chars=None, requires_signature=False)
    assert resolve_constraints("messenger") == Constraints(
        max_words=None, max_sentences=4, max_chars=500, has_subject=False,
        max_subject_chars=None, requires_signature=False)


def test_unknown_channel_without_constraints_is_an_error():
    with pytest.raises(LintError):
        run(make_draft(channel="carrier-pigeon"))


def test_lint_message_rejects_a_non_draft():
    with pytest.raises(LintError):
        lint_message({"body": "hi"}, [], [], "acme hardscape")


# --------------------------------------------------------------------------- #
# sentence splitting
# --------------------------------------------------------------------------- #


def test_split_sentences_guards_abbreviations():
    assert len(split_sentences("we start at 6 a.m. and stop at 4 p.m. every day.")) == 1
    assert len(split_sentences("mr. reyes runs the crew. he answers nothing.")) == 2


def test_split_sentences_guards_decimals():
    assert len(split_sentences("a 3.5 hour install becomes a 5.5 hour day.")) == 1


def test_split_sentences_handles_empty_and_terminatorless_text():
    assert split_sentences("") == []
    assert split_sentences("no terminator here") == ["no terminator here"]


# --------------------------------------------------------------------------- #
# LintReport serialization — the shape trigger G5 reads
# --------------------------------------------------------------------------- #


def test_to_json_emits_a_real_json_boolean_for_ok():
    payload = json.loads(LintReport.build([]).to_json())
    assert payload["ok"] is True
    assert '"ok": true' in LintReport.build([]).to_json()


def test_to_json_round_trips():
    report = LintReport.build([{"rule": "L1", "detail": "url", "where": "body"}])
    again = LintReport.from_json(report.to_json())
    assert again.ok is False and again.violations == report.violations


def test_report_build_derives_ok_from_the_violation_list():
    assert LintReport.build([]).ok is True
    assert LintReport.build([{"rule": "L4", "detail": "emoji"}]).ok is False


def test_violations_carry_rule_and_readable_detail():
    report = run(make_draft(body=body_with("see https://example.com/post for more.")))
    violation = next(v for v in report.violations if v["rule"] == "L1")
    assert violation["where"] in ("body", "subject")
    assert "example.com" in violation["detail"]


# --------------------------------------------------------------------------- #
# the database agrees: G5 accepts a passing report and refuses everything else
# --------------------------------------------------------------------------- #


def _ready(conn, message_id, report_json):
    conn.execute("UPDATE messages SET state='READY', lint_report=? WHERE id=?",
                 (report_json, message_id))
    conn.commit()


def test_g5_accepts_a_passing_lintreport(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    _ready(engine_db, message_id, LintReport.build([]).to_json())
    state = engine_db.execute("SELECT state FROM messages WHERE id=?",
                              (message_id,)).fetchone()["state"]
    assert state == "READY"


def test_g5_refuses_a_failing_lintreport(engine_db, seed_graph, insert_message):
    import sqlite3
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    failing = LintReport.build([{"rule": "L1", "detail": "url"}]).to_json()
    with pytest.raises(sqlite3.IntegrityError, match="G5"):
        _ready(engine_db, message_id, failing)


def test_g5_would_refuse_a_string_ok_which_is_why_to_json_uses_a_boolean(
        engine_db, seed_graph, insert_message):
    import sqlite3
    ids = seed_graph(engine_db)
    message_id = insert_message(engine_db, ids)
    with pytest.raises(sqlite3.IntegrityError, match="G5"):
        _ready(engine_db, message_id, json.dumps({"ok": "true", "violations": []}))


# =========================================================================== #
# RED-TEAM REGRESSIONS
#
# An independent reviewer defeated this linter and rode a live URL, the product
# name and a broken merge token into a would-be Gmail draft with
# `{"ok": true, "violations": []}`. Each case below is verbatim from that report.
# The two structural lessons, worth keeping in mind before relaxing anything here:
#
#   1. L1 and L2 were allowlists pretending to be denylists (27 TLDs, a hand-typed
#      confusables table). Anything that reintroduces an enumerated list of the
#      *safe* spellings will be one entry behind whatever a model emits next.
#   2. The old "legitimate copy" cases were all middle-paragraph substitutions
#      into one fixed hook and one fixed close, so every false positive the
#      reviewer found lived in the hook or the sign-off. The clean cases below
#      vary exactly those two slots.
# =========================================================================== #

REDTEAM_HOOK = ("saw the flagstone patio and firepit you finished off Camelback "
                "last month - the seat wall detail is sharp.")
REDTEAM_PAIN = ("guessing most of your day is on site, not next to a phone. that is "
                "when calls hit voicemail and the homeowner dials the next outfit.")


def redteam_body(hook=REDTEAM_HOOK, pain=REDTEAM_PAIN, greeting="Morgan",
                 close="how many of those do you figure you never hear about?",
                 signoff="Sam"):
    return f"Hey {greeting},\n\n{hook}\n\n{pain}\n\n{close}\n\n{signoff}"


def test_the_full_red_team_payload_is_refused():
    """The exact message that produced `ok: true` — three guarantees at once.

    `bdαi` carries a Greek alpha (U+03B1), which the confusables table missed while
    claiming to fold Greek; `bit.ly` sat outside the TLD allowlist; `None` is a merge
    token that had already rendered.
    """
    body = redteam_body(
        greeting="None",
        pain=(f"{REDTEAM_PAIN} bdαi fixes that - 90 second clip: "
              "bit.ly/3xKq9Za or ring 602-555-0134."))
    report = run(make_draft(body=body))
    assert not report.ok
    assert {"L1", "L2", "L5", "L8"} <= {v["rule"] for v in report.violations}


@pytest.mark.parametrize("link", [
    "bit.ly/3xKq9Za", "goo.gl/aB3xY", "cutt.ly/demo9", "rb.gy/x8k2p", "tiny.cc/x",
    "shorturl.at/kmQZ4", "wa.me/16025550134",          # shorteners
    "voiceclip.contractors", "voiceclip.design", "voiceclip.works",   # new gTLDs
    "voiceclip.me", "voiceclip.tv", "voiceclip.to", "voiceclip.sh",   # ccTLDs
    "143.198.22.7/demo",                                # bare IPv4
    "xn--80ak6aa92e.tv",                                # punycode
])
def test_a_link_outside_the_old_tld_allowlist_is_still_a_link(link):
    """A shortener is the likeliest thing a copy agent emits when told to share a
    demo, and a shortened link in first-touch cold mail is a top spam signal."""
    body = redteam_body(pain=f"{REDTEAM_PAIN} see {link} for it.")
    report = run(make_draft(body=body))
    assert "L1" in {v["rule"] for v in report.violations}, f"{link} passed as clean"


@pytest.mark.parametrize("spelling", [
    "bdαi",                      # Greek alpha
    "bdɑi",                      # Latin alpha
    "ʙᴅᴀɪ",       # small capitals
    "BD41",                           # leetspeak
    "B D A I",                        # spaced
])
def test_the_product_name_is_caught_however_it_is_spelled(spelling):
    body = redteam_body(pain=f"{REDTEAM_PAIN} {spelling} fixes it.")
    report = run(make_draft(body=body))
    assert "L2" in {v["rule"] for v in report.violations}, f"{spelling!r} passed"


@pytest.mark.parametrize("greeting", [
    "None", "null", "N/A", "undefined", "Nan",        # a token that already rendered
    "Owner", "Info", "Sales", "Team",                 # a role, not a person
])
def test_a_rendered_null_or_role_greeting_is_refused(greeting):
    """Worse than sending nothing: it marks the message as automated to exactly the
    skeptical owner-operator this system exists to convert."""
    report = run(make_draft(body=redteam_body(greeting=greeting)))
    assert "L5" in {v["rule"] for v in report.violations}, f"'Hey {greeting},' passed"


@pytest.mark.parametrize("cta", [
    "ring me on 602-555-0134.", "call (602) 555-0134 any time.",
    "tel:+16025550134", "sms:+16025550134",
])
def test_a_phone_number_counts_as_a_second_cta(cta):
    """A number is a stronger CTA than the soft question beside it, and `sms:`
    routes a cold prospect into the channel excluded on TCPA grounds."""
    report = run(make_draft(body=redteam_body(pain=f"{REDTEAM_PAIN} {cta}")))
    assert "L8" in {v["rule"] for v in report.violations}, f"{cta!r} passed"


# --- the other direction: good copy must survive ---------------------------- #

@pytest.mark.parametrize("label,draft_body", [
    ("'solution' is what this ICP calls its own work",
     redteam_body(hook="saw the drainage solution you engineered under that "
                       "Camelback patio - clever work.")),
    ("'thanks,' above the signature",
     redteam_body(signoff="thanks,\nSam")),
    ("a rhetorical question in the hook is not a CTA",
     redteam_body(hook="saw the 40-ton flagstone patio you set in nine days - how "
                       "do you even move that much stone in a week?")),
    ("abbreviations and decimals in the hook",
     redteam_body(hook="saw the 2.5 acre build you did out by St. Louis Park - "
                       "the seat wall is sharp.")),
])
def test_legitimate_copy_is_not_rejected(label, draft_body):
    """A linter that rejects good copy gets routed around, and what Sam would be
    routing around it INTO is every case above this line."""
    report = run(make_draft(body=draft_body))
    assert report.ok, f"{label}: wrongly rejected with {report.violations}"


def test_a_company_actually_named_solutions_is_not_a_banned_word():
    report = run(make_draft(
        body=redteam_body(hook="saw the patio Outdoor Solutions finished off "
                               "Camelback - sharp work.")),
        company_casual="outdoor solutions")
    assert report.ok, report.violations


# =========================================================================== #
# THE CAN-SPAM FOOTER (D-24)
#
# D-24 exempts the block below the `--` line from L6 and L8 — those two rules and
# nothing else. The footer shipped exempt from L1 as well, keyed off a keyword:
# any single link passed as long as the word "unsubscribe" appeared somewhere in
# the block, and L2 never ran over the footer at all despite the note above
# `_FOOTER_DELIMITER` saying it did. Between them that put a link AND the product
# name into a Message 1 that reached READY — the two things hard rule 5 forbids
# ("Email 1 carries no links, no product name, no pitch"), with trigger G6 unable
# to catch either: its LIKE list sees no bare domain, no `BrightFlow Cure` (spaced),
# no `brightdesk`, no `bright-desk ai`.
#
# The exemption is now earned by the LINK — a clean opt-out path on a host we
# actually send from — not by a neighbouring word, not by a path shape any
# domain can wear, and not by a tail permissive enough to swallow a second link.
# =========================================================================== #

FOOTER_ADDRESS = "Northwind Media, 123 Main St, Phoenix AZ 85001"


def footered(footer: str, **kwargs) -> str:
    """The clean red-team body with a `--` delimited CAN-SPAM footer under it."""
    return f"{redteam_body(**kwargs)}\n\n--\n{footer}"


def test_a_footer_with_a_real_opt_out_link_ships():
    """D-24's whole point: a compliant footer has to be *possible*.

    The per-recipient token rides in a path segment, not a query string: a query
    string is the swallow (see below), so `_OPT_OUT_LINK` refuses `[?#&=]` outright
    exactly as `_DEMO_DIRTY` refuses them on the Message 2 demo link.
    """
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\nhttps://northwindmedia.com/unsubscribe/7f3a")))
    assert report.ok, report.summary()


@pytest.mark.parametrize("link", [
    "northwindmedia.com/unsubscribe/7f3a",          # bare domain, no scheme
    "www.northwindmedia.com/optout",
    "https://mail.northwindmedia.com/l/opt-out/9c2",
])
def test_the_opt_out_link_is_recognised_without_a_scheme(link):
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{link}")))
    assert report.ok, report.summary()


def test_a_footer_with_no_link_at_all_ships():
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\nreply stop to opt out.")))
    assert report.ok, report.summary()


@pytest.mark.parametrize("footer", [
    # A bare domain is the exact hole: trigger G6 greps for 'http'/'www.' and sees
    # neither, so lint was the only layer and it was waving this through.
    f"{FOOTER_ADDRESS}\nunsubscribe: brightflowcure.com",
    f"{FOOTER_ADDRESS}\nto unsubscribe, see evil-pay.xyz/inv",
    f"{FOOTER_ADDRESS}\nunsubscribe at www.evil-pay.xyz",
    f"{FOOTER_ADDRESS}\nunsubscribe: https://evil-pay.xyz/inv",
    # 'unsubscribe' in the HOST is not an opt-out path.
    f"{FOOTER_ADDRESS}\nunsubscribe-now.xyz",
    # An open redirect hanging off a genuine opt-out path is not an opt-out link,
    # the same trap `_DEMO_DIRTY` closes for the Message 2 demo link.
    f"{FOOTER_ADDRESS}\nhttps://northwindmedia.com/unsubscribe?next=https://evil-pay.xyz/inv",
    # One opt-out link is the cap; the second is just a link.
    "Northwind Media\nnorthwindmedia.com/unsubscribe or northwind.io/opt-out",
])
def test_a_non_opt_out_link_in_the_footer_is_still_a_link(footer):
    report = run(make_draft(body=footered(footer)))
    assert "L1" in report.rules(), f"{footer!r} shipped a link in Message 1"


@pytest.mark.parametrize("link", [
    # The URL tail was `[^\s<>\]):]*`, so it ate the query string / fragment whole and
    # the second link was blanked away together with the link that earned the
    # exemption. Scheme-less on purpose: G6 (`LIKE '%http%'`, `'%www.%'`) sees none of
    # these, so lint is again the only layer between them and READY.
    "northwindmedia.com/unsubscribe?to=evil-pay.xyz/inv",
    "northwindmedia.com/unsubscribe#evil-pay.xyz/inv",
    # Same swallow with no query string at all — punctuation is a link delimiter.
    "northwindmedia.com/unsubscribe,evil-pay.xyz/inv",
    "northwindmedia.com/unsubscribe;evil-pay.xyz/inv",
    "northwindmedia.com/unsubscribe/evil-pay.xyz/inv",
])
def test_a_second_link_glued_onto_the_opt_out_link_is_still_a_link(link):
    """The carve-out is ONE clean opt-out URL, not everything downstream of it."""
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{link}")))
    assert "L1" in report.rules(), f"{link!r} smuggled a second link into Message 1"


@pytest.mark.parametrize("link", [
    # The host was never checked, so any domain wearing an opt-out-shaped path was
    # exempt. The path shape is not the permission — the sending host is.
    "evil-pay.xyz/unsubscribe",
    "bit.ly/unsubscribe",
    "evilnorthwindmedia.com/unsubscribe",      # a sending domain as a host SUFFIX
    "143.198.22.7/unsubscribe",             # bare IP host
    # 'optout' has to BE a path segment, not merely start one — on any host.
    "evil-pay.xyz/optoutlet/deals-now",
    "northwindmedia.com/optoutlet/deals-now",
])
def test_an_opt_out_path_on_a_host_we_do_not_send_from_is_still_a_link(link):
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{link}")))
    assert "L1" in report.rules(), f"{link!r} shipped a link in Message 1"


@pytest.mark.parametrize("address", [
    "operator@northwindmedia.com",
    "a.d+tag@northwindmedia.com",          # `d` is one letter: not a hostname tail
    "me@northwindmedia.com",               # dotless: never refused by the local-part guard
    "operator@northwindmedia.com.",          # a sentence period is not a path
    "reply to operator@northwindmedia.com and you are off the list.",
])
def test_a_reply_to_address_in_the_footer_is_not_a_link(address):
    """A reply-to address is a normal CAN-SPAM footer element, not a link.

    `_DOMAIN_SHAPED` reaches through `@` on purpose so an address in the MESSAGE is
    an L1 hit (see `test_L1_fires_on_every_url_shape`) — that reach also read the
    host half of a footer address as a bare-domain link once the keyword exemption
    went away, which is a false positive on the most ordinary footer there is.
    """
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\n{address}\nreply stop to opt out.")))
    assert report.ok, report.summary()


@pytest.mark.parametrize("footer", [
    # An address is not permission to carry a destination: with anything glued on it
    # is not an address at all, and the host falls back through to L1.
    "x@evil-pay.xyz/inv",
    "x@northwindmedia.com/inv",                  # not even on a sending domain
    "x@northwindmedia.com/unsubscribe",          # nor wearing the opt-out path
    "x@evil-pay.xyz?to=evil-pay.xyz/inv",
    "x@evil-pay.xyz#evil-pay.xyz/inv",
    "x@evil-pay.xyz,evil-pay.xyz/inv",
    "x@143.198.22.7",                         # bare IP host, still an IP
    "x@143.198.22.7/demo",
    # A domain in the LOCAL part is a domain — it does not ride in behind the blank.
    "evil-pay.xyz@northwindmedia.com",
    "www.evil-pay.xyz@northwindmedia.com",
    # An address next to a link exempts only itself.
    "operator@northwindmedia.com and see evil-pay.xyz/inv",
    "mailto:operator@northwindmedia.com",
])
def test_an_address_does_not_launder_a_link_into_the_footer(footer):
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{footer}")))
    assert "L1" in report.rules(), f"{footer!r} shipped a link in Message 1"


@pytest.mark.parametrize("local", [
    "143.198.22.7",          # a bare IP is a link everywhere else in the module
    "www.evilpay",           # so is anything starting `www.`
    "www2.evilpay",
    "www.evil-pay.wtf",      # ...whatever the suffix
    "xn--evil",              # punycode
    # Shorteners. `rb.gy` is the module's OWN canonical must-block shortener (see
    # test_a_link_outside_the_old_tld_allowlist_is_still_a_link) and it shipped.
    "rb.gy",
    "evil-pay.wtf",
])
def test_a_link_shaped_local_part_is_not_a_reply_to_address(local):
    """The blank covers the WHOLE token, so the local half rides in behind it.

    Guarding that half against a fixed TLD list left exactly the gap `_TLD_HINT`'s
    own docstring warns about ("an allowlist of TLDs is always one entry behind"):
    every suffix off the list walked straight through, `rb.gy@` included. The guard
    is inverted now — L1's own detection runs over the local half, so there is no
    second list to keep in step and nothing to be one entry behind.
    """
    footer = f"{FOOTER_ADDRESS}\n{local}@northwindmedia.com"
    report = run(make_draft(body=footered(footer)))
    assert "L1" in report.rules(), f"{local!r}@ laundered a link into Message 1"
    detail = next(v["detail"] for v in report.violations if v["rule"] == "L1")
    assert f"{local}@northwindmedia.com" in detail, detail


#: Live gTLDs, none of them on `_TLD_HINT` — which is the point: the gTLD space is
#: unbounded, so no allowlist can ever hold this line.
GTLDS_OFF_THE_HINT_LIST = ("plumbing landscaping builders guru digital systems media "
                           "studio group capital finance expert world life plus").split()


@pytest.mark.parametrize("suffix", GTLDS_OFF_THE_HINT_LIST)
def test_a_live_gtld_in_the_local_part_is_not_a_reply_to_address(suffix):
    """`voiceclip.plumbing` resolves. A destination in front of the `@` is still a
    destination, and the whole address was being blanked away before L1 saw it."""
    footer = f"{FOOTER_ADDRESS}\nvoiceclip.{suffix}@northwindmedia.com"
    report = run(make_draft(body=footered(footer)))
    assert "L1" in report.rules(), f"voiceclip.{suffix}@ shipped a link in Message 1"


@pytest.mark.parametrize("local", ["x.pw", "s.id", "t.gg", "j.gy"])
def test_a_one_letter_shortener_in_the_local_part_is_not_a_reply_to_address(local):
    """`_is_prose_dotted` reads a one-letter first label as `a.m`/`e.g` — which is
    also the shape of every one-letter shortener there is.

    A local part is not contractor prose, so that exemption is off when L1 judges the
    halves of an address (`_domain_hits(..., prose_exempt=False)`). The footer scan
    itself keeps the exemption, so the reported snippet here is the host rather than
    the whole token; the refusal is what this pins.
    """
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\n{local}@northwindmedia.com")))
    assert "L1" in report.rules(), f"{local!r}@ laundered a link into Message 1"


@pytest.mark.parametrize("address", [
    # Both halves are prose-exempt on their own: a one-letter first label reads as
    # `e.g`, and so does the host. The strict judging pass refuses to blank these —
    # the local part IS a live shortener — but the footer's own reporting scan keeps
    # the prose exemption, so before the refusal was reported there was nothing left
    # for it to find and the whole body shipped READY-clean.
    "x.pw@a.wtf",
    "s.id@a.gy",
    "t.gg@u.gg",
    "j.gy@a.wtf",
    "x.plumbing@a.wtf",   # the live-gTLD family, reopened by a one-letter first label
    "st.gy@a.wtf",        # `_ABBREVIATIONS` is a second way into the same exemption
])
def test_a_refused_address_is_reported_even_when_both_halves_are_prose_exempt(address):
    """A silent refusal is the same as no rule at all.

    `_blank_addresses` judges with `prose_exempt=False` and leaves a link-bearing
    address standing; `_check_footer` then rescans with the ordinary exemption on. When
    both halves survive that softer pass, the refusal has to speak for itself, or hard
    rule 5 is enforced by nobody for this shape.
    """
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{address}")))
    assert "L1" in report.rules(), f"{address!r} laundered a link into Message 1"


def test_a_plain_reply_to_address_still_earns_the_footer_exemption():
    """The guard above must not swallow the ordinary CAN-SPAM reply-to line."""
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\noperator@northwindmedia.com")))
    assert "L1" not in report.rules(), report.summary()


def test_the_local_part_guard_is_case_blind():
    """A shouted footer is the cheapest bypass there is."""
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\nRB.GY@NORTHWINDMEDIA.COM")))
    assert "L1" in report.rules(), report.summary()


@pytest.mark.parametrize("footer", [
    # `bit dot ly` is an obfuscated domain the module flags anywhere else. The address
    # pattern reads its LAST word as the local part (`ly@`), which is nothing on its
    # own — and the blank then ate the two words in front of it along with the token,
    # so nothing was left for `_iter_links` to find.
    "bit dot ly@northwindmedia.com",
    "rb dot gy@northwindmedia.com",
    "bit [dot] ly@northwindmedia.com",
])
def test_a_link_straddling_the_address_boundary_is_not_blanked_away(footer):
    """Judging the two halves is not enough when the link belongs to neither.

    So the blank is allowed to remove only hits lying WHOLLY INSIDE the address token
    — which for a real reply-to address is exactly one, its own host.
    """
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\n{footer}")))
    assert "L1" in report.rules(), f"{footer!r} laundered a link into Message 1"


@pytest.mark.parametrize("host", [
    "www.evilpay.com",       # the footer-address pattern never constrained the host
    "www2.evilpay.com",
    "xn--evil.com",          # punycode
])
def test_a_link_shaped_host_is_not_a_plain_reply_to_address(host):
    """The carve-out forgives the host for being a BARE DOMAIN and nothing else.

    Every other shape L1 sees in a host it sees everywhere else in the module, and an
    `@` in front does not make it stop being one — so `_host_is_only_a_host` demands
    that the single hit in the host IS the bare domain, spanning the whole host.
    """
    report = run(make_draft(body=footered(f"{FOOTER_ADDRESS}\nx@{host}")))
    assert "L1" in report.rules(), f"x@{host} shipped a link in Message 1"


@pytest.mark.parametrize("address", [
    "john.smith@northwindmedia.com",
    "sam.taylor@example.com",
])
def test_a_dotted_local_part_is_refused_the_exemption_and_that_is_the_trade(address):
    """The deliberate false positive, pinned so nobody re-opens the hole to fix it.

    `john.smith` and `rb.gy` are the same token shape; only a TLD list tells them
    apart, and that list is what failed. Refusing both is the safe side: the cost is
    an optional footer line the author rewrites (undotted local part, or no address
    line at all), against a live shortener in a Message 1 that reached READY.
    """
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\n{address}\nreply stop to opt out.")))
    assert "L1" in report.rules(), report.summary()


@pytest.mark.parametrize("address", [
    "operator@northwindmedia.com",
    "mary-jane.o-brien@northwindmedia.com",  # `o-brien` is not a hostname tail
    "j_c+list@northwindmedia.com",
    "me@northwindmedia.com",                 # one label is not a domain, though `me` is a TLD
    "alex@acmehardscapeaz.com",       # the host is not restricted to a sending domain
])
def test_an_ordinary_reply_to_address_survives_the_local_part_guard(address):
    """The guard must not re-open the false positive it sits inside.

    An UNDOTTED local part is the shape that has to keep working, because it is the
    one that carries no destination. A dotted one does not survive, and cannot: see
    `test_a_dotted_local_part_is_refused_the_exemption_and_that_is_the_trade`.
    """
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\n{address}\nreply stop to opt out.")))
    assert report.ok, report.summary()


@pytest.mark.parametrize("spelling", [
    "BrightFlow Cure", "brightflow", "brightdesk", "bright-desk ai", "BDAI",
])
def test_the_product_name_in_the_footer_is_still_the_product_name(spelling):
    """G6's LIKE list catches none of the spaced/partial spellings here."""
    report = run(make_draft(
        body=footered(f"{spelling}\n{FOOTER_ADDRESS}\nreply stop to opt out.")))
    assert "L2" in report.rules(), f"{spelling!r} named the product in Message 1"
    assert any(v["where"] == "footer" for v in report.violations
               if v["rule"] == "L2"), report.summary()


def test_the_footer_is_still_exempt_from_l6_and_l8():
    """The D-24 exemption itself: an address line after `Sam` is not an unsigned
    message (L6) and 'reply stop to opt out' is not a second CTA (L8)."""
    report = run(make_draft(
        body=footered(f"{FOOTER_ADDRESS}\nreply stop to opt out.")))
    assert "L6" not in report.rules(), report.summary()
    assert "L8" not in report.rules(), report.summary()


def test_the_footer_size_cap_still_bounds_the_exemption():
    report = run(make_draft(body=footered("\n".join([FOOTER_ADDRESS] * 5))))
    assert "L6" in report.rules(), report.summary()
