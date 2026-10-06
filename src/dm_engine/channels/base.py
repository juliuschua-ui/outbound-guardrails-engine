"""Channel adapter protocol + the staging gates every channel shares.

Contract: PLAN.md §12.5. Behaviour: §6, §9 (Approval/staging, Caps/clock/host).

An adapter's `stage()` turns an APPROVED message into an artifact a human can
release: a Gmail draft, or a copy-ready LinkedIn card. That is the last thing the
engine does. There is no send step here or anywhere under `src/` — see
`tests/test_channels.py::test_src_tree_contains_no_send_capability`.

The cap-under-concurrency design (PLAN.md §10 row (c))
------------------------------------------------------
`locks.hold(cfg, 'stage')` wraps the whole batch — taken exactly ONCE per run,
because `hold()` is not reentrant. Inside it, each message gets its own
`BEGIN IMMEDIATE` transaction that:

    1. counts today's `stage_events` for the channel,
    2. inserts this message's `stage_events` row (reserving the slot),
    3. creates the artifact,
    4. transitions APPROVED -> STAGED,
    5. COMMITs.

The count and the insert are therefore atomic, and a failure at step 3 rolls the
reservation back so the message stays APPROVED and retryable (edge 22). Two
concurrent runs cannot jointly exceed the cap — proved with two real processes in
`tests/test_channels.py::test_two_processes_racing_the_cap_stay_under_it`.
"""

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..db import PHX_UTC_OFFSET

__all__ = [
    "ChannelConstraints",
    "StageResult",
    "ChannelAdapter",
    "get_adapter",
    "STAGE_LOCK",
    "CAP_TEMPLATE",
    "CAP_UNIQUE",
    "CHANNEL_CAPS",
    "channel_caps",
    "VERIFY_STALE_DAYS",
    "PERSON_COOLDOWN_DAYS",
    "ROLLING_WINDOW_HOURS",
    "TEMPLATE_SIMILARITY",
    "effective_personalization",
    "cap_refusal",
    "shared_ledger_warning",
    "require_suppression_sync",
    "stagger_plan",
]

#: One lock name for every channel: the daily cap is a shared resource.
STAGE_LOCK = "stage"

#: PLAN.md §9.30 — template-body batches cap at 20/Phoenix-day, fully unique
#: bodies at 30. A day that has staged ANY template-class message takes 20.
CAP_TEMPLATE = 20
CAP_UNIQUE = 30

#: Per-channel override of (template cap, unique cap). Absent = the email numbers.
#:
#: The 20/30 pair is an EMAIL deliverability rule: it protects a Gmail sender
#: reputation that recovers, slowly, from a bad week. The two DM channels fail
#: differently and much harder.
#:
#: WhatsApp bans are spam-DETECTION driven, not policy driven — volume, velocity,
#: near-identical text, and a low reply rate on a number with no sending history are
#: what flip an account into review, and the outcome is the loss of the number
#: itself, which is also the number on the operator's vans and business cards. There is no
#: appeal worth planning around. 8 template / 12 unique keeps daily volume roughly a
#: third of email's and well under the thresholds a fresh number trips; it is also
#: about as many as one human can genuinely hand-send with a personal opener before
#: the copy starts repeating, which is exactly the signal being avoided.
#:
#: Messenger's failure mode is softer — Page-level rate limiting and the "message
#: requests" folder rather than a number ban — so it takes a middle cap.
#:
#: `effective_personalization` applies on these channels too: a batch of
#: near-identical DMs takes the template cap however it declared itself, because
#: near-identical text is precisely what the detection looks for.
CHANNEL_CAPS: dict[str, tuple[int, int]] = {
    "whatsapp": (8, 12),
    "messenger": (12, 18),
}

#: The Phoenix calendar day resets at midnight, so 20 staged at 23:58 and 20 more
#: at 00:01 is 40 artifacts three minutes apart — inside the rules on paper and a
#: cadence violation the moment the queue is released. The cap is therefore also
#: enforced over a rolling window.
ROLLING_WINDOW_HOURS = 24

#: Median pairwise body similarity at or above this means the batch is a template
#: batch whatever it declared. Nothing else in the engine measured this, so a
#: template batch labelled `unique` simply took the 30/day cap.
TEMPLATE_SIMILARITY = 0.75

#: the operator's cadence rule: 3-5 minutes between sends, 10 per window.
STAGGER_MINUTES = 4
STAGGER_WINDOW_SIZE = 10

#: Verdicts older than this get a warning at stage time (edge 11) — domains die
#: between prospecting and sending. A warning, not a refusal.
VERIFY_STALE_DAYS = 30

