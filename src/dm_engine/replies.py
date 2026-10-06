"""Reply detection and classification. Contract: PLAN.md §12.6, §8.1, §9 "Replies / outcomes".

The whole point of this module is that **an auto-responder is not a reply**. A bounce,
an "out of office", and a human typing "what do you charge?" are three different events
and only the third one belongs in the numerator the operator makes decisions on.

So classification is explicit, stored, and auditable:

  bounce      delivery failed          is_human=0  -> suppression + message BOUNCED
  auto_reply  machine answered         is_human=0  -> nothing (never a reply)
  vacation    machine answered (OOO)   is_human=0  -> nothing
  hot/question/objection/no/unsure     is_human=1  -> the reply column

`unsure` means "a human replied, intent unclear" (the Reply Intent Playbook sense) —
NOT "we can't tell whether a machine wrote it". Anything with machine evidence gets a
machine class; `is_human=1` requires the absence of every machine marker below.

Machine evidence is recorded in `replies.classified_by` as `heuristic:<marker>` so a
wrong number can always be traced back to the exact rule that produced it.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import stats

# --------------------------------------------------------------------------- #
# classification vocabulary
# --------------------------------------------------------------------------- #

#: Classes that count as a human reply (PLAN.md §3.2 `replies.classification`).
HUMAN_CLASSES = frozenset({"hot", "question", "objection", "no", "unsure"})
#: Classes that must never reach a reply numerator (PLAN.md §4.4, §9 edge 35).
MACHINE_CLASSES = frozenset({"bounce", "auto_reply", "vacation"})
ALL_CLASSES = HUMAN_CLASSES | MACHINE_CLASSES

SNIPPET_MAX = 300

_DAEMON_LOCALPARTS = ("mailer-daemon", "mailerdaemon", "mail-daemon", "postmaster")
_NOREPLY_LOCALPARTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
    "bounce", "bounces", "notification", "notifications", "automailer", "auto-reply",
)

_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# A delivery DELAY is a machine message but NOT a delivery failure: it must not
# suppress the address and must not mark the message BOUNCED (the mail may still
# land). Real triage files have mis-filed these as prospect replies — see
# ~/work/dashboard/data/approvals/triage-2026-07-13-0150.json.
_DELAY_RE = re.compile(
    r"delivery\s+(is\s+)?delayed|delivery\s+incomplete|will\s+retry|retrying\s+for"
    r"|temporar(y|ily)\s+(failure|error|deferred)|message\s+still\s+undelivered"
    r"|warning:\s*message", re.I)

_BOUNCE_SUBJECT_RE = re.compile(
    r"delivery\s+status\s+notification|undeliverable|undelivered"
    r"|mail\s+delivery\s+(failed|subsystem)|returned\s+mail|delivery\s+failure"
    r"|failure\s+notice|message\s+not\s+delivered|address\s+not\s+found"
    r"|delivery\s+incomplete|delivery\s+delayed", re.I)

_HARD_BOUNCE_BODY_RE = re.compile(
    r"\b5\.\d\.\d\b|\b55\d\b|address\s+(couldn'?t\s+be\s+found|not\s+found)"
    r"|no\s+such\s+(user|mailbox|address)|mailbox\s+(unavailable|not\s+found|does\s+not\s+exist)"
    r"|user\s+unknown|relaying\s+denied|account\s+does\s+not\s+exist"
    r"|recipient\s+(address\s+)?rejected|does\s+not\s+exist", re.I)

# --- vacation / autoresponder wording, in two strengths ------------------------
#
# Finding H1: phrase-matching a body for "out of the office" and calling it a machine
# threw away the highest-intent replies this engine exists to find — "sorry, been out
# of the office on a job all week. what is this about?" is a busy owner answering,
# and dropping it also slams trigger G7 so he can never be sent the demo link.
#
# So wording comes in two strengths and structure outranks both:
#   STRONG  autoresponder GRAMMAR — a first-person present/future announcement, or a
#           canonical OOO form in another language. Machines write these; people don't.
#   WEAK    a bare mention of being away. A human sentence that merely mentions "out
#           of office" is a human reply, so weak wording only wins when nothing in the
#           message reads as human authorship (`_HUMAN_VOICE_RE`).

_VACATION_SUBJECT_RE = re.compile(
    r"\booo\b|out\s+of\s+(the\s+)?office|automatic\s+reply|auto[\s\-]?reply"
    r"|automatische\s+antwort|abwesenheit\w*|respuesta\s+autom\w*"
    r"|r(é|e)ponse\s+automatique|risposta\s+automatica", re.I)

_VACATION_STRONG_RE = re.compile(
    # first-person / office-wide announcement, present or future tense only:
    # "I am out of the office", "I'll be away", "our office is closed" — but NOT
    # "I was away until Monday", which is a human explaining a late answer.
    r"\b(i\s*am|i'?m|i\s+will\s+be|i'?ll\s+be|he\s+is|she\s+is|"
    r"(our|the)\s+(office|team|shop|crew)\s+(is|will\s+be))\s+"
    r"(currently\s+|presently\s+|now\s+)?"
    r"(out\s+of\s+(the\s+)?office|away\s+from\s+(the\s+)?(office|desk|email)|"
    r"on\s+(vacation|holiday|annual\s+leave|leave|maternity\s+leave|"
    r"paternity\s+leave|parental\s+leave)|unavailable|closed\s+(until|for))"
    r"|\bi\s+(will\s+be|'?ll\s+be)\s+back\s+in\s+the\s+office\b"
    r"|\breturning\s+to\s+the\s+office\s+on\b"
    r"|\bin\s+my\s+absence\b"
    r"|\bfor\s+(anything\s+)?urgent\w*(\s+matters)?[,:\s]+(please\s+)?(contact|call|email|reach)\b"
    r"|\bthank\s+you\s+for\s+your\s+(email|message)[^.!?]{0,60}"
    r"\b(out\s+of\s+(the\s+)?office|away|unavailable)\b"
    # canonical non-English out-of-office bodies (finding H1's mirror error: these
    # carried no Auto-Submitted header and were landing as is_human=1 `unsure`)
    r"|abwesenheitsnotiz|ich\s+bin\s+(derzeit\s+|zurzeit\s+)?nicht\s+im\s+b(ü|u)ro"
    r"|estoy\s+fuera\s+de\s+la\s+oficina|fuera\s+de\s+la\s+oficina\s+hasta"
    r"|je\s+suis\s+(actuellement\s+)?absent|absent\w*\s+du\s+bureau"
    r"|sono\s+(attualmente\s+)?fuori\s+(sede|ufficio)"
    r"|estou\s+fora\s+do\s+escrit(ó|o)rio"
    r"|ik\s+ben\s+(momenteel\s+)?afwezig", re.I)

#: A mention of being away, with no autoresponder grammar around it.
_VACATION_WEAK_RE = re.compile(
    r"out\s+of\s+(the\s+)?office|on\s+vacation|annual\s+leave|maternity\s+leave"
    r"|paternity\s+leave|parental\s+leave|away\s+until|currently\s+away"
    r"|holiday\s+until", re.I)

_AUTO_STRONG_RE = re.compile(
    r"auto(matic|mated)?[\s\-_]*(reply|response|responder|-?generated)"
    r"|this\s+is\s+an\s+automated"
    r"|do\s+not\s+reply\s+to\s+this"
    r"|unattended\s+mailbox", re.I)

#: Machine acknowledgement wording. Weak on its own — a human can write "we received
#: your message" too — but decisive when nothing else in the body reads as a person.
_AUTO_WEAK_RE = re.compile(
    r"we\s+(have\s+|'?ve\s+)?received\s+your\s+(message|email|e-mail|inquiry|enquiry|request)"
    r"|your\s+(message|email|e-mail|inquiry|enquiry|request)\s+has\s+been\s+received"
    r"|thank\s+you\s+for\s+contacting"
    r"|someone\s+will\s+(get\s+back|be\s+in\s+touch)"
    r"|(support\s+)?ticket\s+(#|number|has\s+been\s+(opened|created|logged))"
    r"|has\s+been\s+(opened|created|logged)\s+on\s+your\s+behalf"
    r"|\b(reference|case|ticket)\s*(#|number\s*:?|:)\s*\w", re.I)

#: Evidence a person typed this. Vetoes WEAK wording only — never a header, a daemon
#: sender, a DSN subject or a no-reply address. Structure always outranks phrasing.
_HUMAN_VOICE_RE = re.compile(
    r"\?"                                                  # a question aimed at us
    r"|\b(call|text|ring|ping|email|phone)\s+me\b"
    r"|\bhow\s+much\b|\bwhat\s+do\s+you\s+charge\b"
    r"|\bwhat'?s?\s+(the\s+)?(price|cost)\b|\bwhat\s+is\s+(the\s+)?(price|cost)\b"
    r"|\b(not\s+interested|no\s+thanks|all\s+set|take\s+me\s+off|remove\s+me"
    r"|unsubscribe|stop\s+(emailing|contacting|messaging))\b"
    r"|(^|[\s,;])(but|however)\b"                           # a contrast a machine never draws
    r"|\bwe\s+(handle|do|have)\s+(our\s+own|that\s+in[\s\-]house)\b"
    r"|\bi\s+(was|were)\b|\bbeen\s+out\b|\bjust\s+(seeing|saw|got\s+back)\b"
    r"|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",                    # they left a phone number
    re.I)

_UNSUBSCRIBE_RE = re.compile(
    r"\bunsubscribe\b|\bremove\s+me\b|take\s+me\s+off|opt\s*-?\s*out"
    r"|stop\s+(emailing|contacting|messaging)|do\s+not\s+(email|contact)\s+me", re.I)


@dataclass(frozen=True)
class Classification:
    """The verdict plus the exact evidence that produced it."""

    classification: str
    is_human: bool
    marker: str

    @property
    def classified_by(self) -> str:
        # Every row carries its marker, human ones included (finding L2). A row that
        # was WRONGLY called human inflates the numerator just as badly as a wrongly
        # discarded one deflates it, and 'heuristic' with no marker left no way to ask
        # which rule produced it.
        return f"heuristic:{self.marker}"


class ClassificationError(ValueError):
    """A caller supplied a classification the schema does not allow."""


def is_human_class(classification: str) -> bool:
    """Single source of truth for the human/machine split (PLAN.md §4.4)."""
    if classification not in ALL_CLASSES:
        raise ClassificationError(
            f"unknown classification {classification!r}; allowed: {sorted(ALL_CLASSES)}")
    return classification in HUMAN_CLASSES


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def _norm(text: str | None) -> str:
    """NFKC + zero-width strip, so homoglyph/ZWJ smuggling cannot dodge a rule."""
    if not text:
        return ""
    cleaned = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in cleaned if ch not in "​‌‍﻿⁠")


def _localpart(sender: str) -> str:
    match = _ADDR_RE.search(sender or "")
    return match.group(0).split("@", 1)[0].lower() if match else ""


def sender_address(sender: str) -> str | None:
    """'Name <a@b.com>' -> 'a@b.com' (lowercased). None when there is no address."""
    match = _ADDR_RE.search(sender or "")
    return match.group(0).lower() if match else None


def classify(sender: str, subject: str, body: str = "",
             headers: dict | None = None) -> Classification:
    """Decide whether an inbound message is a human reply, and if not, what it is.

    Ordered strongest-evidence-first. Every branch names its marker so the stored
    row explains itself. Defaults to a human `unsure` ONLY when no machine marker
    fires at all.
    """
    headers = {k.lower(): _norm(v) for k, v in (headers or {}).items()}
    sender_n = _norm(sender)
    subject_n = _norm(subject)
    body_n = _norm(body)
    haystack = f"{subject_n}\n{body_n}"
    local = _localpart(sender_n)

    delayed = bool(_DELAY_RE.search(haystack))
    hard = bool(_HARD_BOUNCE_BODY_RE.search(haystack))

    def _delivery_verdict(marker: str) -> Classification:
        # A delay notice is machine mail but not a failure: never suppress on it.
        if delayed and not hard:
            return Classification("auto_reply", False, f"{marker}+delivery_delay")
        return Classification("bounce", False, marker)

    # 1. Null return-path: definitive machine mail (bounces and autoresponders).
    if headers.get("return-path", "").strip() in {"<>", ""} and "return-path" in headers:
        return _delivery_verdict("null_return_path")

    # 2. Daemon sender.
    if any(local == d or local.startswith(d) for d in _DAEMON_LOCALPARTS):
        return _delivery_verdict("daemon_sender")

    # 3. Delivery-status subject / headers.
    if _BOUNCE_SUBJECT_RE.search(subject_n) or "x-failed-recipients" in headers:
        return _delivery_verdict("dsn_subject")
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted.strip() != "no":
        if "failure" in auto_submitted or "non-delivery" in auto_submitted:
            return _delivery_verdict("auto_submitted_failure")
        marker = "auto_submitted_header"
        # the header already proved this is a machine; the wording only picks which
        # kind of machine, so the weak pattern is the right one to use here.
        if (_VACATION_WEAK_RE.search(haystack) or _VACATION_STRONG_RE.search(haystack)
                or _VACATION_SUBJECT_RE.search(subject_n)):
            return Classification("vacation", False, marker)
        return Classification("auto_reply", False, marker)

    # 4. Explicit autoresponder headers.
    for header in ("x-autoreply", "x-autorespond", "x-auto-response-suppress",
                   "x-autoresponder"):
        if header in headers:
            return Classification("auto_reply", False, header.replace("-", "_"))
    precedence = headers.get("precedence", "").lower()
    if precedence in {"auto_reply", "auto-reply", "auto_replied"}:
        return Classification("auto_reply", False, "precedence_auto")
    # bulk/junk/list only counts as machine mail when it really is list mail —
    # some CRMs stamp Precedence: bulk on genuine human outbound.
    if precedence in {"bulk", "junk", "list"} and (
            "list-unsubscribe" in headers or "list-id" in headers):
        return Classification("auto_reply", False, "precedence_list")

    # 5. Unattended sender addresses.
    if any(local == n or local.startswith(n) for n in _NOREPLY_LOCALPARTS):
        return Classification("auto_reply", False, "noreply_sender")

    # --- everything below is body/subject WORDING, which is the weakest evidence
    #     there is. Order: subject line, then autoresponder grammar, then bare
    #     mentions that only count when the message has no human voice at all.

    # 6. Subject line: an autoresponder announces itself there ("Automatic reply:").
    if _VACATION_SUBJECT_RE.search(subject_n):
        return Classification("vacation", False, "vacation_subject")

    # 7. Autoresponder grammar in the body.
    if _VACATION_STRONG_RE.search(haystack):
        return Classification("vacation", False, "vacation_announcement")
    if _AUTO_STRONG_RE.search(haystack):
        return Classification("auto_reply", False, "autoresponder_wording")

    # 8. Bare wording. A human sentence that merely mentions being out of the office,
    #    or thanks us for making contact before asking what we sell, is a human reply.
    weak_vacation = bool(_VACATION_WEAK_RE.search(haystack))
    weak_auto = bool(_AUTO_WEAK_RE.search(haystack))
    if weak_vacation or weak_auto:
        marker = "vacation_mention" if weak_vacation else "machine_ack_wording"
        if not _HUMAN_VOICE_RE.search(haystack):
            return Classification("vacation" if weak_vacation else "auto_reply",
                                  False, marker)
        # a person wrote around the phrase — keep them in the numerator, and record
        # which rule was overruled so a wrong call is traceable (finding L2)
        return Classification("unsure", True, f"human_voice_over_{marker}")

    # 9. No machine evidence: a human wrote it, intent unknown.
    return Classification("unsure", True, "no_machine_marker")


def looks_unsubscribe(text: str) -> bool:
    """Wording that turns a `no` into an unsubscribe (PLAN.md §8.1, §9 edge 34)."""
    return bool(_UNSUBSCRIBE_RE.search(_norm(text)))


# --------------------------------------------------------------------------- #
# small DB helpers (integration seams: see module docstring of cli.py)
# --------------------------------------------------------------------------- #

def _log_state(conn, entity: str, entity_id, from_state, to_state: str,
               actor: str, reason: str | None, evidence: str | None) -> None:
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity, str(entity_id), from_state, to_state, actor, reason, evidence),
    )


def transition(conn, entity: str, entity_id, to_state: str, actor: str,
               reason: str | None = None, evidence: str | None = None) -> None:
    """Move an entity's state and append to the audit log.

    INTEGRATION SEAM — delegates to Owner D's `state.transition` the moment that
    module exists. The fallback performs the same guarded UPDATE plus `state_log`
    append; the G1–G11 triggers enforce transition legality either way, so the
    fallback cannot smuggle an illegal edge past the database.
    """
    try:
        from . import state as _state  # type: ignore
    except ImportError:
        _state = None
    if _state is not None and hasattr(_state, "transition"):
        _state.transition(conn, entity, entity_id, to_state, actor,
                          reason=reason, evidence=evidence)
        return

    table = {"message": "messages", "prospect": "prospects", "batch": "batches"}[entity]
    row = conn.execute(f"SELECT state FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise LookupError(f"{entity} {entity_id} does not exist")
    from_state = row[0]
    if from_state == to_state:
        return
    conn.execute(f"UPDATE {table} SET state = ? WHERE id = ?", (to_state, entity_id))
    _log_state(conn, entity, entity_id, from_state, to_state, actor, reason, evidence)


def _suppress(conn, channel: str, address: str, reason: str, source: str,
              scope: str = "address") -> None:
    """Add a suppression row. Delegates to Owner B's suppression.add when present."""
    try:
        from . import suppression as _suppression  # type: ignore
    except ImportError:
        _suppression = None
    if _suppression is not None and hasattr(_suppression, "add"):
        _suppression.add(conn, channel, address.lower(), scope, reason, source)
        return
    conn.execute(
        "INSERT OR IGNORE INTO suppression (channel, address, scope, reason, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel, address.lower(), scope, reason, source),
    )


