"""Per-variant statistics, Wilson intervals, and the honesty gate. §4.4, §12.3.

The diagnostic that started this project told the operator to ship 20 outreach attempts.
At 20 attempts across 5 variants, nothing here is statistically significant, and the
worst thing this engine could do is dress that noise up as a finding and send him
chasing a "winning" variant that is a coin flip. So:

  * every metric has ONE definition, here, and report.py does no arithmetic of its
    own (§10 row (h) — the reviewer greps for stray divisions);
  * bounces leave the delivered denominator but stay visible as their own column;
  * auto-responders and vacation replies are never replies (`is_human = 0`);
  * **every numerator must prove it belongs to its denominator.** A reply row is not
    evidence because it exists; it is evidence when it can be joined to a delivered
    step-1 message that could actually have produced it. `reply_provenance()` is that
    join, and it is the ONLY definition of a counted reply — `variant_table` and
    `report.population_counts` both go through it, so the three commands that print a
    reply count cannot disagree (finding C3);
  * below the gate — 30 delivered per variant AND 5 human replies in the experiment —
    `report_tier()` returns tier 'raw', `show_rates` False and a banner, and
    `variants.retire()` mechanically refuses. Above it, intervals, not point
    estimates; and `leading_variant()` only names a variant whose Wilson interval is
    disjoint from every other one.

The real deliverable at n=20 is `replies_by_variant()`: what people actually wrote.
"""

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

#: Default per-variant delivered floor before a variant decision is allowed (§4.4, Q8).
MIN_N_DEFAULT = 30
#: Experiment-wide human replies required alongside it. Counted as distinct prospects
#: who replied — five emails from one person is one person, not five signals.
MIN_EXPERIMENT_REPLIES = 5

#: A step-1 message counts as an attempt once it has left the engine.
SENT_STATES = ("SENT", "REPLIED", "BOUNCED", "CLOSED")
#: …and it counts as DELIVERED when it left and did not bounce. This is the
#: population every numerator has to belong to.
DELIVERED_STATES = ("SENT", "REPLIED", "CLOSED")
BOOKED_OUTCOMES = ("demo_booked", "call_booked")

#: Gmail's clock and SQLite's clock are not the same clock. A few minutes of slack
#: keeps a genuine same-minute reply from being called impossible; it is nowhere near
#: enough to admit the carryover replies finding C2 is about (those predate the send
#: by weeks or months).
CLOCK_SKEW_GRACE = timedelta(minutes=5)

BANNER_LOW_N = (
    "SAMPLE TOO SMALL — counts, not conclusions. No winner is declarable below "
    f"n={MIN_N_DEFAULT} delivered per variant AND {MIN_EXPERIMENT_REPLIES} total human "
    "replies. The real n=20 signal is the reply text: run dmctl stats --replies."
)
NO_SEPARABLE_SIGNAL = "no separable signal yet"

_FAMILY_RE = re.compile(r"^([A-Za-z]*)(\d*)")


def variant_sort_key(family: str, version: int) -> tuple:
    """Natural variant order: V2 before V10, versions ascending inside a family."""
    match = _FAMILY_RE.match(str(family))
    prefix, digits = match.group(1).upper(), match.group(2)
    return (prefix, int(digits) if digits else 0, str(family), int(version))


# --------------------------------------------------------------------------- #
# arithmetic
# --------------------------------------------------------------------------- #

class ImpossibleRate(ValueError):
    """A numerator escaped its denominator. Never render this as a percentage."""


def rate(k: int, n: int) -> float | None:
    """k/n, or None when there is no denominator. The engine's only division.

    Refuses k > n. Such a rate is not a large number, it is a broken join — the
    numerator counted something the denominator never contained (finding M1, where a
    booking rate printed a clean `200.0% (n=1)`). A percentage above 100 must be
    impossible to *render*, not merely unlikely to occur, so this raises instead of
    returning something a formatter would happily print.
    """
    if n <= 0:
        return None
    if k > n:
        raise ImpossibleRate(
            f"rate(k={k}, n={n}): a numerator cannot exceed its denominator — "
            "the numerator has leaked outside the population it is divided by")
    return k / n