#: The same human at a second company is still the same human (edge 2).
PERSON_COOLDOWN_DAYS = 90

_MST = timezone(PHX_UTC_OFFSET)     # Phoenix has no DST; the operator's operating clock


@dataclass(frozen=True)
class ChannelConstraints:
    max_words: int | None
    max_sentences: int | None
    max_chars: int | None
    has_subject: bool


@dataclass
class StageResult:
    """Every message is accounted for: staged + skipped + failed == requested.

    skipped  — a gate refused it, deliberately and reversibly (state unchanged
               unless the reason says otherwise, e.g. an already-sent CANCEL).
    failed   — the artifact could not be created; the message stays APPROVED and
               a re-run retries it.
    warnings — advisory, does not block (stale verification, adopted draft).
    """
    staged: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    auth_expired: bool = False

    @property
    def accounted(self) -> int:
        return self.staged + len(self.skipped) + len(self.failed)


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str

    def constraints(self) -> ChannelConstraints: ...

    def stage(self, conn, cfg, message_ids: list[int], dry_run: bool = False,
              service_factory=None) -> StageResult: ...

    def reconcile(self, conn, cfg, service_factory=None) -> dict: ...


def get_adapter(channel: str) -> ChannelAdapter:
    """Adapter for a channel name. Raises ValueError for anything unsupported."""
    from . import email_gmail, linkedin_manual, messenger_manual, whatsapp_manual

    if channel == "email":
        return email_gmail.EmailGmailAdapter()
    if channel == "linkedin":
        return linkedin_manual.LinkedInManualAdapter()
    if channel == "whatsapp":
        return whatsapp_manual.WhatsAppManualAdapter()
    if channel == "messenger":
        return messenger_manual.MessengerManualAdapter()
    if channel == "sms":
        raise ValueError(
            "sms is not a channel of this engine. TCPA requires documented express "
            "written consent before any commercial SMS; cold SMS to scraped numbers "
            "is legal exposure, not a growth channel (PLAN.md §6.3)."
        )
    if channel == "instagram":
        raise ValueError(
            "instagram has no adapter in v1 (PLAN.md §6.3). The schema supports it; "
            "adding it means cloning linkedin_manual.py as another human-send queue."
        )
    raise ValueError(f"unknown channel {channel!r}; supported: email, linkedin, "
                     f"whatsapp, messenger")


# --------------------------------------------------------------------------- #
# shared staging queries + gates
# --------------------------------------------------------------------------- #

MESSAGE_SELECT = """
SELECT m.id, m.state, m.channel, m.subject, m.body, m.sequence_step, m.variant_id,
       m.batch_id, m.prospect_id, m.contact_id, m.gmail_draft_id, m.staged_at,
       m.sent_at, m.gmail_thread_id,
       c.address, c.address_source, c.verify_verdict, c.verify_reason,
       c.verified_at, c.person_key,
       p.prospect_key, p.company_name, p.company_casual, p.market, p.timezone,
       p.owner_first_name, p.website_url,
       b.personalization
FROM messages m
JOIN contacts  c ON c.id = m.contact_id
JOIN prospects p ON p.id = m.prospect_id
JOIN batches   b ON b.id = m.batch_id
"""


def fetch_message(conn, message_id: int):
    """One staging-shaped row, or None when the id does not exist."""
    return conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()


def staged_today(conn, phx_day: str, channel: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM stage_events WHERE phx_date = ? AND channel = ?",
        (phx_day, channel),
    ).fetchone()
    return row[0]


def staged_in_window(conn, channel: str, hours: int = ROLLING_WINDOW_HOURS) -> int:
    """Artifacts staged in the trailing `hours`, ignoring the calendar boundary."""
    row = conn.execute(
        "SELECT count(*) FROM stage_events "
        "WHERE channel = ? AND created_at > datetime('now', ?)",
        (channel, f"-{int(hours)} hours"),
    ).fetchone()
    return row[0]


def channel_caps(channel: str) -> tuple[int, int]:
    """(template cap, unique cap) for a channel. See CHANNEL_CAPS."""
    return CHANNEL_CAPS.get(channel, (CAP_TEMPLATE, CAP_UNIQUE))


