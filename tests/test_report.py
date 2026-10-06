"""report.py — the sheet, the denominator, and honest presentation.

The centrepiece is `test_reply_rate_denominator_is_delivered_not_sent`: it builds the
exact population PLAN.md §10 row (h) specifies and asserts the exact rate. If that
test ever goes green on the wrong number, every decision downstream is corrupted.
"""

import csv
import json
from dataclasses import dataclass

import pytest

from dm_engine import attribution, replies, report

from conftest import PASSING_LINT


# --------------------------------------------------------------------------- #
# population builder
# --------------------------------------------------------------------------- #

def make_sent(conn, seed_graph, insert_message, index, *, state="SENT",
              variant_id="V1.1", batch_id="B-TEST-1"):
    """Seed one prospect whose step-1 message reaches `state` the legal way."""
    ids = seed_graph(conn, prospect_key=f"co{index}.com", company_name=f"Co {index}",
                     domain=f"co{index}.com", address=f"owner{index}@co{index}.com",
                     variant_id=variant_id, batch_id=batch_id)
    message_id = insert_message(conn, ids, subject=f"subject {index}")
    conn.execute("UPDATE messages SET lint_report = ?, state = 'READY' WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED', gmail_thread_id = ? "
                 "WHERE id = ?", (f"thread-{index}", message_id))
    conn.execute("UPDATE messages SET state = 'SENT', sent_at = datetime('now'), "
                 "sent_evidence = 'test' WHERE id = ?", (message_id,))
    if state != "SENT":
        conn.execute("UPDATE messages SET state = ? WHERE id = ?", (state, message_id))
    conn.commit()
    ids["message_id"] = message_id
    return ids


@pytest.fixture
def adversarial_population(engine_db, seed_graph, insert_message):
    """PLAN.md §10 row (h) verbatim: 10 sends, 2 bounces, 1 human reply,
    1 auto-reply, 1 organic GHL contact. Expected: reply_rate = 1/8, replies = 1,
    booked = 0."""
    people = [make_sent(engine_db, seed_graph, insert_message, i) for i in range(10)]
    for person in people[:2]:
        engine_db.execute("UPDATE messages SET state = 'BOUNCED' WHERE id = ?",
                          (person["message_id"],))
    engine_db.commit()
    # one genuine human reply
    replies.record_reply(engine_db, people[2]["prospect_id"], "email", "hot",
                         "what does it cost?", "operator")
    # one auto-responder — machine, must not count
    replies.record_reply(engine_db, people[3]["prospect_id"], "email", "auto_reply",
                         "Thank you for contacting us, we received your message",
                         "engine")
    return people


# --------------------------------------------------------------------------- #
# THE denominator
# --------------------------------------------------------------------------- #

def test_reply_rate_denominator_is_delivered_not_sent(engine_db, adversarial_population):
    counts = report.population_counts(engine_db)
    assert counts["sends"] == 10
    assert counts["bounced"] == 2
    assert counts["delivered"] == 8
    assert counts["replies"] == 1, "the auto-responder must not be in the numerator"
    assert counts["booked"] == 0
    # 1/8 = 12.5% — not 1/10 (10.0%), not 2/10, not 2/8
    assert report.reply_rate_pct(counts) == 12.5
    assert report.booking_rate_pct(counts) == 0.0


def test_an_organic_ghl_contact_does_not_move_the_booking_count(
        engine_db, adversarial_population, tmp_path):
    path = tmp_path / "ghl.json"
    path.write_text(json.dumps([{
        "id": "ghl-organic", "firstName": "Pat", "companyName": "Nobody We Emailed",
        "website": "https://nobody.example", "dateAdded": "2099-01-01T00:00:00Z"}]),
        encoding="utf-8")
    attribution.match(engine_db, path)
    assert report.population_counts(engine_db)["booked"] == 0


def test_a_bounced_prospect_cannot_enter_the_numerator(engine_db, seed_graph,
                                                       insert_message):
    person = make_sent(engine_db, seed_graph, insert_message, 0, state="BOUNCED")
    # a reply row is force-written against the bounced prospect (the pathological case)
    engine_db.execute(
        "INSERT INTO replies (prospect_id, received_at, classification, is_human, "
        "classified_by) VALUES (?, datetime('now'), 'hot', 1, 'test')",
        (person["prospect_id"],))
    engine_db.commit()
    counts = report.population_counts(engine_db)
    assert counts == {"sends": 1, "bounced": 1, "delivered": 0, "replies": 0, "booked": 0}
    assert report.reply_rate_pct(counts) is None, "no denominator, no rate"


