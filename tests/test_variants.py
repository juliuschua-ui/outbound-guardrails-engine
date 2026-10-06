"""Variant registry + deterministic assignment. PLAN.md §4.1-§4.4, §10 row (g).

The load-bearing tests are `test_reassign_identical` and the drift family: if the
mapping from prospect to variant cannot be recomputed from stored inputs, every
per-variant number this engine prints is unfalsifiable.
"""

import hashlib
import json
from pathlib import Path

import pytest
from conftest import PASSING_LINT

from dm_engine import stats, variants
from dm_engine.variants import (
    ALGO,
    AssignmentDrift,
    GateRefused,
    SeedError,
    assign_batch,
    assign_hash,
    load_seed,
    plan_assignment,
    variant_set_hash,
)

SEED_FILE = Path(__file__).resolve().parents[1] / "data" / "variants" / "EXP-2026-08-skeptic.json"
EXPERIMENT = "EXP-2026-08-skeptic"

#: Recomputable by hand, exactly as §10 row (g) tells the reviewer to:
#:   python -c "import hashlib; print(hashlib.sha256(b'EXP-2026-08-skeptic|acmehardscapeaz.com').hexdigest())"
KNOWN_ASSIGN_HASH = "04bc483d55be76e529fe4e7eb99d6d02c0d43432a2733ff02be1f42917e4faf6"


# --------------------------------------------------------------------------- #
# seeding helpers (assignment needs prospects WITHOUT assignments, so conftest's
# seed_graph — which always writes one — is the wrong tool here)
# --------------------------------------------------------------------------- #

def make_batch(conn, batch_id, experiment_id=EXPERIMENT, market="Boise ID", seq=0,
               channel="email"):
    conn.execute(
        "INSERT INTO batches (id, experiment_id, market, channel, personalization, batch_seq) "
        "VALUES (?, ?, ?, ?, 'unique', ?)",
        (batch_id, experiment_id, market, channel, seq),
    )
    conn.commit()


def make_prospects(conn, keys, market="Boise ID", state="QUALIFIED", channel="email"):
    """Prospects that are actually REACHABLE on `channel`.

    `assign_batch` only claims prospects holding a contact on the batch's channel, so
    a prospect with no contacts is not a candidate for anything. Pass `channel=None`
    to seed an unreachable prospect deliberately.
    """
    ids = []
    for key in keys:
        cur = conn.execute(
            "INSERT INTO prospects (prospect_key, company_name, market, us_state, timezone, "
            "source, state) VALUES (?, ?, ?, 'ID', 'America/Boise', 'manual', ?)",
            (key, key, market, state),
        )
        if channel is not None:
            address = {"whatsapp": f"+1602555{cur.lastrowid:04d}",
                       "messenger": f"https://m.me/{key.split('.')[0]}",
                       "linkedin": f"https://linkedin.com/in/{key.split('.')[0]}",
                       }.get(channel, f"owner@{key}")
            conn.execute(
                "INSERT INTO contacts (prospect_id, person_key, channel, address, "
                "address_source) VALUES (?, ?, ?, ?, 'website')",
                (cur.lastrowid, address, channel, address),
            )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def seeded(conn):
    load_seed(conn, SEED_FILE)
    return variants.active_variant_ids(conn, EXPERIMENT)


# --------------------------------------------------------------------------- #
# the seed file itself
# --------------------------------------------------------------------------- #

def test_seed_file_carries_the_five_skeptic_families():
    data = json.loads(SEED_FILE.read_text())
    assert data["experiment"]["id"] == EXPERIMENT
    names = [v["name"] for v in data["variants"]]
    assert names == ["burned-before", "missed-call-math", "off-the-tools",
                     "after-hours", "competitor-quietly-wins"]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_seed_guidance_is_message_one_safe():
    """Guidance gets pasted into compose jobs; it must not smuggle a lint failure.

    L1/L2 ban links and the product name; L5 hard-fails brace or bracket
    placeholders. Guidance copied verbatim by an agent is the likeliest route for
    any of those to reach a body, so the guidance itself carries none.
    """
    data = json.loads(SEED_FILE.read_text())
    for text in _strings(data):
        lowered = text.lower()
        for banned in ("http://", "https://", "www.", "brightflowcure", "bdai",
                       "bright desk ai"):
            assert banned not in lowered, f"{banned!r} in seed guidance: {text!r}"
        for placeholder in "{}[]":
            assert placeholder not in text, f"{placeholder!r} in seed guidance: {text!r}"


