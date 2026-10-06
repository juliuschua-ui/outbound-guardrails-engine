"""Honest statistics. PLAN.md §4.4, §10 row (h), §13.

Two failure modes are being tested against here, and the second is the dangerous one:

  1. arithmetic that is simply wrong (a Wilson bound above 1.0, a divide by zero);
  2. arithmetic that is right but *presented* as a finding at n=4, which sends Sam
     chasing a variant that is a coin flip.

So there are tests for the numbers, and tests for the gate that decides whether the
numbers are allowed to be called a result.
"""

import math

import pytest
from conftest import PASSING_LINT

from dm_engine import stats
from dm_engine.stats import (
    BANNER_LOW_N,
    MIN_EXPERIMENT_REPLIES,
    MIN_N_DEFAULT,
    VariantStats,
    leading_variant,
    rate,
    replies_by_variant,
    report_tier,
    variant_table,
    wilson,
)

EXPERIMENT = "EXP-TEST"


# --------------------------------------------------------------------------- #
# wilson
# --------------------------------------------------------------------------- #

def test_wilson_no_data_is_total_ignorance():
    assert wilson(0, 0) == (0.0, 1.0)


def test_wilson_zero_successes_does_not_divide_by_zero_or_go_negative():
    low, high = wilson(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.2775402, abs=1e-7)


def test_wilson_all_successes_never_exceeds_one():
    low, high = wilson(10, 10)
    assert high == 1.0
    assert low == pytest.approx(0.7224598, abs=1e-7)


@pytest.mark.parametrize("k,n,expected", [
    (5, 10, (0.2365896, 0.7634104)),
    (1, 8, (0.0224169, 0.4708948)),
    (3, 100, (0.0102543, 0.0845208)),
])
def test_wilson_matches_known_values(k, n, expected):
    low, high = wilson(k, n)
    assert low == pytest.approx(expected[0], abs=1e-7)
    assert high == pytest.approx(expected[1], abs=1e-7)


@pytest.mark.parametrize("k,n", [(1, 8), (3, 100), (5, 10), (17, 43)])
def test_wilson_bounds_solve_the_score_equation(k, n):
    """Independent check: the bounds ARE the roots of |p̂ - p| = z·sqrt(p(1-p)/n).

    This proves the closed form rather than trusting a memorised constant — a
    transcription error in the formula would satisfy neither root.
    """
    z = 1.96
    p_hat = k / n
    for bound in wilson(k, n, z):
        assert abs(p_hat - bound) == pytest.approx(
            z * math.sqrt(bound * (1.0 - bound) / n), abs=1e-12
        )


def test_wilson_is_symmetric():
    for k, n in [(1, 8), (3, 17), (0, 5), (5, 5)]:
        low, high = wilson(k, n)
        mirror_low, mirror_high = wilson(n - k, n)
        assert low == pytest.approx(1.0 - mirror_high, abs=1e-12)
        assert high == pytest.approx(1.0 - mirror_low, abs=1e-12)


def test_wilson_stays_inside_the_unit_interval_everywhere():
    for n in range(1, 60):
        for k in range(n + 1):
            low, high = wilson(k, n)
            assert 0.0 <= low <= high <= 1.0


