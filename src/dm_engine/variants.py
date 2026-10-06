"""Variant registry + deterministic assignment. Contract: PLAN.md §12.3, algo §4.3.

`sorted-hash-rr-v1` is the scientific integrity of the whole engine. Every per-variant
number the system reports is only meaningful if the mapping from prospect to variant
is a pure function of recorded inputs — otherwise "V3 is winning" is unfalsifiable.
So assignment is:

    assign_hash      = sha256(experiment_id + '|' + prospect_key)
    variant_set_hash = sha256('|'.join(active variant ids, in canonical order))
    order            = batch members sorted by assign_hash
    variant          = variants[(batch_seq + rank) % len(variants)]

Both hashes are stored on every row, so any later run can recompute the mapping from
the database alone. Re-running `assign_batch` recomputes and COMPARES: identical is a
no-op, any difference is `AssignmentDrift` and nothing is written. There is no code
path that reassigns a prospect — drift means either the inputs were tampered with or
the algorithm changed, and both of those need a human, not a silent overwrite.

Batch membership is frozen at first assignment. That is what makes re-runs stable:
later imports into the same market do not retroactively change who was in the batch,
so the recompute sees exactly the inputs the original run saw.

Variants are immutable once used (§4.1). `load_seed` enforces it: a changed variant
that any message or assignment already references becomes a NEW version and the old
row is retired, so statistics always attach to the exact copy that was sent.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from . import stats
from .stats import variant_sort_key

#: Recorded in `assignments.algo`. Bump the suffix if the mapping rule ever changes —
#: old rows keep their algo name and stay recomputable under the old rule.
ALGO = "sorted-hash-rr-v1"


class SeedError(ValueError):
    """The seed file is malformed or contradicts the database."""


class AssignmentDrift(RuntimeError):
    """A recompute disagreed with what is stored. Refuse rather than reassign."""


class GateRefused(RuntimeError):
    """A min-n gate blocked a decision that the data cannot support (§4.4)."""


# --------------------------------------------------------------------------- #
# hashes and ordering
# --------------------------------------------------------------------------- #

def assign_hash(experiment_id: str, prospect_key: str) -> str:
    """sha256 of 'experiment_id|prospect_key'. Verifiable by hand from the row."""
    return hashlib.sha256(f"{experiment_id}|{prospect_key}".encode()).hexdigest()


def variant_set_hash(variant_ids: list[str]) -> str:
    """sha256 of the ordered active variant ids joined by '|'."""
    return hashlib.sha256("|".join(variant_ids).encode()).hexdigest()


def active_variant_ids(conn: sqlite3.Connection, experiment_id: str) -> list[str]:
    """Active variant ids for the experiment, in the canonical order the hash uses.

    Ordering is `stats.variant_sort_key` — shared with the reporting table so the
    hashed set and the printed table can never disagree about what "the variants" are.
    """
    rows = conn.execute(
        "SELECT id, family, version FROM variants "
        "WHERE experiment_id = ? AND status = 'active'",
        (experiment_id,),
    ).fetchall()
    rows = sorted(rows, key=lambda r: variant_sort_key(r["family"], r["version"]))
    return [r["id"] for r in rows]


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #

def _canonical_spec(spec) -> str:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise SeedError(f"variant spec is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise SeedError("variant spec must be a JSON object")
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def _log_variant(conn, variant_id, from_state, to_state, actor, reason, evidence=None):
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason, evidence) "
        "VALUES ('variant', ?, ?, ?, ?, ?, ?)",
        (variant_id, from_state, to_state, actor, reason, evidence),
    )


def load_seed(conn: sqlite3.Connection, json_path: Path) -> int:
    """Idempotent upsert of the experiment + its variants. Returns rows written.

    Re-running with an unchanged file writes nothing and returns 0. A variant whose
    copy guidance changed is updated in place ONLY while nothing references it; once
    a message or an assignment points at it, the change lands as a new version and
    the old row is retired (§4.1 — statistics must never be contaminated by an edit).
    """
    path = Path(json_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SeedError(f"cannot read seed file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SeedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "experiment" not in data or "variants" not in data:
        raise SeedError(f"{path} must be an object with 'experiment' and 'variants'")

    experiment = data["experiment"]
    experiment_id = str(experiment.get("id") or "").strip()
    if not experiment_id:
        raise SeedError("experiment.id is required")

    written = 0
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO experiments (id, name, hypothesis) VALUES (?, ?, ?)",
            (experiment_id, str(experiment.get("name") or experiment_id),
             experiment.get("hypothesis")),
        )
        for index, definition in enumerate(data["variants"]):
            written += _upsert_variant(conn, experiment_id, definition, index)
    return written


def _upsert_variant(conn, experiment_id: str, definition, index: int) -> int:
    if not isinstance(definition, dict):
        raise SeedError(f"variants[{index}] is not an object")
    family = str(definition.get("family") or "").strip()
    name = str(definition.get("name") or "").strip()
    angle = str(definition.get("angle") or "").strip()
    if not (family and name and angle):
        raise SeedError(f"variants[{index}] needs family, name and angle")
    version = int(definition.get("version") or 1)
    min_n_gate = int(definition.get("min_n_gate") or stats.MIN_N_DEFAULT)
    spec = _canonical_spec(definition.get("spec"))
    variant_id = str(definition.get("id") or f"{family}.{version}")

    family_rows = conn.execute(
        "SELECT * FROM variants WHERE experiment_id = ? AND family = ?",
        (experiment_id, family),
    ).fetchall()
    # Already present with exactly this content, under any version — nothing to do.
    for row in family_rows:
        if (row["name"] == name and row["angle"] == angle
                and _canonical_spec(row["spec"]) == spec
                and row["min_n_gate"] == min_n_gate):
            return 0

    existing = next((r for r in family_rows if r["id"] == variant_id), None)
    if existing is None:
        conn.execute(
            "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec, "
            "min_n_gate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (variant_id, family, version, experiment_id, name, angle, spec, min_n_gate),
        )
        return 1

    if _is_referenced(conn, variant_id):
        next_version = max(int(r["version"]) for r in family_rows) + 1
        new_id = f"{family}.{next_version}"
        conn.execute(
            "INSERT INTO variants (id, family, version, experiment_id, name, angle, spec, "
            "min_n_gate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, family, next_version, experiment_id, name, angle, spec, min_n_gate),
        )
        conn.execute("UPDATE variants SET status = 'retired' WHERE id = ?", (variant_id,))
        _log_variant(conn, variant_id, "active", "retired", "engine",
                     f"superseded by {new_id}: variant copy changed after use",
                     json.dumps({"successor": new_id}))
        return 1

    conn.execute(
        "UPDATE variants SET name = ?, angle = ?, spec = ?, min_n_gate = ? WHERE id = ?",
        (name, angle, spec, min_n_gate, variant_id),
    )
    return 1


def _is_referenced(conn, variant_id: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM messages WHERE variant_id = ?) "
        "    OR EXISTS (SELECT 1 FROM assignments WHERE variant_id = ?)",
        (variant_id, variant_id),
    ).fetchone()
    return bool(row[0])


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #

def new_batch(conn: sqlite3.Connection, batch_id: str, experiment_id: str,
              market: str, channel: str, personalization: str = "unique") -> int:
    """Open a batch and allocate its `batch_seq`. Returns the allocated seq.

    This lives here rather than in the CLI because `batch_seq` is not bookkeeping —
    it is the round-robin offset in §4.3, an input to `plan_assignment` and therefore
    to every variant a prospect ever gets. Allocating it next to the algorithm that
    consumes it keeps the two from drifting apart.

    Seq is `max(batch_seq) + 1` within the experiment, allocated and inserted in one
    transaction so two concurrent `new-batch` runs cannot hand out the same offset
    (which would make two batches round-robin identically and quietly bias the split).
    """
    if not str(batch_id or "").strip():
        raise ValueError("batch_id is required")
    with conn:
        if conn.execute("SELECT 1 FROM experiments WHERE id = ?",
                        (experiment_id,)).fetchone() is None:
            raise KeyError(
                f"no experiment {experiment_id!r} — run `dmctl init` to load the seed")
        if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
            raise ValueError(
                f"batch {batch_id!r} already exists. Batch membership is frozen at "
                "first assignment (§4.3), so reusing an id would silently mix two "
                "runs — pick a new id.")
        seq = conn.execute(
            "SELECT COALESCE(max(batch_seq), 0) + 1 FROM batches WHERE experiment_id = ?",
            (experiment_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO batches (id, experiment_id, market, channel, personalization, "
            "batch_seq) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, experiment_id, market, channel, personalization, seq))
    return int(seq)


# --------------------------------------------------------------------------- #
# assignment
# --------------------------------------------------------------------------- #

def plan_assignment(experiment_id: str, batch_id: str, batch_seq: int,
                    variant_ids: list[str], members: list[tuple[int, str]],
                    limit: int | None = None) -> list[dict]:
    """The pure function at the centre of §4.3. No database, no clock, no randomness.

    `members` is [(prospect_id, prospect_key)]. Returns one dict per assigned member
    in assignment order. Given the same arguments it returns the same list, forever —
    that property is what `assign_batch` verifies against stored rows.
    """
    if not variant_ids:
        raise ValueError("cannot assign: the experiment has no active variants")
    ranked = sorted(
        (
            {
                "prospect_id": prospect_id,
                "prospect_key": prospect_key,
                "assign_hash": assign_hash(experiment_id, prospect_key),
            }
            for prospect_id, prospect_key in members
        ),
        key=lambda row: (row["assign_hash"], row["prospect_key"]),
    )
    if limit is not None:
        ranked = ranked[:limit]

    set_hash = variant_set_hash(variant_ids)
    offset = int(batch_seq) % len(variant_ids)
    for rank, row in enumerate(ranked):
        row["variant_id"] = variant_ids[(offset + rank) % len(variant_ids)]
        row["variant_set_hash"] = set_hash
        row["algo"] = ALGO
        row["experiment_id"] = experiment_id
        row["batch_id"] = batch_id
        row["rank"] = rank
    return ranked


def assign_batch(conn: sqlite3.Connection, experiment_id: str, batch_id: str,
                 *, limit: int | None = None) -> list[dict]:
    """Assign every member of the batch a variant, or verify an existing assignment.

    First run: members are the QUALIFIED prospects in the batch's market that no
    assignment in this experiment claims yet (`limit` caps the batch, taking the
    lowest assign_hashes so the cut is itself reproducible). Later runs: membership
    is whatever the batch already holds, `limit` is ignored, and the run only
    verifies — it either confirms every row or raises AssignmentDrift.
    """
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError(f"no batch {batch_id!r}")
    if batch["experiment_id"] != experiment_id:
        raise ValueError(
            f"batch {batch_id!r} belongs to experiment {batch['experiment_id']!r}, "
            f"not {experiment_id!r}"
        )

    variant_ids = active_variant_ids(conn, experiment_id)
    if not variant_ids:
        raise ValueError(f"experiment {experiment_id!r} has no active variants")

    existing = conn.execute(
        "SELECT a.*, p.prospect_key FROM assignments a "
        "JOIN prospects p ON p.id = a.prospect_id "
        "WHERE a.batch_id = ?",
        (batch_id,),
    ).fetchall()

    if existing:
        members = [(r["prospect_id"], r["prospect_key"]) for r in existing]
        planned = plan_assignment(experiment_id, batch_id, batch["batch_seq"],
                                  variant_ids, members)
        _verify_no_drift(existing, planned, batch_id)
        for row in planned:
            row["created"] = False
        return planned

    # The channel clause matters most for DM-first outreach, where a prospect
    # typically has a phone OR a Page, rarely both. Without it, a batch claims every
    # QUALIFIED prospect in the market — a prospect reachable only on Messenger gets
    # consumed by a WhatsApp batch, is blocked at compose for a missing contact, and
    # (because an assignment is one-per-experiment) can never join the Messenger
    # batch that could actually reach it. It silently costs a prospect.
    candidates = conn.execute(
        "SELECT id, prospect_key FROM prospects "
        "WHERE state = 'QUALIFIED' AND market = ? "
        "  AND id NOT IN (SELECT prospect_id FROM assignments WHERE experiment_id = ?) "
        "  AND EXISTS (SELECT 1 FROM contacts c "
        "              WHERE c.prospect_id = prospects.id AND c.channel = ?)",
        (batch["market"], experiment_id, batch["channel"]),
    ).fetchall()
    members = [(r["id"], r["prospect_key"]) for r in candidates]
    planned = plan_assignment(experiment_id, batch_id, batch["batch_seq"],
                              variant_ids, members, limit=limit)

    with conn:
        for row in planned:
            conn.execute(
                "INSERT INTO assignments (experiment_id, batch_id, prospect_id, variant_id, "
                "algo, assign_hash, variant_set_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, batch_id, row["prospect_id"], row["variant_id"],
                 row["algo"], row["assign_hash"], row["variant_set_hash"]),
            )
            row["created"] = True
    return planned


def _verify_no_drift(existing, planned, batch_id: str) -> None:
    by_prospect = {row["prospect_id"]: row for row in planned}
    differences: list[str] = []
    for row in existing:
        computed = by_prospect.get(row["prospect_id"])
        if computed is None:                       # cannot happen; belt for a bad query
            differences.append(f"prospect {row['prospect_id']} is stored but not planned")
            continue
        for column in ("variant_id", "assign_hash", "variant_set_hash", "algo"):
            if row[column] != computed[column]:
                differences.append(
                    f"prospect {row['prospect_id']} ({row['prospect_key']}): "
                    f"{column} stored={row[column]!r} recomputed={computed[column]!r}"
                )
    if differences:
        raise AssignmentDrift(
            f"assignment drift in batch {batch_id!r} — refusing to reassign. "
            "Stored rows no longer match a recompute over the same inputs, so either "
            "a row was edited or the active variant set changed. Open a NEW batch "
            "instead of rewriting history.\n  " + "\n  ".join(differences)
        )


def get_assignment(conn: sqlite3.Connection, experiment_id: str,
                   prospect_id: int) -> sqlite3.Row | None:
    """The one variant this prospect holds in this experiment, forever (§4.3)."""
    return conn.execute(
        "SELECT * FROM assignments WHERE experiment_id = ? AND prospect_id = ?",
        (experiment_id, prospect_id),
    ).fetchone()


# --------------------------------------------------------------------------- #
# the min-n gate (§4.4)
# --------------------------------------------------------------------------- #

def min_n_gate_status(conn: sqlite3.Connection, variant_id: str) -> dict:
    """Can this variant's performance carry a decision yet? Numbers, then a verdict."""
    row = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if row is None:
        raise KeyError(f"no variant {variant_id!r}")
    experiment_id = row["experiment_id"]

    table = {vs.variant_id: vs for vs in stats.variant_table(conn, experiment_id)}
    delivered = table[variant_id].delivered if variant_id in table else 0
    human_replies = stats.experiment_human_replies(conn, experiment_id)
    min_n = int(row["min_n_gate"])
    ok = delivered >= min_n and human_replies >= stats.MIN_EXPERIMENT_REPLIES

    return {
        "variant_id": variant_id,
        "experiment_id": experiment_id,
        "delivered": delivered,
        "min_n": min_n,
        "human_replies": human_replies,
        "required_replies": stats.MIN_EXPERIMENT_REPLIES,
        "ok": ok,
        "detail": (
            f"{variant_id}: delivered={delivered} (needs {min_n}), "
            f"experiment human replies={human_replies} "
            f"(needs {stats.MIN_EXPERIMENT_REPLIES})"
        ),
    }