def test_staged_and_composed_messages_are_not_sends(engine_db, seed_graph,
                                                    insert_message):
    ids = seed_graph(engine_db)
    insert_message(engine_db, ids)
    assert report.population_counts(engine_db)["sends"] == 0


def test_report_never_divides(monkeypatch, engine_db, adversarial_population):
    """§10 row (h): all rate arithmetic is delegated to stats.rate, one source."""
    from dm_engine import stats

    calls = []
    original = stats.rate
    monkeypatch.setattr(stats, "rate", lambda k, n: calls.append((k, n)) or original(k, n))
    counts = report.population_counts(engine_db)
    report.reply_rate_pct(counts)
    assert calls == [(1, 8)]


# --------------------------------------------------------------------------- #
# the sheet (SC-2)
# --------------------------------------------------------------------------- #

def test_sheet_has_exactly_the_four_report_columns(engine_db, adversarial_population):
    rows = report.sheet_rows(engine_db)
    assert rows, "sent messages must appear on the sheet"
    assert list(rows[0]) == ["variant", "sent_to", "replied", "booked"]


def test_sheet_covers_every_attempt_including_bounces(engine_db, adversarial_population):
    rows = report.sheet_rows(engine_db)
    assert len(rows) == 10, "a bounced attempt is still an attempt (SC-2: 100%)"
    assert sum(1 for r in rows if r["replied"] == "y") == 1


def test_sheet_full_adds_context_after_the_contract_columns(engine_db,
                                                            adversarial_population):
    rows = report.sheet_rows(engine_db, full=True)
    assert list(rows[0])[:4] == list(report.SHEET_COLUMNS)
    assert "bounced" in rows[0]
    assert sum(r["bounced"] for r in rows) == 2


def test_sheet_is_ordered_chronologically_not_by_performance(engine_db,
                                                             adversarial_population):
    rows = report.sheet_rows(engine_db, full=True)
    sent = [r["sent_at"] for r in rows]
    assert sent == sorted(sent)
    # the replied row is not floated to the top
    assert rows[0]["replied"] in ("y", "n")
    assert [r["replied"] for r in rows] != sorted(
        [r["replied"] for r in rows], reverse=True) or len({r["replied"] for r in rows}) == 1


def test_write_csv_round_trips_every_row(engine_db, adversarial_population, tmp_path):
    rows = report.sheet_rows(engine_db)
    path = tmp_path / "sheet.csv"
    report.write_csv(rows, path)
    with path.open(encoding="utf-8") as handle:
        exported = list(csv.DictReader(handle))
    assert len(exported) == len(rows)
    assert list(exported[0]) == list(report.SHEET_COLUMNS)


def test_write_csv_refuses_to_drop_a_column(tmp_path):
    rows = [{"variant": "V1.1", "sent_to": "a@b.com", "replied": "n", "booked": "n"},
            {"variant": "V2.1", "sent_to": "c@d.com", "replied": "n", "booked": "n",
             "surprise": "extra"}]
    with pytest.raises(report.ReportError):
        report.write_csv(rows, tmp_path / "sheet.csv")


def test_empty_sheet_still_writes_the_header(tmp_path, engine_db):
    path = tmp_path / "sheet.csv"
    report.write_csv(report.sheet_rows(engine_db), path)
    assert path.read_text(encoding="utf-8").strip() == ",".join(report.SHEET_COLUMNS)


# --------------------------------------------------------------------------- #
# DASHBOARD metrics
# --------------------------------------------------------------------------- #

def test_metrics_file_matches_the_dashboard_contract(engine_db, engine_cfg,
                                                  adversarial_population):
    path = report.emit_dashboard_metrics(engine_db, engine_cfg)
    assert path.parent == engine_cfg.dashboard_metrics_dir
    assert path.name.startswith("pull-") and path.name.endswith(".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"generatedAt", "metrics"}
    assert payload["generatedAt"].endswith("Z")
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in payload["metrics"].values())
    assert all(key.startswith("dm.") for key in payload["metrics"])