def test_wilson_narrows_as_n_grows():
    widths = [wilson(n // 2, n)[1] - wilson(n // 2, n)[0] for n in (10, 50, 200, 1000)]
    assert widths == sorted(widths, reverse=True)


@pytest.mark.parametrize("k,n", [(-1, 5), (3, -1), (6, 5)])
def test_wilson_rejects_impossible_counts(k, n):
    with pytest.raises(ValueError):
        wilson(k, n)


def test_rate_is_the_only_division_and_refuses_an_empty_denominator():
    assert rate(1, 8) == 0.125
    assert rate(0, 0) is None
    assert rate(3, 0) is None


# --------------------------------------------------------------------------- #
# fixtures for the table
# --------------------------------------------------------------------------- #

def send(conn, message_id, final="SENT"):
    conn.execute("UPDATE messages SET state = 'READY', lint_report = ? WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'SENT', sent_evidence = 'test' WHERE id = ?",
                 (message_id,))
    if final != "SENT":
        conn.execute("UPDATE messages SET state = ? WHERE id = ?", (final, message_id))
    conn.commit()


def traffic(conn, seed_graph, insert_message, variant_id, sends, *, bounced=0,
            human_replies=0, auto_replies=0, booked=0, prefix=""):
    """Seed `sends` step-1 attempts for one variant and return their prospect ids."""
    prospect_ids = []
    for index in range(sends):
        key = f"{prefix}{variant_id.lower().replace('.', '-')}-{index}.com"
        ids = seed_graph(conn, experiment_id=EXPERIMENT, variant_id=variant_id,
                         prospect_key=key, domain=key, company_name=key,
                         address=f"owner@{key}", batch_id=f"B-{variant_id}")
        send(conn, insert_message(conn, ids),
             final="BOUNCED" if index < bounced else "SENT")
        prospect_ids.append(ids["prospect_id"])

    delivered_ids = prospect_ids[bounced:]
    for prospect_id in delivered_ids[:human_replies]:
        conn.execute(
            "INSERT INTO replies (prospect_id, received_at, classification, is_human, "
            "snippet, classified_by) VALUES (?, datetime('now'), 'hot', 1, ?, 'operator')",
            (prospect_id, "we tried an answering service, it was useless"),
        )
    for prospect_id in delivered_ids[human_replies:human_replies + auto_replies]:
        conn.execute(
            "INSERT INTO replies (prospect_id, received_at, classification, is_human, "
            "snippet, classified_by) VALUES (?, datetime('now'), 'auto_reply', 0, ?, "
            "'heuristic')",
            (prospect_id, "Out of office until Monday"),
        )
    for prospect_id in delivered_ids[:booked]:
        conn.execute(
            "INSERT INTO outcomes (prospect_id, outcome, attribution_method, "
            "attribution_confidence) VALUES (?, 'demo_booked', 'utm', 'exact')",
            (prospect_id,),
        )
    conn.commit()
    return prospect_ids


def row_for(table, variant_id):
    return next(vs for vs in table if vs.variant_id == variant_id)


# --------------------------------------------------------------------------- #
# metric definitions — §10 row (h)
# --------------------------------------------------------------------------- #

def test_the_adversarial_metric_scenario(engine_db, seed_graph, insert_message):
    """10 sends, 2 bounces, 1 human reply, 1 auto-reply, 1 organic booking."""
    traffic(engine_db, seed_graph, insert_message, "V1.1", 10,
            bounced=2, human_replies=1, auto_replies=1)
    # an organic demo: a GHL contact that never came from this engine at all
    engine_db.execute(
        "INSERT INTO prospects (prospect_key, company_name, market, us_state, timezone, "
        "source) VALUES ('organic.com', 'Organic Co', 'Boise ID', 'ID', 'America/Boise', "
        "'manual')")
    organic = engine_db.execute(
        "SELECT id FROM prospects WHERE prospect_key = 'organic.com'").fetchone()["id"]
    engine_db.execute(
        "INSERT INTO outcomes (prospect_id, outcome) VALUES (?, 'demo_booked')", (organic,))
    engine_db.commit()

    row = row_for(variant_table(engine_db, EXPERIMENT), "V1.1")
    assert row.sends == 10
    assert row.bounced == 2
    assert row.delivered == 8                       # bounces leave the denominator
    assert row.replies == 1                         # the auto-reply is not a reply
    assert row.booked == 0                          # organic demos are never claimed
    assert row.reply_rate == pytest.approx(1 / 8)
    assert row.reply_rate_incl_bounces == pytest.approx(1 / 10)
    assert row.ci == pytest.approx(wilson(1, 8))


def test_bounces_stay_visible_rather_than_disappearing(engine_db, seed_graph, insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 4, bounced=4)
    row = row_for(variant_table(engine_db, EXPERIMENT), "V1.1")
    assert (row.sends, row.bounced, row.delivered) == (4, 4, 0)
    assert row.reply_rate is None and row.ci is None    # no denominator, no number


def test_multiple_replies_from_one_person_are_one_signal(engine_db, seed_graph,
                                                         insert_message):
    ids = traffic(engine_db, seed_graph, insert_message, "V1.1", 3, human_replies=1)
    engine_db.execute(
        "INSERT INTO replies (prospect_id, received_at, classification, is_human, snippet, "
        "classified_by) VALUES (?, datetime('now'), 'question', 1, 'and one more thing', "
        "'operator')", (ids[0],))
    engine_db.commit()
    assert row_for(variant_table(engine_db, EXPERIMENT), "V1.1").replies == 1


def test_variants_with_no_data_still_appear(engine_db, seed_graph, insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 2)
    engine_db.execute(
        "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES ('V2.1', 'V2', 1, ?, 'quiet', 'never sent', '{}')", (EXPERIMENT,))
    engine_db.commit()

    table = variant_table(engine_db, EXPERIMENT)
    assert [vs.variant_id for vs in table] == ["V1.1", "V2.1"]
    empty = row_for(table, "V2.1")
    assert (empty.sends, empty.delivered, empty.reply_rate, empty.ci) == (0, 0, None, None)


def test_composed_messages_are_not_attempts(engine_db, seed_graph, insert_message):
    ids = seed_graph(engine_db, experiment_id=EXPERIMENT, variant_id="V1.1")
    insert_message(engine_db, ids)                      # never left COMPOSED
    assert row_for(variant_table(engine_db, EXPERIMENT), "V1.1").sends == 0


def test_by_family_aggregates_versions_and_keeps_the_split(engine_db, seed_graph,
                                                           insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 3, human_replies=1)
    engine_db.execute(
        "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES ('V1.2', 'V1', 2, ?, 'burned-before', 'rewritten', '{}')", (EXPERIMENT,))
    engine_db.commit()
    traffic(engine_db, seed_graph, insert_message, "V1.2", 2, prefix="b")

    table = variant_table(engine_db, EXPERIMENT, by_family=True)
    assert len(table) == 1
    family = table[0]
    assert family.variant_id == "V1"
    assert family.sends == 5 and family.replies == 1
    assert family.family_split == {"V1.1": 3, "V1.2": 2}


def test_experiment_human_replies_counts_people_not_messages(engine_db, seed_graph,
                                                             insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 3,
            human_replies=2, auto_replies=1)
    assert stats.experiment_human_replies(engine_db, EXPERIMENT) == 2


# --------------------------------------------------------------------------- #
# the honesty gate
# --------------------------------------------------------------------------- #

def test_low_n_reports_counts_not_conclusions(engine_db, seed_graph, insert_message):
    """The n=20 regime this project actually ships in."""
    traffic(engine_db, seed_graph, insert_message, "V1.1", 4, human_replies=2)
    traffic(engine_db, seed_graph, insert_message, "V2.1", 4, human_replies=0)

    tier = report_tier(engine_db, EXPERIMENT)
    assert tier["tier"] == "raw"
    assert tier["show_rates"] is False
    assert tier["winner_declarable"] is False
    assert tier["banner"] == BANNER_LOW_N
    assert "counts, not conclusions" in tier["banner"]
    assert str(MIN_N_DEFAULT) in tier["banner"]
    assert str(MIN_EXPERIMENT_REPLIES) in tier["banner"]


def test_one_variant_alone_can_never_be_a_winner(engine_db, seed_graph, insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 60,
            human_replies=MIN_EXPERIMENT_REPLIES)
    tier = report_tier(engine_db, EXPERIMENT)
    assert tier["variants_with_data"] == 1
    assert tier["tier"] == "raw"


def test_gate_needs_both_halves(engine_db, seed_graph, insert_message):
    """Plenty of volume but only 2 replies is still not a result."""
    traffic(engine_db, seed_graph, insert_message, "V1.1", 40, human_replies=2)
    traffic(engine_db, seed_graph, insert_message, "V2.1", 40)

    tier = report_tier(engine_db, EXPERIMENT)
    assert tier["min_delivered"] == 40
    assert tier["human_replies"] == 2
    assert tier["tier"] == "raw"


def test_gate_opens_once_both_halves_are_met(engine_db, seed_graph, insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 40, human_replies=4)
    traffic(engine_db, seed_graph, insert_message, "V2.1", 40, human_replies=1)

    tier = report_tier(engine_db, EXPERIMENT)
    assert tier["tier"] == "wilson"
    assert tier["show_rates"] is True
    assert tier["banner"] is None


def test_the_weakest_arm_sets_the_tier(engine_db, seed_graph, insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 200, human_replies=5)
    traffic(engine_db, seed_graph, insert_message, "V2.1", 10)
    assert report_tier(engine_db, EXPERIMENT)["tier"] == "raw"


# --------------------------------------------------------------------------- #
# leading variant
# --------------------------------------------------------------------------- #

def _stat(variant_id, replies, delivered):
    return VariantStats(
        variant_id=variant_id, sends=delivered, delivered=delivered, bounced=0,
        replies=replies, booked=0, reply_rate=rate(replies, delivered),
        booking_rate=None, ci=wilson(replies, delivered),
    )


def test_overlapping_intervals_have_no_leader():
    table = [_stat("V1.1", 6, 40), _stat("V2.1", 4, 40)]
    assert leading_variant(table) is None


def test_a_leader_needs_separation_from_every_rival():
    strong = _stat("V1.1", 80, 100)          # ci (0.7112, 0.8666)
    weak = _stat("V2.1", 5, 100)             # ci (0.0215, 0.1118) — clearly beaten
    middle = _stat("V3.1", 70, 100)          # ci (0.6042, 0.7811) — overlaps the strong one
    assert leading_variant([strong, weak]) == "V1.1"
    # beating one rival is not a finding across five arms; it is a multiple comparison
    assert leading_variant([strong, weak, middle]) is None


def test_a_single_arm_is_never_leading():
    assert leading_variant([_stat("V1.1", 50, 100)]) is None
    assert leading_variant([]) is None


def test_summary_withholds_a_leader_below_the_gate(engine_db, seed_graph, insert_message):
    """Even a wide separation is not a winner at n=4 — the gate outranks the maths."""
    traffic(engine_db, seed_graph, insert_message, "V1.1", 4, human_replies=4)
    traffic(engine_db, seed_graph, insert_message, "V2.1", 4)

    summary = stats.summary(engine_db, EXPERIMENT)
    assert summary["tier"]["tier"] == "raw"
    assert summary["leader"] is None
    assert summary["separation_note"] == stats.NO_SEPARABLE_SIGNAL


# --------------------------------------------------------------------------- #
# verbatim replies — the honest n=20 deliverable
# --------------------------------------------------------------------------- #

def test_replies_by_variant_returns_verbatim_human_text(engine_db, seed_graph,
                                                        insert_message):
    traffic(engine_db, seed_graph, insert_message, "V1.1", 3,
            human_replies=1, auto_replies=1)
    engine_db.execute(
        "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec) "
        "VALUES ('V2.1', 'V2', 1, ?, 'quiet', 'never answered', '{}')", (EXPERIMENT,))
    engine_db.commit()

    grouped = replies_by_variant(engine_db, EXPERIMENT)
    assert set(grouped) == {"V1.1", "V2.1"}
    assert grouped["V2.1"] == []                       # silence is a result too

    assert len(grouped["V1.1"]) == 1                   # the auto-reply is excluded
    reply = grouped["V1.1"][0]
    assert reply["snippet"] == "we tried an answering service, it was useless"
    assert reply["classification"] == "hot"
    assert reply["address"].startswith("owner@")
    assert reply["company_name"]


def test_replies_by_variant_on_an_empty_experiment(engine_db):
    assert replies_by_variant(engine_db, "EXP-NOTHING") == {}
    assert variant_table(engine_db, "EXP-NOTHING") == []
    tier = report_tier(engine_db, "EXP-NOTHING")
    assert tier["tier"] == "raw" and tier["min_delivered"] == 0