def test_every_variant_spec_carries_the_pain_first_structure():
    for variant in json.loads(SEED_FILE.read_text())["variants"]:
        spec = variant["spec"]
        assert spec["structure"] == ["pain_opener", "soft_question"]
        assert spec["soft_question_examples"]
        assert all(question.endswith("?") for question in spec["soft_question_examples"])
        assert spec["no_name_open"]          # §5.3 — first_name may be missing
        assert spec["pain_named"] and spec["opening_move"]
        assert "120 words" in spec["email_adaptation"]
        assert "3 sentences" in spec["dm_constraint"]


def test_load_seed_creates_the_experiment_and_five_variants(engine_db):
    written = load_seed(engine_db, SEED_FILE)
    assert written == 5

    experiment = engine_db.execute("SELECT * FROM experiments").fetchone()
    assert experiment["id"] == EXPERIMENT
    assert "burned" in experiment["hypothesis"]

    ids = variants.active_variant_ids(engine_db, EXPERIMENT)
    assert ids == ["V1.1", "V2.1", "V3.1", "V4.1", "V5.1"]
    assert all(row["min_n_gate"] == 30
               for row in engine_db.execute("SELECT min_n_gate FROM variants"))


def test_load_seed_is_idempotent(engine_db):
    load_seed(engine_db, SEED_FILE)
    assert load_seed(engine_db, SEED_FILE) == 0
    assert engine_db.execute("SELECT count(*) FROM variants").fetchone()[0] == 5


def test_unused_variant_may_be_edited_in_place(engine_db, tmp_path):
    load_seed(engine_db, SEED_FILE)
    data = json.loads(SEED_FILE.read_text())
    data["variants"][0]["angle"] = "a sharper angle, nothing has shipped yet"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(data))

    assert load_seed(engine_db, edited) == 1
    assert engine_db.execute("SELECT count(*) FROM variants").fetchone()[0] == 5
    assert engine_db.execute(
        "SELECT angle FROM variants WHERE id = 'V1.1'").fetchone()["angle"].startswith("a sharper")


def test_used_variant_becomes_a_new_version_instead_of_mutating(engine_db, tmp_path,
                                                                seed_graph, insert_message):
    """§4.1 — statistics attach to the exact copy that was sent."""
    load_seed(engine_db, SEED_FILE)
    ids = seed_graph(engine_db, experiment_id=EXPERIMENT, variant_id="V1.1")
    insert_message(engine_db, ids)

    data = json.loads(SEED_FILE.read_text())
    data["variants"][0]["angle"] = "rewritten after the first send"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(data))

    assert load_seed(engine_db, edited) == 1
    rows = {r["id"]: r for r in engine_db.execute(
        "SELECT id, status, angle FROM variants WHERE family = 'V1'")}
    assert rows["V1.1"]["status"] == "retired"
    assert rows["V1.1"]["angle"] != "rewritten after the first send"
    assert rows["V1.2"]["status"] == "active"
    assert rows["V1.2"]["angle"] == "rewritten after the first send"

    # and re-running the edited file does not keep minting versions
    assert load_seed(engine_db, edited) == 0
    assert engine_db.execute("SELECT count(*) FROM variants").fetchone()[0] == 6