def daily_cap(conn, phx_day: str, channel: str, personalization: str) -> int:
    """Cap for this channel on this Phoenix day (PLAN.md §9.30).

    Mixed days take the lower bound: one template-class artifact already staged
    today pins the whole day to the template cap, and a template-class candidate
    does the same. The two numbers are per channel — WhatsApp and Messenger are
    hand-sent and fail differently from email, so they carry their own, lower pair.
    """
    template_cap, unique_cap = channel_caps(channel)
    if personalization == "template":
        return template_cap
    row = conn.execute(
        "SELECT count(*) FROM stage_events "
        "WHERE phx_date = ? AND channel = ? AND personalization = 'template'",
        (phx_day, channel),
    ).fetchone()
    return template_cap if row[0] else unique_cap


def cap_refusal(conn, phx_day: str, channel: str, personalization: str) -> str | None:
    """The cap gate, both halves. Returns a refusal reason or None.

    Two limits, because one calendar day is not the same thing as one sending
    window: the Phoenix-day count (the documented rule) AND a rolling 24h count
    (so midnight cannot double the queue).
    """
    cap = daily_cap(conn, phx_day, channel, personalization)
    used = staged_today(conn, phx_day, channel)
    if used >= cap:
        return (f"daily cap reached: {used}/{cap} {channel} artifacts staged "
                f"for Phoenix day {phx_day} ({personalization}-body class)")
    rolling = staged_in_window(conn, channel)
    if rolling >= cap:
        return (f"rolling cap reached: {rolling}/{cap} {channel} artifacts staged in "
                f"the last {ROLLING_WINDOW_HOURS}h ({personalization}-body class). "
                f"The Phoenix day rolled over at midnight but the send queue did "
                f"not — releasing both halves would break the cadence rule.")
    return None


# --------------------------------------------------------------------------- #
# is this batch REALLY unique-bodied?  (the 30/day cap is the exception)
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _body_shape(row) -> str:
    """A body with its prospect-specific tokens removed, for comparison only."""
    text = (row["body"] or "").lower()
    for column in ("company_casual", "company_name", "owner_first_name", "market"):
        value = (row[column] or "").lower() if column in row.keys() else ""
        if value:
            text = text.replace(value, " ")
    return " ".join(_TOKEN_RE.findall(text))