#: Outcomes that record a historical EVENT rather than a current status.
BOOKED_OUTCOMES = ("demo_booked", "call_booked")
#: `to_state` marker for a status change that arrived after a booking. `dmctl status`
#: counts these; nothing else needs to know.
POST_BOOKING_STATE = "outcome_after_booking"


def set_outcome(conn, prospect_id: int, outcome: str, actor: str,
                evidence: str | None = None) -> None:
    """Upsert `outcomes.outcome` without touching attribution columns.

    A booking is never overwritten (finding H3). `outcomes.outcome` was one slot doing
    two jobs — the immutable historical event AND the mutable current status — so a
    later "no thanks" silently erased a recorded demo: sheet `booked` flipped back to
    'n', `dm.demos_booked_total` returned to 0, and the variant that actually
    converted read zero. Books-then-unsubscribes is a completely normal sequence.
    The later status still lands where it operationally matters — `suppression`, which
    is what refuses the next message (G8) — and in `state_log`, which is the record of
    what happened when.
    """
    existing = conn.execute(
        "SELECT outcome FROM outcomes WHERE prospect_id = ?", (prospect_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO outcomes (prospect_id, outcome, evidence) VALUES (?, ?, ?)",
            (prospect_id, outcome, evidence),
        )
        _log_state(conn, "prospect", prospect_id, None, f"outcome:{outcome}",
                   actor, "outcome recorded", evidence)
        return
    if existing[0] == outcome:
        return
    if existing[0] in BOOKED_OUTCOMES and outcome not in BOOKED_OUTCOMES:
        _log_state(
            conn, "prospect", prospect_id, f"outcome:{existing[0]}",
            f"{POST_BOOKING_STATE}:{outcome}", actor,
            "booking kept: a demo that happened cannot be un-happened by a later "
            "reply (suppression still applied)", evidence)
        return
    conn.execute(
        "UPDATE outcomes SET outcome = ?, evidence = COALESCE(?, evidence), "
        "updated_at = datetime('now') WHERE prospect_id = ?",
        (outcome, evidence, prospect_id),
    )
    _log_state(conn, "prospect", prospect_id, f"outcome:{existing[0]}",
               f"outcome:{outcome}", actor, "outcome changed", evidence)


def _prospect_contact_address(conn, prospect_id: int, channel: str) -> str | None:
    row = conn.execute(
        "SELECT address FROM contacts WHERE prospect_id = ? AND channel = ? "
        "ORDER BY id LIMIT 1", (prospect_id, channel)).fetchone()
    return row[0] if row else None


def _is_suppressed_prospect(conn, prospect_id: int) -> bool:
    """Any contact of this prospect on the suppression list. Mirrors trigger G8.

    Compares `address_canon`, not the raw address, and lets a `person`-scope row
    cross channels — the same two rules G8 and `channels.base._SUPPRESSION_MATCH`
    apply. Without the canonical form this answered "not suppressed" for a WhatsApp
    number stored as '+1602…' against a suppression row holding the folded digits,
    and a prospect who said stop would be reopened by their own next reply.
    """
    row = conn.execute(
        "SELECT 1 FROM contacts c JOIN suppression s "
        "  ON (s.channel = c.channel OR s.channel = 'all' OR s.scope = 'person') "
        "WHERE c.prospect_id = ? AND ("
        "     (s.scope = 'address'"
        "      AND (s.channel = c.channel OR s.channel = 'all')"
        "      AND (lower(s.address) = c.addr_lc OR lower(s.address) = c.address_canon))"
        "  OR (s.scope = 'person'"
        "      AND (lower(s.address) = lower(c.person_key)"
        "           OR lower(s.address) = c.address_canon))"
        "  OR (s.scope = 'domain'"
        "      AND (s.channel = c.channel OR s.channel = 'all')"
        "      AND c.channel NOT IN ('whatsapp','messenger')"
        "      AND ltrim(lower(s.address), '@') <> ''"
        "      AND (   c.addr_host = ltrim(lower(s.address), '@')"
        "           OR c.addr_host LIKE '%.' || ltrim(lower(s.address), '@')"
        "           OR (c.addr_host = '' AND c.addr_lc LIKE '%' || lower(s.address))))"
        ") LIMIT 1",
        (prospect_id,)).fetchone()
    return row is not None


def _apply_side_effects(conn, prospect_id: int, channel: str, classification: str,
                        snippet: str, actor: str) -> None:
    """Suppression + outcome consequences of a recorded reply (PLAN.md §8.1)."""
    address = _prospect_contact_address(conn, prospect_id, channel)
    if classification == "no":
        unsub = looks_unsubscribe(snippet)
        reason = "unsubscribe" if unsub else "hard_no"
        if address:
            _suppress(conn, channel, address, reason, "reply_scan")
            if not unsub:
                row = conn.execute(
                    "SELECT person_key FROM contacts WHERE prospect_id = ? AND channel = ? "
                    "AND person_key IS NOT NULL ORDER BY id LIMIT 1",
                    (prospect_id, channel)).fetchone()
                # A hard no follows the human, not the company (§9 edge 34). Skipped
                # when person_key == address: suppression is UNIQUE(channel, address),
                # so the address-scope row already covers that identity.
                if row and row[0] and row[0].lower() != address.lower():
                    _suppress(conn, channel, row[0], "hard_no", "reply_scan", scope="person")
        set_outcome(conn, prospect_id, "unsubscribed" if unsub else "replied_no", actor)
        return
    if classification in HUMAN_CLASSES:
        current = conn.execute(
            "SELECT outcome FROM outcomes WHERE prospect_id = ?", (prospect_id,)).fetchone()
        # §9 edge 33: a human reply reopens a dead prospect — unless they are
        # suppressed, in which case the engine records it and stays silent.
        if current is None:
            set_outcome(conn, prospect_id, "in_progress", actor)
        elif current[0] in ("dead",) and not _is_suppressed_prospect(conn, prospect_id):
            set_outcome(conn, prospect_id, "in_progress", actor, "reply after dead")


def _step1_message(conn, prospect_id: int):
    """The prospect's step-1 message row, whatever state it reached."""
    return conn.execute(
        "SELECT id, state, created_at, staged_at, sent_at FROM messages "
        "WHERE prospect_id = ? AND sequence_step = 1 ORDER BY id LIMIT 1",
        (prospect_id,)).fetchone()


def demote_carryover(conn, prospect_id: int, received_at,
                     verdict: Classification) -> Classification:
    """Refuse to credit an inbound that predates the message it would be credited to.

    Finding C2: `scan()` searched `from:<address> newer_than:30d` with no lower bound
    tied to the send, so mail from an EARLIER campaign to the same ICP — the operator has
    been emailing these people since June — landed as a dm-engine reply. The row even
    stored the timestamp that proves it impossible; nothing compared it to `sent_at`.

    The row is still recorded (never lose evidence), but with `is_human = 0` and a
    marker naming why: it is not a reply to this message, so it must not enter the
    numerator, must not flip the message to REPLIED, and must not open trigger G7 to
    a prospect who never answered us.
    """
    if not verdict.is_human:
        return verdict
    row = _step1_message(conn, prospect_id)
    if row is None:
        return verdict
    if stats.reply_could_answer(received_at, row["created_at"], row["staged_at"],
                                row["sent_at"]):
        return verdict
    return Classification(verdict.classification, False,
                          f"pre_send_carryover+{verdict.marker}")


def _mark_replied(conn, prospect_id: int, actor: str, evidence: str | None) -> bool:
    """SENT -> REPLIED on the prospect's step-1 message. True when it moved."""
    row = conn.execute(
        "SELECT id FROM messages WHERE prospect_id = ? AND sequence_step = 1 "
        "AND state = 'SENT' ORDER BY id LIMIT 1", (prospect_id,)).fetchone()
    if row is None:
        return False
    transition(conn, "message", row[0], "REPLIED", actor,
               reason="human reply recorded", evidence=evidence)
    return True


def _mark_bounced(conn, prospect_id: int, actor: str, evidence: str | None) -> bool:
    row = conn.execute(
        "SELECT id FROM messages WHERE prospect_id = ? AND sequence_step = 1 "
        "AND state IN ('SENT','STAGED') ORDER BY id DESC LIMIT 1",
        (prospect_id,)).fetchone()
    if row is None:
        return False
    transition(conn, "message", row[0], "BOUNCED", actor,
               reason="bounce detected", evidence=evidence)
    return True


def _insert_reply(conn, *, prospect_id: int, message_id: int | None,
                  gmail_message_id: str | None, received_at: str,
                  classification: str, is_human: bool, snippet: str,
                  classified_by: str) -> int | None:
    """Insert one reply row. Returns its id, or None when already recorded."""
    if gmail_message_id:
        existing = conn.execute(
            "SELECT id FROM replies WHERE gmail_message_id = ?",
            (gmail_message_id,)).fetchone()
        if existing is not None:
            return None
    cur = conn.execute(
        "INSERT INTO replies (prospect_id, message_id, gmail_message_id, received_at, "
        "classification, is_human, snippet, classified_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (prospect_id, message_id, gmail_message_id, received_at, classification,
         1 if is_human else 0, (snippet or "")[:SNIPPET_MAX], classified_by),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def record_reply(conn, prospect_id: int, channel: str, classification: str,
                 snippet: str, actor: str) -> None:
    """Manually record a reply (LinkedIn, or the operator correcting the scanner).

    Contract: PLAN.md §12.6. Raises ClassificationError on an unknown class, so a
    typo can never quietly land as a human reply.
    """
    human = is_human_class(classification)
    _insert_reply(
        conn, prospect_id=prospect_id, message_id=None, gmail_message_id=None,
        received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        classification=classification, is_human=human, snippet=snippet,
        classified_by=actor or "operator",
    )
    _apply_side_effects(conn, prospect_id, channel, classification, snippet, actor)
    if human:
        _mark_replied(conn, prospect_id, actor, "manual record-reply")
    elif classification == "bounce":
        address = _prospect_contact_address(conn, prospect_id, channel)
        if address:
            _suppress(conn, channel, address, "bounce", "dmctl")
        _mark_bounced(conn, prospect_id, actor, "manual record-reply")
    conn.commit()


def ingest_triage(conn, cfg, path: Path) -> int:
    """Ingest a `dashboard-reply-triage` output file. Returns rows recorded.

    The triage file is an LLM's judgment and is trusted only for INTENT. Whether
    something is a human at all is re-decided here from the sender/subject: real
    triage files have labelled a mailer-daemon delivery notice as `no`, which would
    have booked a fake human reply and suppressed a live address.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    source = f"triage_file:{path.name}"
    recorded = 0
    for item in items:
        sender = item.get("from") or ""
        address = sender_address(sender)
        if not address:
            continue
        row = conn.execute(
            "SELECT prospect_id, channel FROM contacts WHERE lower(address) = ? "
            "ORDER BY id LIMIT 1", (address,)).fetchone()
        if row is None:
            continue  # not one of ours — never invent a prospect
        prospect_id, channel = row["prospect_id"], row["channel"]
        text = " ".join(str(item.get(k) or "") for k in ("summary", "snippet", "suggested"))
        received_at = (data.get("generatedAt")
                       or datetime.now(timezone.utc).isoformat(timespec="seconds"))
        verdict = classify(sender, item.get("subject") or "", text)
        # the triage file checks no message state at all — one of the two documented
        # routes by which a reply used to reach a prospect who was never delivered to
        verdict = demote_carryover(conn, prospect_id, received_at, verdict)
        if verdict.is_human:
            claimed = (item.get("classification") or "unsure").strip().lower()
            classification = claimed if claimed in HUMAN_CLASSES else "unsure"
            classified_by = source
        else:
            # machine evidence overrides whatever the triage file claimed
            classification = verdict.classification
            classified_by = f"{source}+{verdict.classified_by}"
        new_id = _insert_reply(
            conn, prospect_id=prospect_id, message_id=None,
            gmail_message_id=item.get("id"), received_at=received_at,
            classification=classification, is_human=verdict.is_human,
            snippet=item.get("summary") or text, classified_by=classified_by,
        )
        if new_id is None:
            continue
        recorded += 1
        _apply_side_effects(conn, prospect_id, channel, classification,
                            item.get("summary") or text, source)
        if verdict.is_human:
            _mark_replied(conn, prospect_id, source, f"triage:{item.get('id')}")
        elif classification == "bounce":
            addr = _prospect_contact_address(conn, prospect_id, channel) or address
            _suppress(conn, channel, addr, "bounce", "triage_file")
            _mark_bounced(conn, prospect_id, source, f"triage:{item.get('id')}")
    conn.commit()
    return recorded


# --------------------------------------------------------------------------- #
# Gmail scan (read-only)
# --------------------------------------------------------------------------- #

def build_service(cfg, service_factory=None):
    """Resolve a Gmail service. Read-only use only — this engine cannot send.

    `service_factory` may be a zero-arg callable, a one-arg callable taking cfg, or
    an already-built service object (anything exposing `.users`). With no factory it
    defers to Owner D's `gmail_compat.get_service`, which refuses under test mode.
    """
    if service_factory is None:
        from . import gmail_compat  # noqa: PLC0415 — lazy: touches the skill scripts
        return gmail_compat.get_service(cfg)
    if not callable(service_factory) and hasattr(service_factory, "users"):
        return service_factory
    try:
        params = inspect.signature(service_factory).parameters
        required = [p for p in params.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        required = []
    return service_factory(cfg) if required else service_factory()


def _headers(payload: dict) -> dict:
    out: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name", "")).lower()
        value = str(header.get("value", ""))
        out[name] = f"{out[name]}\n{value}" if name in out else value
    return out


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _body_text(payload: dict) -> str:
    """First text/plain part, else text/html stripped, else ''."""
    stack = [payload]
    html = ""
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            return _decode(data)
        if data and mime == "text/html" and not html:
            html = re.sub(r"<[^>]+>", " ", _decode(data))
        stack.extend(part.get("parts") or [])
    return html


def message_text(msg: dict) -> str:
    text = _body_text(msg.get("payload") or {})
    return text or (msg.get("snippet") or "")


def _received_at(msg: dict, headers: dict) -> str:
    internal = msg.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(
                int(internal) / 1000, timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            pass
    return headers.get("date") or datetime.now(timezone.utc).isoformat(timespec="seconds")


def _list_ids(service, query: str, limit: int) -> list[str]:
    result = service.users().messages().list(
        userId="me", q=query, maxResults=limit).execute() or {}
    return [m["id"] for m in (result.get("messages") or []) if m.get("id")]


def _get(service, message_id: str) -> dict:
    return service.users().messages().get(
        userId="me", id=message_id, format="full").execute() or {}


def scan(conn, cfg, service_factory=None, newer_than_days: int = 30,
         per_address_limit: int = 10) -> dict:
    """Scan Gmail for replies to sent email messages. Contract: PLAN.md §12.6.

    Read-only. Returns `{'scanned', 'new_replies', 'bounces', 'auto', 'carryover'}`
    where `new_replies` counts ONLY newly recorded human replies that could actually
    be answers to the message they are credited to; `carryover` counts inbound mail
    that arrived before that message existed — recorded, visible, never counted.
    """
    service = build_service(cfg, service_factory)
    counts = {"scanned": 0, "new_replies": 0, "bounces": 0, "auto": 0, "carryover": 0}

    targets = conn.execute(
        "SELECT m.id AS message_id, m.prospect_id, m.gmail_thread_id, "
        "       m.created_at, m.staged_at, m.sent_at, "
        "       lower(c.address) AS address "
        "FROM messages m JOIN contacts c ON c.id = m.contact_id "
        "WHERE m.channel = 'email' AND m.state IN ('SENT','REPLIED') "
        "ORDER BY m.id").fetchall()
    if not targets:
        return counts

    by_address: dict[str, list] = {}
    for row in targets:
        by_address.setdefault(row["address"], []).append(row)

    window = f" newer_than:{newer_than_days}d" if newer_than_days else ""
    for address, rows in by_address.items():
        query = f"from:{address}{window}"
        for gmail_id in _list_ids(service, query, per_address_limit):
            msg = _get(service, gmail_id)
            counts["scanned"] += 1
            _record_inbound(conn, msg, gmail_id, rows, counts,
                            fallback_address=address)

    _scan_threads(conn, service, targets, counts)

    counts_from_dsn = _scan_bounces(conn, service, by_address, newer_than_days,
                                    per_address_limit * max(len(by_address), 1))
    counts["scanned"] += counts_from_dsn["scanned"]
    counts["bounces"] += counts_from_dsn["bounces"]
    counts["auto"] += counts_from_dsn["auto"]
    conn.commit()
    return counts


def _record_inbound(conn, msg: dict, gmail_id: str, rows: list, counts: dict,
                    fallback_address: str, bound_message=None) -> None:
    """Classify one inbound Gmail message against the outbound rows it may answer.

    `rows` are this prospect's sent messages, carrying the timestamps that decide
    whether the inbound is even possible as a reply (finding C2).
    """
    labels = set(msg.get("labelIds") or [])
    if labels & {"SENT", "DRAFT", "TRASH"}:
        return  # our own copy, never a reply
    headers = _headers(msg.get("payload") or {})
    text = message_text(msg)
    verdict = classify(headers.get("from", fallback_address),
                       headers.get("subject", ""), text, headers)
    thread_id = msg.get("threadId")
    anchor = bound_message or next(
        (r for r in rows if r["gmail_thread_id"] and r["gmail_thread_id"] == thread_id),
        None) or rows[0]
    prospect_id = anchor["prospect_id"]
    bound = anchor["message_id"] if (bound_message is not None or (
        anchor["gmail_thread_id"] and anchor["gmail_thread_id"] == thread_id)) else None
    received_at = _received_at(msg, headers)
    if verdict.is_human and not stats.reply_could_answer(
            received_at, anchor["created_at"], anchor["staged_at"], anchor["sent_at"]):
        verdict = Classification(verdict.classification, False,
                                 f"pre_send_carryover+{verdict.marker}")
        carryover = True
    else:
        carryover = False
    snippet = re.sub(r"\s+", " ", text).strip() or (msg.get("snippet") or "")
    new_id = _insert_reply(
        conn, prospect_id=prospect_id, message_id=bound, gmail_message_id=gmail_id,
        received_at=received_at, classification=verdict.classification,
        is_human=verdict.is_human, snippet=snippet,
        classified_by=verdict.classified_by,
    )
    if new_id is None:
        return
    if carryover:
        counts["carryover"] += 1
        return
    if verdict.is_human:
        counts["new_replies"] += 1
        _apply_side_effects(conn, prospect_id, "email", verdict.classification,
                            snippet, "engine")
        _mark_replied(conn, prospect_id, "engine", f"gmail:{gmail_id}")
        sender = sender_address(headers.get("from", "")) or fallback_address
        _remember_reply_address(conn, prospect_id, sender)
    elif verdict.classification == "bounce":
        counts["bounces"] += 1
        _suppress(conn, "email", fallback_address, "bounce", "reply_scan")
        _mark_bounced(conn, prospect_id, "engine", f"gmail:{gmail_id}")
    else:
        counts["auto"] += 1


def _remember_reply_address(conn, prospect_id: int, address: str | None) -> None:
    """PLAN §9 edge 36: the address they answered FROM becomes a reachable contact.

    `address_source='reply'` is what makes it stageable for Message 2 under G9 — a
    human demonstrably reads that mailbox, which is stronger evidence than any
    verifier produces.
    """
    if not address:
        return
    conn.execute(
        "INSERT OR IGNORE INTO contacts (prospect_id, person_key, channel, address, "
        "address_source, verify_verdict, verified_at) "
        "VALUES (?, ?, 'email', ?, 'reply', 'RISK', datetime('now'))",
        (prospect_id, address.lower(), address.lower()))


def _scan_threads(conn, service, targets: list, counts: dict) -> None:
    """Second pass over the threads we started. PLAN.md §9 edge 36.

    `from:<the address we wrote to>` cannot see an owner who answers from his personal
    Gmail: the reply is absent from the sheet, absent from the numerator, and G7 then
    refuses to let him be sent the demo link he asked for (finding M3). Thread
    membership is also the strongest provenance evidence available — a message inside
    the thread we started is, by construction, a response to it.

    Degrades silently when the service does not model threads (test doubles) — this is
    an additional pass, never the only one.
    """
    users = service.users()
    threads_api = getattr(users, "threads", None)
    if threads_api is None:
        return
    for row in targets:
        thread_id = row["gmail_thread_id"]
        if not thread_id:
            continue
        try:
            thread = threads_api().get(userId="me", id=thread_id,
                                       format="full").execute() or {}
        except Exception:  # noqa: BLE001 — one unreadable thread must not end the scan
            continue
        for msg in thread.get("messages") or []:
            gmail_id = msg.get("id")
            if not gmail_id:
                continue
            counts["scanned"] += 1
            _record_inbound(conn, msg, gmail_id, [row], counts,
                            fallback_address=row["address"], bound_message=row)


def _scan_bounces(conn, service, by_address: dict, newer_than_days: int,
                  limit: int) -> dict:
    """Second pass for daemon DSNs, which arrive from mailer-daemon, not the prospect.

    Without this the delivered denominator silently overstates: a message nobody
    ever received would still count as delivered.
    """
    counts = {"scanned": 0, "bounces": 0, "auto": 0}
    window = f" newer_than:{newer_than_days}d" if newer_than_days else ""
    ids: list[str] = []
    # Two queries rather than one `OR` group: Gmail accepts both, but a single
    # daemon term per query is the form every client and test double parses.
    for daemon in ("mailer-daemon", "postmaster"):
        try:
            ids.extend(_list_ids(service, f"from:{daemon}{window}", min(limit, 50)))
        except Exception:  # noqa: BLE001 — a fake without this query must not break the scan
            continue
    for gmail_id in dict.fromkeys(ids):
        msg = _get(service, gmail_id)
        counts["scanned"] += 1
        headers = _headers(msg.get("payload") or {})
        text = message_text(msg)
        failed = headers.get("x-failed-recipients", "")
        candidates = {a.lower() for a in _ADDR_RE.findall(f"{failed}\n{text}")}
        matches = [addr for addr in by_address if addr in candidates]
        if not matches:
            continue
        verdict = classify(headers.get("from", ""), headers.get("subject", ""),
                           text, headers)
        for address in matches:
            prospect_id = by_address[address][0]["prospect_id"]
            new_id = _insert_reply(
                conn, prospect_id=prospect_id, message_id=None,
                gmail_message_id=f"{gmail_id}:{address}",
                received_at=_received_at(msg, headers),
                classification=verdict.classification, is_human=False,
                snippet=re.sub(r"\s+", " ", text).strip(),
                classified_by=verdict.classified_by,
            )
            if new_id is None:
                continue
            if verdict.classification == "bounce":
                counts["bounces"] += 1
                _suppress(conn, "email", address, "bounce", "reply_scan")
                _mark_bounced(conn, prospect_id, "engine", f"gmail:{gmail_id}")
            else:
                counts["auto"] += 1
    return counts


def recent_window(days: int) -> str:
    """ISO timestamp `days` ago (UTC) — used by report/status queries."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


__all__ = [
    "ALL_CLASSES", "BOOKED_OUTCOMES", "Classification", "ClassificationError",
    "HUMAN_CLASSES", "MACHINE_CLASSES", "POST_BOOKING_STATE", "SNIPPET_MAX",
    "build_service", "classify", "demote_carryover", "ingest_triage",
    "is_human_class", "looks_unsubscribe", "message_text", "record_reply",
    "recent_window", "scan", "sender_address", "set_outcome", "transition",
]