@pytest.mark.parametrize("payload", [
    {"variants": []},
    {"experiment": {"id": ""}, "variants": []},
    {"experiment": {"id": "E"}, "variants": [{"family": "V1"}]},
    {"experiment": {"id": "E"}, "variants": [{"family": "V1", "name": "n", "angle": "a",
                                              "spec": "not-json"}]},
])
def test_load_seed_rejects_a_malformed_file(engine_db, tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(SeedError):
        load_seed(engine_db, path)


# --------------------------------------------------------------------------- #
# the hashes
# --------------------------------------------------------------------------- #

def test_assign_hash_is_the_documented_sha256():
    assert assign_hash(EXPERIMENT, "acmehardscapeaz.com") == KNOWN_ASSIGN_HASH
    assert assign_hash(EXPERIMENT, "acmehardscapeaz.com") == hashlib.sha256(
        b"EXP-2026-08-skeptic|acmehardscapeaz.com").hexdigest()


def test_variant_set_hash_tracks_membership_and_order():
    base = variant_set_hash(["V1.1", "V2.1", "V3.1"])
    assert base == variant_set_hash(["V1.1", "V2.1", "V3.1"])
    assert base != variant_set_hash(["V3.1", "V2.1", "V1.1"])
    assert base != variant_set_hash(["V1.1", "V2.1"])


def test_plan_assignment_is_a_pure_function():
    members = [(i, f"co{i}.com") for i in range(1, 8)]
    first = plan_assignment(EXPERIMENT, "B-1", 0, ["V1.1", "V2.1", "V3.1"], members)
    second = plan_assignment(EXPERIMENT, "B-1", 0, ["V1.1", "V2.1", "V3.1"],
                             list(reversed(members)))
    assert first == second                      # input order cannot change the outcome


def test_plan_assignment_needs_variants():
    with pytest.raises(ValueError):
        plan_assignment(EXPERIMENT, "B-1", 0, [], [(1, "a.com")])


# --------------------------------------------------------------------------- #
# new_batch — batch_seq is the §4.3 round-robin offset, not bookkeeping
# --------------------------------------------------------------------------- #

def test_new_batch_allocates_sequential_seqs_per_experiment(engine_db):
    seeded(engine_db)
    first = variants.new_batch(engine_db, "B-1", EXPERIMENT, "Boise ID", "email")
    second = variants.new_batch(engine_db, "B-2", EXPERIMENT, "Boise ID", "linkedin")
    assert (first, second) == (1, 2)
    stored = engine_db.execute(
        "SELECT id, batch_seq, channel, personalization, state FROM batches "
        "ORDER BY batch_seq").fetchall()
    assert [(r["id"], r["batch_seq"]) for r in stored] == [("B-1", 1), ("B-2", 2)]
    assert (stored[0]["channel"], stored[0]["personalization"]) == ("email", "unique")
    assert stored[0]["state"] == "OPEN"


def test_new_batch_seq_makes_consecutive_batches_start_on_different_variants(engine_db):
    """The whole point of batch_seq: two batches must not round-robin identically."""
    seeded(engine_db)
    variants.new_batch(engine_db, "B-1", EXPERIMENT, "Boise ID", "email")
    variants.new_batch(engine_db, "B-2", EXPERIMENT, "Boise ID", "email")
    make_prospects(engine_db, ["a.com"])
    first = assign_batch(engine_db, EXPERIMENT, "B-1")[0]["variant_id"]
    make_prospects(engine_db, ["b.com"])
    second = assign_batch(engine_db, EXPERIMENT, "B-2")[0]["variant_id"]
    assert first != second


def test_new_batch_refuses_to_reuse_a_batch_id(engine_db):
    seeded(engine_db)
    variants.new_batch(engine_db, "B-1", EXPERIMENT, "Boise ID", "email")
    with pytest.raises(ValueError, match="already exists"):
        variants.new_batch(engine_db, "B-1", EXPERIMENT, "Glendale AZ", "email")
    assert engine_db.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
    assert engine_db.execute(
        "SELECT market FROM batches").fetchone()["market"] == "Boise ID"


def test_new_batch_refuses_an_unknown_experiment(engine_db):
    with pytest.raises(KeyError):
        variants.new_batch(engine_db, "B-1", "EXP-NOPE", "Boise ID", "email")
    assert engine_db.execute("SELECT count(*) FROM batches").fetchone()[0] == 0


def test_new_batch_demands_a_batch_id(engine_db):
    seeded(engine_db)
    with pytest.raises(ValueError):
        variants.new_batch(engine_db, "   ", EXPERIMENT, "Boise ID", "email")


def test_new_batch_counts_seqs_within_one_experiment_only(engine_db):
    seeded(engine_db)
    engine_db.execute("INSERT INTO experiments (id, name) VALUES ('EXP-OTHER', 'other')")
    variants.new_batch(engine_db, "B-1", EXPERIMENT, "Boise ID", "email")
    variants.new_batch(engine_db, "B-2", EXPERIMENT, "Boise ID", "email")
    assert variants.new_batch(engine_db, "B-3", "EXP-OTHER", "Boise ID", "email") == 1


# --------------------------------------------------------------------------- #
# assign_batch
# --------------------------------------------------------------------------- #

def test_assign_batch_covers_the_market_and_balances_within_one(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1", seq=0)
    make_prospects(engine_db, [f"co{i}.com" for i in range(12)])

    rows = assign_batch(engine_db, EXPERIMENT, "B-1")
    assert len(rows) == 12
    counts = {}
    for row in rows:
        counts[row["variant_id"]] = counts.get(row["variant_id"], 0) + 1
    assert set(counts) == {"V1.1", "V2.1", "V3.1", "V4.1", "V5.1"}
    assert max(counts.values()) - min(counts.values()) <= 1


def test_assign_batch_persists_the_algorithm_and_both_hashes(engine_db):
    variant_ids = seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, ["acmehardscapeaz.com"])
    assign_batch(engine_db, EXPERIMENT, "B-1")

    row = engine_db.execute("SELECT * FROM assignments").fetchone()
    assert row["algo"] == ALGO
    assert row["assign_hash"] == KNOWN_ASSIGN_HASH
    assert row["variant_set_hash"] == variant_set_hash(variant_ids)


def test_batch_seq_rotates_the_starting_variant(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-0", seq=0)
    make_batch(engine_db, "B-1", seq=1, market="Mesa AZ")
    make_prospects(engine_db, ["a.com"], market="Boise ID")
    make_prospects(engine_db, ["a2.com"], market="Mesa AZ")

    first = assign_batch(engine_db, EXPERIMENT, "B-0")[0]["variant_id"]
    second = assign_batch(engine_db, EXPERIMENT, "B-1")[0]["variant_id"]
    assert (first, second) == ("V1.1", "V2.1")           # offset = batch_seq % 5


def test_small_batch_still_spreads_across_distinct_variants(engine_db):
    """§9 edge 21 — three prospects must not all land on V1."""
    seeded(engine_db)
    make_batch(engine_db, "B-1", seq=0)
    make_prospects(engine_db, ["a.com", "b.com", "c.com"])

    rows = assign_batch(engine_db, EXPERIMENT, "B-1")
    assert len({row["variant_id"] for row in rows}) == 3


def test_limit_caps_the_batch_reproducibly(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, [f"co{i}.com" for i in range(20)])

    rows = assign_batch(engine_db, EXPERIMENT, "B-1", limit=10)
    assert len(rows) == 10
    # the cut is by assign_hash, so it is the same ten every time
    expected = sorted(assign_hash(EXPERIMENT, f"co{i}.com") for i in range(20))[:10]
    assert sorted(row["assign_hash"] for row in rows) == expected


def test_only_qualified_unassigned_prospects_in_the_market_are_drawn(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, ["good.com"])
    make_prospects(engine_db, ["new.com"], state="NEW")
    make_prospects(engine_db, ["excluded.com"], state="EXCLUDED")
    make_prospects(engine_db, ["elsewhere.com"], market="Mesa AZ")

    rows = assign_batch(engine_db, EXPERIMENT, "B-1")
    assert [row["prospect_key"] for row in rows] == ["good.com"]


def test_a_prospect_keeps_its_variant_across_batches(engine_db):
    """UNIQUE(experiment_id, prospect_id): re-imported in a new batch, same variant."""
    seeded(engine_db)
    make_batch(engine_db, "B-1", seq=0)
    make_batch(engine_db, "B-2", seq=1)
    make_prospects(engine_db, ["a.com"])
    first = assign_batch(engine_db, EXPERIMENT, "B-1")

    second = assign_batch(engine_db, EXPERIMENT, "B-2")
    assert second == []                                   # already claimed, not re-drawn
    stored = engine_db.execute("SELECT count(*) FROM assignments").fetchone()[0]
    assert stored == 1
    assert variants.get_assignment(engine_db, EXPERIMENT, first[0]["prospect_id"]
                                   )["variant_id"] == first[0]["variant_id"]


def test_assign_batch_rejects_a_batch_from_another_experiment(engine_db):
    seeded(engine_db)
    engine_db.execute("INSERT INTO experiments (id, name) VALUES ('EXP-OTHER', 'other')")
    make_batch(engine_db, "B-1", experiment_id="EXP-OTHER")
    with pytest.raises(ValueError):
        assign_batch(engine_db, EXPERIMENT, "B-1")


def test_assign_batch_rejects_an_unknown_batch(engine_db):
    seeded(engine_db)
    with pytest.raises(KeyError):
        assign_batch(engine_db, EXPERIMENT, "B-nope")


def test_assign_batch_refuses_when_no_variant_is_active(engine_db):
    load_seed(engine_db, SEED_FILE)
    engine_db.execute("UPDATE variants SET status = 'retired'")
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, ["a.com"])
    with pytest.raises(ValueError):
        assign_batch(engine_db, EXPERIMENT, "B-1")


# --------------------------------------------------------------------------- #
# reproducibility and drift — §10 row (g)
# --------------------------------------------------------------------------- #

def test_reassign_identical(engine_db):
    """Assign, recompute, assert identical. This is the scientific-integrity test."""
    seeded(engine_db)
    make_batch(engine_db, "B-1", seq=3)
    make_prospects(engine_db, [f"co{i}.com" for i in range(11)])

    first = assign_batch(engine_db, EXPERIMENT, "B-1")
    stored = [dict(r) for r in engine_db.execute(
        "SELECT prospect_id, variant_id, assign_hash, variant_set_hash, algo "
        "FROM assignments ORDER BY prospect_id")]

    second = assign_batch(engine_db, EXPERIMENT, "B-1")
    assert [(r["prospect_id"], r["variant_id"]) for r in second] == \
           [(r["prospect_id"], r["variant_id"]) for r in first]
    assert all(row["created"] is False for row in second)
    assert [dict(r) for r in engine_db.execute(
        "SELECT prospect_id, variant_id, assign_hash, variant_set_hash, algo "
        "FROM assignments ORDER BY prospect_id")] == stored


def test_reassign_survives_later_imports_into_the_same_market(engine_db):
    """Membership freezes at first assign, so new prospects cannot rewrite history."""
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, ["a.com", "b.com", "c.com"])
    first = assign_batch(engine_db, EXPERIMENT, "B-1")

    make_prospects(engine_db, ["d.com", "e.com"])
    second = assign_batch(engine_db, EXPERIMENT, "B-1")
    assert second == [{**row, "created": False} for row in first]