# --------------------------------------------------------------------------- #
# timestamps + provenance: proving a reply belongs to a send
# --------------------------------------------------------------------------- #

def parse_ts(value) -> datetime | None:
    """Parse the four timestamp shapes this system stores, or return None.

    SQLite's `datetime('now')` writes `'YYYY-MM-DD HH:MM:SS'`; Python writes ISO-8601
    with an offset; Gmail hands back epoch milliseconds; an RFC-2822 `Date:` header
    survives as a fallback in `replies.received_at`. Lexicographic comparison across
    those shapes is simply wrong (`' ' < 'T'`), so nothing in this engine compares
    timestamps as strings — everything comes through here first.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed = None
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def send_floor(created_at=None, staged_at=None, sent_at=None) -> datetime | None:
    """The earliest moment a message could possibly have left the building.

    Preference order staged -> sent -> created. `staged_at` is the tightest PROVABLE
    floor: the artifact existed and was queued for a human to release. `sent_at` is
    deliberately NOT preferred — reconcile stamps it when it *observes* the send,
    which is whenever the operator happens to run reconcile (§9 edge 25). Anchoring on an
    observation that arrives late silently discards every genuine event that happened
    inside the lag, which is exactly finding H4.
    """
    for value in (staged_at, sent_at, created_at):
        parsed = parse_ts(value)
        if parsed is not None:
            return parsed
    return None


def reply_could_answer(received_at, created_at=None, staged_at=None,
                       sent_at=None) -> bool:
    """Could this inbound message be an answer to that outbound one?

    Only one thing is actually provable here, and it is enough: an inbound that
    arrived before the outbound message even existed cannot be a reply to it. The operator
    has been cold-emailing this same ICP from this same mailbox since June, so his
    inbox is full of replies to *other* batches; without this check they land in
    dm-engine's variant metrics (finding C2 — a 25-point swing on a 4-per-variant
    pilot). When there is nothing to compare, this returns True: absent evidence is
    not evidence of impossibility.
    """
    received = parse_ts(received_at)
    floor = send_floor(created_at, staged_at, sent_at)
    if received is None or floor is None:
        return True
    return received >= floor - CLOCK_SKEW_GRACE


@dataclass(frozen=True)
class ReplyProvenance:
    """Which human replies are allowed into a numerator, and why the rest are not."""

    #: prospect ids whose human reply is joined to a delivered attempt
    credited: frozenset
    #: prospect_id -> the reason its human reply cannot be counted
    uncredited: dict


def reply_provenance(conn: sqlite3.Connection) -> ReplyProvenance:
    """Join every human reply to the step-1 message it claims to answer.

    A reply counts when BOTH hold:
      * the prospect has a step-1 message in the delivered population (a bounced
        attempt was never read, so nothing can be a reply to it — finding C3), and
      * the reply did not arrive before that message existed (finding C2).

    Everything else is kept, visible, and excluded — `uncredited` is reported by
    `dmctl status` rather than silently dropped.
    """
    placeholders = ", ".join("?" for _ in DELIVERED_STATES)
    rows = conn.execute(
        "SELECT r.prospect_id AS prospect_id, r.received_at AS received_at, "
        "       m.id AS message_id, m.created_at AS created_at, "
        "       m.staged_at AS staged_at, m.sent_at AS sent_at "
        "FROM replies r "
        "LEFT JOIN messages m ON m.prospect_id = r.prospect_id "
        f"  AND m.sequence_step = 1 AND m.state IN ({placeholders}) "
        "WHERE r.is_human = 1 ORDER BY r.id",
        DELIVERED_STATES,
    ).fetchall()

    credited: set[int] = set()
    uncredited: dict[int, str] = {}
    for row in rows:
        prospect_id = row["prospect_id"]
        if prospect_id in credited:
            continue
        if row["message_id"] is None:
            uncredited.setdefault(
                prospect_id,
                "no delivered step-1 message — the attempt bounced or never left, "
                "so nothing was there to reply to")
            continue
        if not reply_could_answer(row["received_at"], row["created_at"],
                                  row["staged_at"], row["sent_at"]):
            uncredited.setdefault(
                prospect_id,
                f"arrived {row['received_at']} — before the message existed "
                "(carryover from an earlier, non-engine batch)")
            continue
        credited.add(prospect_id)
        uncredited.pop(prospect_id, None)
    return ReplyProvenance(frozenset(credited), uncredited)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. Closed form, stdlib only.

    Chosen over the normal approximation because at these sample sizes the normal
    interval is simply wrong: it produces negative lower bounds at k=0 and collapses
    to zero width at k=n, which is exactly the regime this engine reports in.
    Returns (0.0, 1.0) for n=0 — total ignorance, stated as total ignorance.
    """
    if n < 0 or k < 0:
        raise ImpossibleRate(f"wilson(k={k}, n={n}): counts must not be negative")
    if k > n:
        raise ImpossibleRate(f"wilson(k={k}, n={n}): successes cannot exceed trials")
    if n == 0:
        return (0.0, 1.0)

    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# --------------------------------------------------------------------------- #