def test_metrics_carry_the_denominator_alongside_the_rate(engine_db, engine_cfg,
                                                          adversarial_population):
    metrics = report.collect_metrics(engine_db)
    assert metrics["dm.sends_total"] == 10
    assert metrics["dm.delivered_total"] == 8
    assert metrics["dm.bounced_total"] == 2
    assert metrics["dm.replies_human_total"] == 1
    assert metrics["dm.reply_rate_delivered_pct"] == 12.5
    assert metrics["dm.demos_booked_total"] == 0
    assert metrics["dm.attempts_gap_to_20"] == 10


def test_rate_key_is_omitted_not_zeroed_when_nothing_delivered(engine_db):
    metrics = report.collect_metrics(engine_db)
    assert "dm.reply_rate_delivered_pct" not in metrics
    assert metrics["dm.sends_total"] == 0


def test_no_revenue_or_pipeline_metric_is_emitted(engine_db, adversarial_population):
    """GHL monetaryValue is 0 everywhere — such a metric would be garbage (§8.3)."""
    keys = " ".join(report.collect_metrics(engine_db))
    for banned in ("revenue", "pipeline", "value_usd", "monetary", "won"):
        assert banned not in keys


def test_metrics_emit_never_overwrites_an_existing_snapshot(engine_db, engine_cfg):
    first = report.emit_dashboard_metrics(engine_db, engine_cfg)
    second = report.emit_dashboard_metrics(engine_db, engine_cfg)
    assert first != second
    assert first.exists() and second.exists()


def test_metrics_land_only_under_the_redirected_dir(engine_db, engine_cfg, tmp_path):
    path = report.emit_dashboard_metrics(engine_db, engine_cfg)
    assert path.is_relative_to(tmp_path), "a test must never write into real DASHBOARD"


# --------------------------------------------------------------------------- #
# approvals cards
# --------------------------------------------------------------------------- #

def test_approvals_card_matches_the_exec_card_shape(engine_cfg):
    path = report.write_approvals_card(engine_cfg, "dmengine-1", "title", "summary",
                                       "link")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == ["items"]
    assert set(payload["items"][0]) == {"id", "title", "summary", "link"}
    assert path.name.startswith("dmengine-")


def test_approvals_cards_share_a_file_and_dedupe_by_id(engine_cfg):
    report.write_approvals_card(engine_cfg, "card-a", "A", "s", "l")
    report.write_approvals_card(engine_cfg, "card-b", "B", "s", "l")
    path = report.write_approvals_card(engine_cfg, "card-a", "A2", "s2", "l2")
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert [i["id"] for i in items] == ["card-b", "card-a"]
    assert items[-1]["title"] == "A2"


# --------------------------------------------------------------------------- #
# honest presentation
# --------------------------------------------------------------------------- #

@dataclass
class FakeVariantStats:
    variant_id: str
    sends: int
    delivered: int
    bounced: int
    replies: int
    booked: int
    reply_rate: float | None = None
    booking_rate: float | None = None
    ci: tuple | None = None


def low_n_rows():
    return [
        FakeVariantStats("V3.1", 4, 4, 0, 3, 1),   # the "obvious winner" at n=4
        FakeVariantStats("V1.1", 4, 4, 0, 0, 0),
        FakeVariantStats("V2.1", 4, 3, 1, 1, 0),
    ]


def test_below_the_gate_no_rate_is_printed_at_all():
    text = report.render_stats(report.summary_from_rows(low_n_rows(), 4))
    assert "%" not in text
    assert report.LOW_N_BANNER in text


def test_below_the_gate_no_winner_language_appears():
    text = report.render_stats(report.summary_from_rows(low_n_rows(), 4))
    table = [line.lower() for line in text.splitlines() if line.startswith("V")]
    for line in table:
        for word in ("leading", "winner", "wins", "best", "outperform"):
            assert word not in line, f"a variant is being promoted at n=4: {line}"
    # the only place 'winner' may appear is the banner, denying one
    assert "no winner is declarable" in text.lower()