def effective_personalization(conn, batch_id: str, declared: str) -> str:
    """`template` whenever the bodies in a batch are near-identical, whatever it says.

    The operator's rule makes 30/day the EXCEPTION for uniquely-written bodies. Nothing
    measured that, so declaring `unique` on a mail-merge batch bought the higher
    cap for free. Similarity is measured on the body with the merge tokens taken
    out, so swapping the company name is not mistaken for writing a new email.
    """
    if declared == "template":
        return "template"
    rows = conn.execute(
        "SELECT m.body, p.company_casual, p.company_name, p.owner_first_name, p.market "
        "FROM messages m JOIN prospects p ON p.id = m.prospect_id "
        "WHERE m.batch_id = ? AND m.state NOT IN ('REJECTED','CANCELLED','LINT_FAILED') "
        "ORDER BY m.id", (batch_id,)).fetchall()
    shapes = [_body_shape(row) for row in rows]
    shapes = [shape for shape in shapes if shape]
    if len(shapes) < 2:
        return declared
    scores = []
    for index, left in enumerate(shapes):
        for right in shapes[index + 1:]:
            scores.append(difflib.SequenceMatcher(None, left, right).ratio())
    scores.sort()
    median = scores[len(scores) // 2]
    return "template" if median >= TEMPLATE_SIMILARITY else declared


# --------------------------------------------------------------------------- #
# the cap ledger is per-database, and DM_ENGINE_DB is an environment variable
# --------------------------------------------------------------------------- #

def shared_ledger_warning(cfg) -> str | None:
    """Loud note when this run's cap ledger is NOT the default database.

    The cap counts `stage_events` in one SQLite file, but every draft lands in one
    Gmail account. Two databases (the documented rehearsal mechanism) therefore
    hold two independent ledgers that both allow a full day's cap. There is no
    cross-database ledger to fix this with — locks are shared, counters are not —
    so the foot-gun is made loud instead of silent: the warning goes into the
    stage output AND into `state_log`, where it survives the terminal session.
    """
    default = Path(cfg.root) / "data" / "dm.db"
    current = Path(cfg.db_path)
    try:
        if current.resolve() == default.resolve():
            return None
    except OSError:                                     # pragma: no cover - fs error
        return None
    return (f"cap ledger is {current}, not the default {default}. The daily cap "
            "counts artifacts in THIS database only, while every draft lands in "
            "one Gmail account — two databases can jointly stage two full days' "
            "worth. Check the other ledger before releasing anything.")


def require_suppression_sync(conn, cfg, gate: str):
    """Import the shared suppression list before a gate, or refuse the run.

    `channels.stage()` calls this itself rather than trusting `cli.py` to have
    done it: an agent that imports the adapter directly must hit the same gate.
    """
    from .. import suppression

    return suppression.require_sync(conn, cfg, gate)


def record_stage_event(conn, message_id: int, channel: str, personalization: str,
                       phx_day: str) -> None:
    conn.execute(
        "INSERT INTO stage_events (message_id, channel, personalization, phx_date) "
        "VALUES (?, ?, ?, ?)",
        (message_id, channel, personalization, phx_day),
    )


#: Kept in lockstep with trigger G8 in data/migrations/005_dm_channels.sql.
#: `contacts.address_canon` folds '+tag' (every provider) and dots (gmail.com only)
#: for email, digits for a WhatsApp number, and the page handle for a Messenger URL —
#: so suppressing one spelling of a destination blocks the others. A `person`-scope
#: row crosses channels (it is a fact about a human); `domain` scope never applies to
#: a phone number or a page handle.
_SUPPRESSION_MATCH = """
        SELECT s.reason, s.scope, s.address
        FROM contacts c
        JOIN suppression s
          ON (s.channel = c.channel OR s.channel = 'all' OR s.scope = 'person')
        WHERE c.id = ?
          AND ( (s.scope = 'address'
                 AND (s.channel = c.channel OR s.channel = 'all')
                 AND (lower(s.address) = c.addr_lc OR lower(s.address) = c.address_canon))
             OR (s.scope = 'person'
                 AND (lower(s.address) = lower(c.person_key)
                      OR lower(s.address) = c.address_canon))
             OR (s.scope = 'domain'
                 AND (s.channel = c.channel OR s.channel = 'all')
                 AND c.channel NOT IN ('whatsapp','messenger')
                 AND ltrim(lower(s.address), '@') <> ''
                 AND (   c.addr_host = ltrim(lower(s.address), '@')
                      OR c.addr_host LIKE '%.' || ltrim(lower(s.address), '@')
                      OR (c.addr_host = '' AND c.addr_lc LIKE '%' || lower(s.address)) )) )
        ORDER BY CASE s.scope WHEN 'address' THEN 0 WHEN 'person' THEN 1 ELSE 2 END
        LIMIT 1
"""


def suppression_hit(conn, contact_id: int) -> str | None:
    """Mirror of trigger G8, as a readable pre-check. Returns the reason or None.

    Fails CLOSED: if the query itself cannot run, the answer is "suppressed", not
    "clear". This is the same stance `suppression.is_suppressed` takes.
    """
    try:
        row = conn.execute(_SUPPRESSION_MATCH, (contact_id,)).fetchone()
    except Exception as exc:                            # noqa: BLE001 — fail closed
        return f"suppressed (fail_closed: suppression lookup failed: {exc})"
    if row is None:
        return None
    return f"suppressed ({row['reason']}, scope={row['scope']}, match={row['address']})"


def prospect_suppressed(conn, prospect_id: int) -> str | None:
    """Mirror of trigger G12: the COMPANY asked to stop, not just one mailbox."""
    try:
        row = conn.execute("SELECT state FROM prospects WHERE id = ?",
                           (prospect_id,)).fetchone()
    except Exception as exc:                            # noqa: BLE001 — fail closed
        return f"prospect state unreadable ({exc}) — refusing to stage"
    if row is not None and row["state"] == "SUPPRESSED":
        return ("prospect is SUPPRESSED — this company asked to stop. Suppression "
                "is permanent by design (PLAN.md §9.34); there is no release path.")
    return None


def verify_refusal(row) -> str | None:
    """Mirror of trigger G9. Returns a refusal reason, or None when stageable.

    PASS stages. RISK stages only from a source a human actually saw (website,
    reply, linkedin, manual) — never from a format guess, Apollo or a directory.
    FAIL and unverified never stage.
    """
    from ..gmail_compat import FAIL, PASS, RISK, VERIFIED_SOURCES

    verdict = row["verify_verdict"]
    source = row["address_source"]
    if verdict == PASS:
        return None
    if verdict == RISK:
        if source in VERIFIED_SOURCES:
            return None
        return (f"RISK verdict from an unverified source ({source}) — "
                f"{row['verify_reason'] or 'no reason recorded'}. "
                "Only website/reply/linkedin/manual RISK addresses may stage.")
    if verdict == FAIL:
        return (f"FAIL verdict — {row['verify_reason'] or 'no reason recorded'}. "
                "A FAIL address bounces and damages sender reputation; "
                "fall back to LinkedIn or a contact form.")
    return ("no verification verdict recorded — run `dmctl verify` "
            "(authoritative --smtp runs on the VPS)")


def verification_staleness(row, now: datetime | None = None) -> str | None:
    """Advisory warning when a verdict has aged past VERIFY_STALE_DAYS (edge 11)."""
    verified_at = row["verified_at"]
    if not verified_at:
        return None
    checked = _parse_sqlite_utc(verified_at)
    if checked is None:
        return None
    age = (now or datetime.now(timezone.utc)) - checked
    if age > timedelta(days=VERIFY_STALE_DAYS):
        return (f"verification is {age.days} days old (>{VERIFY_STALE_DAYS}) — "
                "domains die between prospecting and sending; consider re-verifying")
    return None


def recent_person_touch(conn, message_id: int, person_key: str | None,
                        days: int = PERSON_COOLDOWN_DAYS) -> str | None:
    """The same human at another company inside the cooldown window (edge 2)."""
    if not person_key:
        return None
    row = conn.execute(
        """
        SELECT m.id, m.sent_at, p.company_name
        FROM messages m
        JOIN contacts  c ON c.id = m.contact_id
        JOIN prospects p ON p.id = m.prospect_id
        WHERE c.person_key = ?
          AND m.id != ?
          AND m.state IN ('SENT','REPLIED','BOUNCED','CLOSED')
          AND m.sent_at IS NOT NULL
          AND m.sent_at > datetime('now', ?)
        ORDER BY m.sent_at DESC LIMIT 1
        """,
        (person_key, message_id, f"-{int(days)} days"),
    ).fetchone()
    if row is None:
        return None
    return (f"held: same person ({person_key}) was messaged {row['sent_at']} "
            f"via {row['company_name']} — inside the {days}-day cooldown. "
            f"Release deliberately with `dmctl stage --allow-person {person_key}`.")


def stagger_plan(count: int, start_hour: int = 11,
                 minutes: int = STAGGER_MINUTES) -> list[str]:
    """Planned release clock times, MST, one per artifact (cadence rule 2/3).

    The engine cannot send and Gmail has no `scheduledSendTime`, so nothing
    downstream can enforce the 3-5 minute stagger for a human releasing drafts by
    hand. Printing the plan is the honest middle: whoever arms the send has the
    times in front of them, and `stage`/`review` record the same plan.
    """
    times = []
    for index in range(max(0, count)):
        window, position = divmod(index, STAGGER_WINDOW_SIZE)
        # 11:00 MST for the first window, 17:00 MST for the second (§ cadence 3).
        base = start_hour + (6 * window)
        total = base * 60 + position * minutes
        times.append(f"{(total // 60) % 24:02d}:{total % 60:02d} MST")
    return times


# --------------------------------------------------------------------------- #
# prospect-local send window (edge 31 — the ICP is national, the operator is MST)
# --------------------------------------------------------------------------- #

def send_window_line(timezone_name: str, start_hour: int = 8, end_hour: int = 18,
                     ref: datetime | None = None) -> str:
    """'08:00-18:00 America/Boise = 07:00-17:00 MST' for a review file or card.

    The prospect list is national across 4+ time zones while the operator arms sends on
    a Phoenix clock. Printing both sides is what stops a cold email landing at
    5am local — a deliverability and reputation problem, and a deliberate act if
    it ever happens, never a default.

    Gmail cannot schedule this for us: `scheduledSendTime` does not exist in the
    Gmail API (it is silently ignored and the message sends immediately). The
    window is guidance for whoever arms the real send — DASHBOARD EXEC or the operator.
    """
    local = f"{start_hour:02d}:00-{end_hour:02d}:00 {timezone_name}"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
    except Exception as exc:                       # unknown zone / no tzdata
        return f"{local} (MST conversion unavailable: {type(exc).__name__})"

    day = (ref or datetime.now(timezone.utc)).astimezone(tz).date()
    converted = []
    for hour in (start_hour, end_hour):
        moment = datetime.combine(day, time(hour), tzinfo=tz).astimezone(_MST)
        converted.append(moment.strftime("%H:%M"))
    return f"{local} = {converted[0]}-{converted[1]} MST"


def _parse_sqlite_utc(value: str) -> datetime | None:
    """Parse a `datetime('now')` string ('YYYY-MM-DD HH:MM:SS') as UTC."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def dry_run_path(cfg, batch_id: str, message_id: int, suffix: str = "json") -> Path:
    directory = Path(cfg.outbox_dir) / "dry-run"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{batch_id}-{message_id}.{suffix}"