# per-variant table
# --------------------------------------------------------------------------- #

@dataclass
class VariantStats:
    variant_id: str
    sends: int
    delivered: int
    bounced: int
    replies: int
    booked: int
    reply_rate: float | None
    booking_rate: float | None
    ci: tuple[float, float] | None
    #: only populated by variant_table(by_family=True): {variant_id: sends}
    family_split: dict[str, int] | None = None

    @property
    def reply_rate_incl_bounces(self) -> float | None:
        """The other denominator, printed alongside so the choice stays visible."""
        return rate(self.replies, self.sends)


def _experiment_messages(conn: sqlite3.Connection, experiment_id: str) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in SENT_STATES)
    return conn.execute(
        "SELECT m.variant_id, m.prospect_id, m.state "
        "FROM messages m JOIN batches b ON b.id = m.batch_id "
        f"WHERE b.experiment_id = ? AND m.sequence_step = 1 AND m.state IN ({placeholders})",
        (experiment_id, *SENT_STATES),
    ).fetchall()


def credited_reply_prospects(conn: sqlite3.Connection) -> frozenset:
    """Prospects whose human reply belongs to a delivered attempt. THE numerator set.

    `report.population_counts` uses this same set, which is what stops `dmctl stats`,
    `dmctl status` and `dmctl metrics-emit` reporting three different reply counts off
    one database (finding C3).
    """
    return reply_provenance(conn).credited


def booked_prospects(conn: sqlite3.Connection) -> set[int]:
    placeholders = ", ".join("?" for _ in BOOKED_OUTCOMES)
    return {
        row["prospect_id"]
        for row in conn.execute(
            f"SELECT prospect_id FROM outcomes WHERE outcome IN ({placeholders})",
            BOOKED_OUTCOMES,
        )
    }


def experiment_human_replies(conn: sqlite3.Connection, experiment_id: str) -> int:
    """Distinct prospects in this experiment whose human reply counts (§4.4 gate half).

    Credited replies only: a reply that arrived before we ever wrote, or one against a
    prospect whose only attempt bounced, must not help open the gate that lets a
    winner be declared.
    """
    credited = credited_reply_prospects(conn)
    if not credited:
        return 0
    assigned = {
        row["prospect_id"] for row in conn.execute(
            "SELECT prospect_id FROM assignments WHERE experiment_id = ?",
            (experiment_id,))
    }
    return len(assigned & credited)