def test_rows_are_ordered_by_variant_id_never_by_performance():
    text = report.render_stats(report.summary_from_rows(low_n_rows(), 4))
    order = [line.split()[0] for line in text.splitlines()
             if line.startswith("V")]
    assert order == ["V1.1", "V2.1", "V3.1"]


def test_the_gate_needs_both_n_and_replies():
    rows = [FakeVariantStats("V1.1", 40, 40, 0, 4, 0, ci=(0.03, 0.24)),
            FakeVariantStats("V2.1", 40, 40, 0, 2, 0, ci=(0.01, 0.17))]
    # enough n, not enough replies
    assert "%" not in report.render_stats(report.summary_from_rows(rows, 4))
    # enough of both
    assert "%" in report.render_stats(report.summary_from_rows(rows, 6))


def test_above_the_gate_every_rate_carries_its_n():
    rows = [FakeVariantStats("V1.1", 40, 40, 0, 4, 1, ci=(0.03, 0.24)),
            FakeVariantStats("V2.1", 40, 38, 2, 2, 0, ci=(0.01, 0.18))]
    text = report.render_stats(report.summary_from_rows(rows, 6))
    for line in text.splitlines():
        if "%" in line:
            assert "(n=" in line, f"a rate without its n: {line}"


def test_leading_is_only_declared_on_disjoint_intervals():
    overlapping = [FakeVariantStats("V1.1", 40, 40, 0, 12, 0, ci=(0.18, 0.45)),
                   FakeVariantStats("V2.1", 40, 40, 0, 5, 0, ci=(0.05, 0.27))]
    assert "leading" not in report.render_stats(report.summary_from_rows(overlapping, 17))
    assert "no separable signal" in report.render_stats(
        report.summary_from_rows(overlapping, 17))

    disjoint = [FakeVariantStats("V1.1", 200, 200, 0, 80, 0, ci=(0.34, 0.47)),
                FakeVariantStats("V2.1", 200, 200, 0, 10, 0, ci=(0.03, 0.09))]
    assert "leading" in report.render_stats(report.summary_from_rows(disjoint, 90))


def test_verbatim_replies_are_the_low_n_deliverable():
    verbatim = {"V1.1": [{"classification": "hot", "snippet": "we tried a chatbot, "
                                                              "it was useless"}],
                "V2.1": []}
    text = report.render_stats(report.summary_from_rows(low_n_rows(), 4),
                               replies_by_variant=verbatim)
    assert "we tried a chatbot, it was useless" in text
    assert "V2.1 (0)" in text


def test_render_stats_consumes_owner_b_summary(engine_db, adversarial_population):
    from dm_engine import stats

    summary = stats.summary(engine_db, "EXP-TEST")
    text = report.render_stats(summary)
    assert "experiment: EXP-TEST" in text
    assert "%" not in text, "n=10 is far below the gate"


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_reports_the_denominator_explicitly(engine_db, adversarial_population):
    text = report.render_status(report.status_summary(engine_db))
    assert "delivered         8" in text
    assert "reply-rate denominator" in text
    assert "12.5% (n=8 delivered)" in text


def test_status_caveats_a_rate_computed_on_a_tiny_denominator(engine_db, seed_graph,
                                                              insert_message):
    """'100.0% reply rate' is technically true at n=1 and useless — say so."""
    person = make_sent(engine_db, seed_graph, insert_message, 0)
    replies.record_reply(engine_db, person["prospect_id"], "email", "hot", "yes",
                         "operator")
    text = report.render_status(report.status_summary(engine_db))
    assert "100.0% (n=1 delivered)" in text
    assert "too small to mean anything" in text


def test_status_warns_about_the_sc1_gap(engine_db, adversarial_population):
    summary = report.status_summary(engine_db)
    assert any("SC-1" in w for w in summary["warnings"])


def test_status_warns_about_an_unattributed_booking(engine_db, adversarial_population):
    person = adversarial_population[4]
    engine_db.execute("INSERT INTO outcomes (prospect_id, outcome) VALUES (?, ?)",
                      (person["prospect_id"], "demo_booked"))
    engine_db.commit()
    warnings = report.status_summary(engine_db)["warnings"]
    assert any("attribution_method" in w for w in warnings)