def test_tamper_detect_on_a_mutated_hash(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, [f"co{i}.com" for i in range(6)])
    assign_batch(engine_db, EXPERIMENT, "B-1")

    engine_db.execute("UPDATE assignments SET assign_hash = 'deadbeef' WHERE id = 1")
    engine_db.commit()

    with pytest.raises(AssignmentDrift, match="assign_hash"):
        assign_batch(engine_db, EXPERIMENT, "B-1")


def test_tamper_detect_on_a_swapped_variant(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, [f"co{i}.com" for i in range(6)])
    rows = assign_batch(engine_db, EXPERIMENT, "B-1")

    victim = rows[0]
    other = "V5.1" if victim["variant_id"] != "V5.1" else "V4.1"
    engine_db.execute("UPDATE assignments SET variant_id = ? WHERE prospect_id = ?",
                      (other, victim["prospect_id"]))
    engine_db.commit()

    with pytest.raises(AssignmentDrift, match="variant_id"):
        assign_batch(engine_db, EXPERIMENT, "B-1")


def test_drift_aborts_when_the_active_variant_set_changes(engine_db):
    """§9 edge 20 — a retired variant means a NEW batch, never a silent reassignment."""
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, [f"co{i}.com" for i in range(6)])
    assign_batch(engine_db, EXPERIMENT, "B-1")

    engine_db.execute("UPDATE variants SET status = 'retired' WHERE id = 'V5.1'")
    engine_db.commit()

    with pytest.raises(AssignmentDrift, match="variant_set_hash"):
        assign_batch(engine_db, EXPERIMENT, "B-1")