def _variant_rows(conn: sqlite3.Connection, experiment_id: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, family, version FROM variants WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchall()
    return sorted(rows, key=lambda r: variant_sort_key(r["family"], r["version"]))


def _build(variant_id: str, messages: list[sqlite3.Row], replied, booked_ids,
           family_split: dict[str, int] | None = None) -> VariantStats:
    sends = len(messages)
    bounced = sum(1 for m in messages if m["state"] == "BOUNCED")
    delivered = sends - bounced
    # Both numerators are intersected with the DELIVERED prospects of this variant,
    # never with every prospect it ever touched. That is what makes replies <= delivered
    # and booked <= delivered true by construction rather than by luck — the shape of
    # finding C3, where a bounced prospect's reply produced wilson(k=2, n=1).
    delivered_prospects = {m["prospect_id"] for m in messages if m["state"] != "BOUNCED"}
    replies = len(delivered_prospects & replied)
    booked = len(delivered_prospects & booked_ids)
    return VariantStats(
        variant_id=variant_id,
        sends=sends,
        delivered=delivered,
        bounced=bounced,
        replies=replies,
        booked=booked,
        reply_rate=rate(replies, delivered),
        booking_rate=rate(booked, delivered),
        ci=wilson(replies, delivered) if delivered > 0 else None,
        family_split=family_split,
    )


def variant_table(conn: sqlite3.Connection, experiment_id: str,
                  by_family: bool = False) -> list[VariantStats]:
    """One row per variant (or per family), including variants with no data yet.

    A variant sitting at zero belongs in the table: an empty cell is information,
    and hiding it is how a 4-variant comparison quietly becomes a 3-variant one.
    """
    messages = _experiment_messages(conn, experiment_id)
    replied = credited_reply_prospects(conn)
    booked_ids = booked_prospects(conn)

    grouped: dict[str, list[sqlite3.Row]] = {}
    for message in messages:
        grouped.setdefault(message["variant_id"], []).append(message)

    variant_rows = _variant_rows(conn, experiment_id)
    known = {row["id"] for row in variant_rows}
    orphans = sorted(set(grouped) - known)          # defensive: cross-experiment rows

    if not by_family:
        table = [_build(row["id"], grouped.get(row["id"], []), replied, booked_ids)
                 for row in variant_rows]
        table.extend(_build(vid, grouped[vid], replied, booked_ids) for vid in orphans)
        return table

    families: dict[str, list[sqlite3.Row]] = {}
    splits: dict[str, dict[str, int]] = {}
    for row in variant_rows:
        family = row["family"]
        member = grouped.get(row["id"], [])
        families.setdefault(family, []).extend(member)
        splits.setdefault(family, {})[row["id"]] = len(member)
    return [_build(family, members, replied, booked_ids, splits[family])
            for family, members in families.items()]


def replies_by_variant(conn: sqlite3.Connection, experiment_id: str) -> dict[str, list[dict]]:
    """Verbatim human reply snippets grouped by variant — the honest n=20 output.

    Every variant gets a key, including the ones nobody answered; a silent variant
    is a result too.
    """
    grouped: dict[str, list[dict]] = {row["id"]: [] for row in _variant_rows(conn, experiment_id)}
    credited = credited_reply_prospects(conn)
    placeholders = ", ".join("?" for _ in SENT_STATES)
    rows = conn.execute(
        "SELECT m.variant_id, r.prospect_id, p.company_name, c.address, r.classification, "
        "       r.received_at, r.snippet, r.classified_by "
        "FROM replies r "
        "JOIN prospects p ON p.id = r.prospect_id "
        "JOIN messages m ON m.prospect_id = r.prospect_id AND m.sequence_step = 1 "
        "JOIN batches b ON b.id = m.batch_id "
        "JOIN contacts c ON c.id = m.contact_id "
        f"WHERE b.experiment_id = ? AND r.is_human = 1 AND m.state IN ({placeholders}) "
        "ORDER BY r.received_at, r.id",
        (experiment_id, *SENT_STATES),
    ).fetchall()
    for row in rows:
        # the same credited set the counts use: a variant's verbatim column and its
        # reply column must never describe different populations
        if row["prospect_id"] not in credited:
            continue
        grouped.setdefault(row["variant_id"], []).append({
            "prospect_id": row["prospect_id"],
            "company_name": row["company_name"],
            "address": row["address"],
            "classification": row["classification"],
            "received_at": row["received_at"],
            "snippet": row["snippet"],
            "classified_by": row["classified_by"],
        })
    return grouped


# --------------------------------------------------------------------------- #
# the honesty gate
# --------------------------------------------------------------------------- #

def report_tier(conn: sqlite3.Connection, experiment_id: str,
                table: list[VariantStats] | None = None) -> dict:
    """What this data is allowed to say. §4.4's reporting tiers, decided in one place.

    `n` is the SMALLEST delivered count among variants that have any data, because a
    comparison is only as strong as its weakest arm. Fewer than two variants with
    data means there is nothing to compare and the tier stays 'raw'.
    """
    table = variant_table(conn, experiment_id) if table is None else table
    with_data = [vs for vs in table if vs.sends > 0]
    human_replies = experiment_human_replies(conn, experiment_id)
    min_delivered = min((vs.delivered for vs in with_data), default=0) if len(with_data) >= 2 else 0

    gate_ok = min_delivered >= MIN_N_DEFAULT and human_replies >= MIN_EXPERIMENT_REPLIES
    if not gate_ok:
        tier = "raw"
    elif min_delivered < 100:
        tier = "wilson"
    elif min_delivered < 500:
        tier = "segment"
    else:
        tier = "deep"

    return {
        "tier": tier,
        "min_delivered": min_delivered,
        "human_replies": human_replies,
        "required_delivered": MIN_N_DEFAULT,
        "required_replies": MIN_EXPERIMENT_REPLIES,
        "variants_with_data": len(with_data),
        "show_rates": tier != "raw",
        "winner_declarable": tier != "raw",
        "banner": BANNER_LOW_N if tier == "raw" else None,
    }


def leading_variant(table: list[VariantStats]) -> str | None:
    """The one variant whose Wilson interval clears EVERY other one, or None.

    §4.4 makes disjoint intervals a necessary condition; this requires it against all
    rivals, not just one, because "beat somebody" across five arms is a multiple
    comparison and will happen by chance long before it means anything.
    """
    candidates = [vs for vs in table if vs.ci is not None and vs.delivered > 0]
    if len(candidates) < 2:
        return None
    for candidate in candidates:
        others = [vs for vs in candidates if vs.variant_id != candidate.variant_id]
        if all(candidate.ci[0] > other.ci[1] for other in others):
            return candidate.variant_id
    return None


def summary(conn: sqlite3.Connection, experiment_id: str,
            by_family: bool = False) -> dict:
    """Everything report.py needs, already gated. No arithmetic left for the caller.

    The gate is ALWAYS evaluated on the per-variant arms, even when the table being
    rendered is aggregated by family. Pooling V1.1 and V1.2 into one 40-delivered row
    does not mean either piece of copy reached 40, and finding H2 is precisely that
    `--by-family` used to turn "SAMPLE TOO SMALL" into a `leading` label on the same
    data. The flag changes the presentation; it cannot change what the data may say.
    """
    table = variant_table(conn, experiment_id, by_family=by_family)
    gate_table = variant_table(conn, experiment_id) if by_family else table
    tier = report_tier(conn, experiment_id, gate_table)
    leader = leading_variant(table) if tier["winner_declarable"] else None
    return {
        "experiment_id": experiment_id,
        "table": table,
        "tier": tier,
        "leader": leader,
        "separation_note": None if leader else NO_SEPARABLE_SIGNAL,
    }