def _check_gate(conn, variant_id: str, verb: str, force: bool, reason: str | None) -> dict:
    """Shared refusal for every decision the sample size cannot support (§4.4)."""
    status = min_n_gate_status(conn, variant_id)
    if status["ok"]:
        return status
    if not force:
        raise GateRefused(
            f"refusing to {verb} {variant_id}: sample too small to call it. "
            f"{status['detail']}. A decision on this much data is a coin flip dressed "
            f"as a finding — pass force=True with a reason if you are overriding "
            f"deliberately."
        )
    if not str(reason or "").strip():
        raise ValueError(f"forcing a {verb} below the min-n gate requires a reason")
    return status


def declare_winner(conn: sqlite3.Connection, variant_id: str, force: bool = False,
                   reason: str | None = None, actor: str = "operator") -> None:
    """Record a winning variant. Same gate as retire — and it is only ever a record.

    There is no 'winner' status on the variants table on purpose: declaring a winner
    changes nothing the engine does, it only writes an auditable claim into
    `state_log`. Nothing downstream reads it, so a premature call cannot quietly
    start steering assignment.
    """
    row = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if row is None:
        raise KeyError(f"no variant {variant_id!r}")
    status = _check_gate(conn, variant_id, "declare a winner for", force, reason)
    with conn:
        _log_variant(
            conn, variant_id, row["status"], "winner", actor,
            reason or "min-n gate satisfied",
            json.dumps({**status, "forced": bool(force and not status["ok"])}),
        )


def retire(conn: sqlite3.Connection, variant_id: str, force: bool = False,
           reason: str | None = None, actor: str = "operator") -> None:
    """Retire a variant. REFUSES below the min-n gate unless forced (§4.4).

    The override exists — the operator may have a reason the data cannot see — but it
    demands an explicit reason and leaves a permanent `state_log` row, so a variant
    killed on a hunch at n=4 is visible forever rather than looking like a finding.
    """
    row = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if row is None:
        raise KeyError(f"no variant {variant_id!r}")
    if row["status"] == "retired":
        return

    status = _check_gate(conn, variant_id, "retire", force, reason)

    with conn:
        conn.execute("UPDATE variants SET status = 'retired' WHERE id = ?", (variant_id,))
        _log_variant(
            conn, variant_id, "active", "retired", actor,
            reason or "min-n gate satisfied",
            json.dumps({**status, "forced": bool(force and not status["ok"])}),
        )