def test_drift_writes_nothing(engine_db):
    seeded(engine_db)
    make_batch(engine_db, "B-1")
    make_prospects(engine_db, [f"co{i}.com" for i in range(6)])
    assign_batch(engine_db, EXPERIMENT, "B-1")
    engine_db.execute("UPDATE assignments SET assign_hash = 'deadbeef' WHERE id = 1")
    engine_db.commit()
    before = [dict(r) for r in engine_db.execute("SELECT * FROM assignments ORDER BY id")]

    with pytest.raises(AssignmentDrift):
        assign_batch(engine_db, EXPERIMENT, "B-1")

    after = [dict(r) for r in engine_db.execute("SELECT * FROM assignments ORDER BY id")]
    assert after == before


# --------------------------------------------------------------------------- #
# the min-n gate — §4.4
# --------------------------------------------------------------------------- #

def send(conn, message_id, final="SENT"):
    """Walk a message through the trigger-enforced path to a terminal send state."""
    conn.execute("UPDATE messages SET state = 'READY', lint_report = ? WHERE id = ?",
                 (PASSING_LINT, message_id))
    conn.execute("UPDATE messages SET state = 'APPROVED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'STAGED' WHERE id = ?", (message_id,))
    conn.execute("UPDATE messages SET state = 'SENT', sent_evidence = 'test' WHERE id = ?",
                 (message_id,))
    if final != "SENT":
        conn.execute("UPDATE messages SET state = ? WHERE id = ?", (final, message_id))
    conn.commit()


def build_traffic(conn, seed_graph, insert_message, variant_id, count, replies=0,
                  experiment_id=EXPERIMENT, start=0):
    for index in range(count):
        key = f"{variant_id.lower().replace('.', '-')}-{start + index}.com"
        ids = seed_graph(conn, experiment_id=experiment_id, variant_id=variant_id,
                         prospect_key=key, domain=key, company_name=key,
                         address=f"owner@{key}", batch_id=f"B-{variant_id}")
        send(conn, insert_message(conn, ids))
        if index < replies:
            conn.execute(
                "INSERT INTO replies (prospect_id, received_at, classification, is_human, "
                "snippet, classified_by) VALUES (?, datetime('now'), 'hot', 1, ?, 'operator')",
                (ids["prospect_id"], f"interested — {key}"),
            )
    conn.commit()


def test_retire_refuses_below_the_gate(engine_db, seed_graph, insert_message):
    load_seed(engine_db, SEED_FILE)
    build_traffic(engine_db, seed_graph, insert_message, "V1.1", 4, replies=1)

    with pytest.raises(GateRefused, match="sample too small"):
        variants.retire(engine_db, "V1.1", force=False, reason=None, actor="operator")
    assert engine_db.execute(
        "SELECT status FROM variants WHERE id = 'V1.1'").fetchone()["status"] == "active"


def test_gate_status_reports_both_halves(engine_db, seed_graph, insert_message):
    load_seed(engine_db, SEED_FILE)
    build_traffic(engine_db, seed_graph, insert_message, "V1.1", 4, replies=1)

    status = variants.min_n_gate_status(engine_db, "V1.1")
    assert status["delivered"] == 4 and status["min_n"] == 30
    assert status["human_replies"] == 1 and status["required_replies"] == 5
    assert status["ok"] is False


def test_forced_retire_demands_a_reason_and_leaves_an_audit_trail(engine_db, seed_graph,
                                                                  insert_message):
    load_seed(engine_db, SEED_FILE)
    build_traffic(engine_db, seed_graph, insert_message, "V1.1", 4, replies=1)

    with pytest.raises(ValueError):
        variants.retire(engine_db, "V1.1", force=True, reason="  ", actor="operator")

    variants.retire(engine_db, "V1.1", force=True, reason="copy is off-brand",
                    actor="operator")
    assert engine_db.execute(
        "SELECT status FROM variants WHERE id = 'V1.1'").fetchone()["status"] == "retired"

    log = engine_db.execute(
        "SELECT * FROM state_log WHERE entity = 'variant' AND entity_id = 'V1.1'").fetchone()
    assert log["actor"] == "operator" and log["reason"] == "copy is off-brand"
    assert json.loads(log["evidence"])["forced"] is True


def test_retire_passes_once_the_data_supports_it(engine_db, seed_graph, insert_message):
    load_seed(engine_db, SEED_FILE)
    engine_db.execute("UPDATE variants SET min_n_gate = 3 WHERE id = 'V1.1'")
    engine_db.commit()
    build_traffic(engine_db, seed_graph, insert_message, "V1.1", 5,
                  replies=stats.MIN_EXPERIMENT_REPLIES)

    status = variants.min_n_gate_status(engine_db, "V1.1")
    assert status["ok"] is True

    variants.retire(engine_db, "V1.1", force=False, reason=None, actor="operator")
    assert engine_db.execute(
        "SELECT status FROM variants WHERE id = 'V1.1'").fetchone()["status"] == "retired"
    log = engine_db.execute(
        "SELECT * FROM state_log WHERE entity = 'variant'").fetchone()
    assert json.loads(log["evidence"])["forced"] is False


def test_declare_winner_is_gated_the_same_way(engine_db, seed_graph, insert_message):
    load_seed(engine_db, SEED_FILE)
    build_traffic(engine_db, seed_graph, insert_message, "V1.1", 4, replies=1)

    with pytest.raises(GateRefused, match="sample too small"):
        variants.declare_winner(engine_db, "V1.1", force=False, reason=None, actor="operator")
    with pytest.raises(ValueError):
        variants.declare_winner(engine_db, "V1.1", force=True, reason="", actor="operator")

    variants.declare_winner(engine_db, "V1.1", force=True, reason="gut call",
                            actor="operator")
    log = engine_db.execute(
        "SELECT * FROM state_log WHERE to_state = 'winner'").fetchone()
    assert log["entity_id"] == "V1.1" and json.loads(log["evidence"])["forced"] is True
    # declaring a winner must not quietly change what the engine does
    assert engine_db.execute(
        "SELECT status FROM variants WHERE id = 'V1.1'").fetchone()["status"] == "active"


def test_declare_winner_rejects_unknown_ids(engine_db):
    load_seed(engine_db, SEED_FILE)
    with pytest.raises(KeyError):
        variants.declare_winner(engine_db, "V9.9", force=True, reason="x", actor="operator")


def test_retire_is_idempotent_and_rejects_unknown_ids(engine_db):
    load_seed(engine_db, SEED_FILE)
    engine_db.execute("UPDATE variants SET status = 'retired' WHERE id = 'V1.1'")
    engine_db.commit()
    variants.retire(engine_db, "V1.1", force=False, reason=None, actor="operator")  # no-op

    with pytest.raises(KeyError):
        variants.retire(engine_db, "V9.9", force=False, reason=None, actor="operator")


def test_a_batch_only_claims_prospects_it_can_actually_reach(engine_db):
    """Found by running the DM pipeline for real, not by a test.

    `assign_batch` used to select on market alone. With DM-first outreach a prospect
    typically has a phone OR a Page, rarely both — so a WhatsApp batch would claim a
    Messenger-only prospect, that prospect would be blocked at compose for a missing
    contact, and because an assignment is one-per-experiment it could never join the
    Messenger batch that could actually reach it. The prospect was silently lost.
    """
    seeded(engine_db)
    make_prospects(engine_db, ["reachable-on-wa.com"], channel="whatsapp")
    make_prospects(engine_db, ["reachable-on-ms.com"], channel="messenger")

    make_batch(engine_db, "B-WA", channel="whatsapp")
    make_batch(engine_db, "B-MS", channel="messenger", seq=1)

    wa = assign_batch(engine_db, EXPERIMENT, "B-WA")
    ms = assign_batch(engine_db, EXPERIMENT, "B-MS")

    assert [row["prospect_key"] for row in wa] == ["reachable-on-wa.com"]
    assert [row["prospect_key"] for row in ms] == ["reachable-on-ms.com"]


def test_a_prospect_with_no_contact_at_all_is_never_assigned(engine_db):
    """Nothing can be sent to it, so claiming it only burns its one assignment."""
    seeded(engine_db)
    make_prospects(engine_db, ["unreachable.com"], channel=None)
    make_batch(engine_db, "B-E", channel="email")

    assert assign_batch(engine_db, EXPERIMENT, "B-E") == []
