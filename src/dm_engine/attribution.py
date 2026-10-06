"""Demo-booking attribution. Contract: PLAN.md §12.6, §8.2, §8.3.

The demo form collects first name, phone, company name, company type, company
website — **no email**. There is therefore no natural exact join key between a
message we sent and a booking that appears in GHL. Everything here is an attempt to
bridge that gap without ever inventing a link, because a fabricated attribution is
strictly worse than an unattributed booking: it makes a variant look like a winner.

**A match is not a booking.** Identity ("this contact is that prospect") and event
("this contact represents a demo being booked") are two separate questions, and only
the second one belongs in the report's fourth column. PLAN.md §8.3 fixes the answer:
*booking signal = a form-submission contact (source/tag) or an appointment* — never a
plain CRM row. GHL holds ~1,600 contacts; without that test, any one of them created
in the 45-day window whose website or company+first-name lined up became a booking on
that prospect's variant, and the column was manufactured from CRM noise (finding C1).

Confidence ladder (PLAN.md §8.2), highest first — applied ONLY to contacts that
already carry a booking signal:

  utm               exact   `utm_content=<prospect_key>` we put on the Message-2 link
  domain_match      high    normalized form website == prospects.domain
  name_phone_match  high    STRICT company name AND (first name OR phone)
  manual_confirm    manual  everything ambiguous -> a DASHBOARD card, never auto-booked

Only `exact`/`high` are written automatically. Ambiguity (two prospects matching one
form), weak matches, and bookings we cannot place in time become human-confirm cards.
A GHL contact matching no prospect is organic and is never claimed (§9 edge 37) — but
"organic" now means *a booking by someone we never messaged*, not "everything left
over", because a real booking reported as organic is a lost signal (finding H4).

GHL is read from a **disk export only** — never live. All 30 open opportunities carry
`monetaryValue: 0` and a limit=100 `search_opportunities` response is ~61,500 chars,
so this module emits no revenue metric and makes no network call (§8.3, §14 non-goal 9).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import stats

#: Only a contact created after we first touched the prospect, within this window,
#: can be attributed to the outreach (PLAN.md §8.2).
ATTRIBUTION_WINDOW_DAYS = 45

CONFIDENCE_BY_METHOD = {
    "utm": "exact",
    "domain_match": "high",
    "name_phone_match": "high",
    "manual_confirm": "manual",
}
AUTO_METHODS = frozenset({"utm", "domain_match", "name_phone_match"})

_COMPANY_NOISE_RE = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|co|corp|corporation|ltd|limited|"
    r"landscap\w*|hardscap\w*|construction|contracting|contractors?|"
    r"design[\s\-]?build|outdoor\s+living|services?|company)\b", re.I)
#: Legal form only. Everything the ICP is actually named after — Hardscape,
#: Landscaping, Construction — is KEPT, because those words are the discriminating
#: part of a contractor's name, not noise (finding C1).
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|limited|"
    r"co|company|the)\b", re.I)
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

#: PLAN.md §8.3: "Booking signal = form-submission contact (source/tag) or an
#: appointment — NOT opportunity monetaryValue, NOT Closed Won."
_BOOKING_WORD_RE = re.compile(
    r"\b(form|forms|demo|booking|booked|book|appointment|appt|calendar|"
    r"schedul\w*|consult\w*|webform|opt[\s\-]?in|survey)\b", re.I)
#: Where an appointment shows up in the shapes GHL exports.
_APPOINTMENT_KEYS = ("appointments", "appointment", "calendarEvents", "events")
_APPOINTMENT_SCALAR_KEYS = ("appointmentId", "calendarId", "dateOfAppointment",
                            "appointmentStatus", "eventStartTime")


class AttributionError(RuntimeError):
    """The export file is unusable, or a booking was asked for on bad inputs."""


@dataclass(frozen=True)
class MatchCandidate:
    """One prospect<->GHL-contact link proposal. Contract: PLAN.md §12.6."""

    prospect_id: int
    ghl_contact_id: str
    method: str
    confidence: str
    evidence: str
    #: Every prospect this contact could be, when more than one is possible.
    #: `prospect_id` is the lowest of them purely because the index is ordered by
    #: id (import order), so for a manual candidate it must never read as a
    #: nomination — the card renders all of these (finding M2).
    alternatives: tuple[int, ...] = ()

    @property
    def auto(self) -> bool:
        return self.method in AUTO_METHODS and self.confidence in ("exact", "high")


class MatchResult(list):
    """The candidates `match()` produced, plus what it did with everything else.

    Subclasses `list` so the historical contract (`match(...) == []`, indexing,
    iteration) still holds. The extra buckets exist so that no category is ever
    inferred by subtraction: an export row is counted exactly once, in exactly one
    place, and a booking that could not be attributed is distinguishable from one
    that was genuinely organic (finding H4). `read` is the denominator every other
    count must reconcile against.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.read = 0                                  # contacts seen in the export
        self.unusable = 0                              # no id — cannot be referenced
        self.organic: list[str] = []                   # a real booking, never messaged
        self.predates: list[str] = []                  # existed before we could message
        #: (contact_id, matched_a_prospect) — carries a booking signal? no.
        self.no_booking_signal: list[tuple[str, bool]] = []

    @property
    def auto(self) -> list[MatchCandidate]:
        """Candidates written straight to `outcomes`."""
        return [candidate for candidate in self if candidate.auto]

    @property
    def cards(self) -> list[MatchCandidate]:
        """Candidates a human must resolve. Nothing here has been booked."""
        return [candidate for candidate in self if not candidate.auto]

    @property
    def accounted(self) -> int:
        """Every read contact lands in exactly one bucket; this must equal `read`."""
        return (len(self) + self.unusable + len(self.organic) + len(self.predates)
                + len(self.no_booking_signal))


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

def normalize_website(value: str | None) -> str | None:
    """'https://WWW.Acme Hardscape.com/contact?x=1' -> 'acme hardscape.com'."""
    if not value:
        return None
    text = str(value).strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1]
    if text.startswith("www."):
        text = text[4:]
    text = text.rstrip(".").strip()
    if not text or "." not in text:
        return None
    return text


def normalize_company(value: str | None) -> str:
    """Lowercase, strip punctuation and trade-suffix noise (PLAN.md §8.2 step 3)."""
    if not value:
        return ""
    text = _COMPANY_NOISE_RE.sub(" ", str(value).lower())
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def normalize_company_strict(value: str | None) -> str:
    """Punctuation + LEGAL suffix only. 'Acme Hardscape, LLC' -> 'acme hardscape'.

    `normalize_company` additionally strips the trade words, which collapses
    'Acme Hardscape LLC', 'Acme Landscaping Inc' and 'Acme Construction Co' all
    onto 'acme'. That looseness is fine for *proposing* a human-confirm card and
    catastrophic for an automatic booking: it is how a company the operator never messaged
    got attributed to a different company that shares an owner first name (finding C1).
    """
    if not value:
        return ""
    text = _LEGAL_SUFFIX_RE.sub(" ", str(value).lower())
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def booking_signal(contact: dict) -> str | None:
    """The explicit evidence that this contact represents a booking, or None (§8.3).

    A contact that merely matches a prospect is a contact. Returning None here means
    "this is not a booking", and nothing downstream may promote it into one.
    """
    for key in _APPOINTMENT_KEYS:
        value = contact.get(key)
        if value:
            return f"appointment:{key}"
    for key in _APPOINTMENT_SCALAR_KEYS:
        if contact.get(key):
            return f"appointment:{key}"
    for key in ("source", "contactSource", "sourceType"):
        source = str(contact.get(key) or "").strip().lower()
        if source and _BOOKING_WORD_RE.search(source):
            return f"{key}={source}"
    tags = contact.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, dict):
        tags = list(tags.values())
    for tag in tags:
        text = str(tag).strip().lower()
        if text and _BOOKING_WORD_RE.search(text):
            return f"tag={text}"
    return None


def normalize_phone(value: str | None) -> str:
    """Digits only, US country code dropped. '' when there is nothing usable."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 7 else ""


def extract_utm_content(contact: dict) -> str | None:
    """Pull `utm_content` out of whatever shape GHL used for attribution."""
    direct = contact.get("utm_content") or contact.get("utmContent")
    if direct:
        return str(direct).strip().lower()
    attributions = contact.get("attributions") or contact.get("attributionSource") or []
    if isinstance(attributions, dict):
        attributions = [attributions]
    for attribution in attributions:
        if not isinstance(attribution, dict):
            continue
        for key in ("utmContent", "utm_content"):
            if attribution.get(key):
                return str(attribution[key]).strip().lower()
        url = attribution.get("url") or attribution.get("referrer") or ""
        match = re.search(r"[?&]utm_content=([^&#\s]+)", str(url), re.I)
        if match:
            return match.group(1).strip().lower()
    return None


def _parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(text)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# export reading (disk only, chunked)
# --------------------------------------------------------------------------- #

def load_ghl_export(path: Path) -> Iterator[dict]:
    """Yield contact dicts from a GHL export written to disk.

    Accepts a JSON array, `{"contacts": [...]}`, or JSON Lines (one contact per
    line, preferred for large exports — it streams instead of loading the whole
    document). Never contacts GHL: the MCP response overflows context (§8.3).
    """
    path = Path(path)
    if not path.exists():
        raise AttributionError(f"GHL export not found: {path}")
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AttributionError(
                        f"{path}:{line_no} is not valid JSON: {exc}") from exc
                if isinstance(record, dict):
                    yield record
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AttributionError(f"{path} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("contacts") or data.get("items") or []
    if not isinstance(data, list):
        raise AttributionError(f"{path}: expected a list of contacts, got {type(data).__name__}")
    for record in data:
        if isinstance(record, dict):
            yield record


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

def _load_prospect_index(conn) -> tuple[list[dict], dict]:
    """Prospects whose message was actually DELIVERED, plus their attribution anchor.

    Two provenance rules live in this query:

    * BOUNCED is not in the eligible population. A prospect whose email hard-bounced
      never read anything, so no booking can have come from it — crediting one was
      finding C1's quietest symptom.
    * The anchor is the earliest moment the message could have left (`stats.send_floor`
      — staged, else sent, else created), preferring the Message-2 row because that is
      the message carrying the demo link. `sent_at` alone would be an anchor stamped
      whenever reconcile happened to run, which discards every booking that landed
      inside that lag (finding H4).
    """
    rows = conn.execute(
        "SELECT p.id, p.prospect_key, p.company_name, p.domain, p.owner_first_name, "
        "       MAX(CASE WHEN m.sequence_step = 2 THEN m.staged_at END) AS s2_staged, "
        "       MAX(CASE WHEN m.sequence_step = 2 THEN m.sent_at END) AS s2_sent, "
        "       MAX(CASE WHEN m.sequence_step = 2 THEN m.created_at END) AS s2_created, "
        "       MIN(CASE WHEN m.sequence_step = 1 THEN m.staged_at END) AS s1_staged, "
        "       MIN(CASE WHEN m.sequence_step = 1 THEN m.sent_at END) AS s1_sent, "
        "       MIN(CASE WHEN m.sequence_step = 1 THEN m.created_at END) AS s1_created "
        "FROM prospects p JOIN messages m ON m.prospect_id = p.id "
        f"WHERE m.state IN ({', '.join('?' for _ in stats.DELIVERED_STATES)}) "
        "GROUP BY p.id ORDER BY p.id", stats.DELIVERED_STATES).fetchall()
    index = []
    for row in rows:
        anchor = (stats.send_floor(row["s2_created"], row["s2_staged"], row["s2_sent"])
                  or stats.send_floor(row["s1_created"], row["s1_staged"],
                                      row["s1_sent"]))
        index.append({
            "id": row["id"],
            "prospect_key": (row["prospect_key"] or "").lower(),
            "company_norm": normalize_company(row["company_name"]),
            "company_strict": normalize_company_strict(row["company_name"]),
            "domain": normalize_website(row["domain"]),
            "first_name": (row["owner_first_name"] or "").strip().lower(),
            "anchor": anchor,
        })
    phones = {}
    for row in conn.execute(
            "SELECT prospect_id, address FROM contacts WHERE channel = 'sms'"):
        digits = normalize_phone(row["address"])
        if digits:
            phones.setdefault(row["prospect_id"], set()).add(digits)
    return index, phones


def _in_window(prospect: dict, added: datetime | None) -> bool:
    anchor = prospect["anchor"]
    if anchor is None:
        return False
    if added is None:
        return False
    return anchor <= added <= anchor + timedelta(days=ATTRIBUTION_WINDOW_DAYS)


def _predates(prospect: dict, added: datetime) -> bool:
    """True when the contact existed before we could possibly have messaged them.

    The anchor is `stats.send_floor` — the EARLIEST moment the message could have
    left — so a True here means the booking cannot have been caused by this outreach,
    rather than merely that reconcile stamped `sent_at` late (finding H4).
    """
    anchor = prospect["anchor"]
    return anchor is not None and added < anchor


def contact_added_at(contact: dict) -> datetime | None:
    """When GHL created the contact, or None when the export did not say."""
    return _parse_ts(contact.get("dateAdded") or contact.get("createdAt")
                     or contact.get("date_added"))


def _candidates_for(contact: dict, index: list[dict],
                    phones: dict) -> list[tuple[dict, str, str]]:
    """Return [(prospect, method, evidence)] ordered by confidence. IDENTITY only.

    Deliberately does NOT filter by the attribution window: "who is this" and "did it
    happen in a period we can claim" are separate questions, and collapsing them is
    how a real, in-window-but-for-reconcile-lag booking became `unmatched(organic)`
    with no card and no distinct count (finding H4). `match()` applies the window.
    """
    utm = extract_utm_content(contact)
    if utm:
        hits = [p for p in index if p["prospect_key"] and p["prospect_key"] == utm]
        if hits:
            return [(p, "utm", f"utm_content={utm}") for p in hits]

    site = normalize_website(contact.get("website") or contact.get("companyWebsite"))
    if site:
        hits = [p for p in index if p["domain"] and p["domain"] == site]
        if hits:
            return [(p, "domain_match", f"website={site}") for p in hits]

    raw_company = contact.get("companyName") or contact.get("company")
    strict = normalize_company_strict(raw_company)
    loose = normalize_company(raw_company)
    if not (strict or loose):
        return []

    first = (contact.get("firstName") or contact.get("first_name") or "").strip().lower()
    digits = normalize_phone(contact.get("phone"))
    hits = []
    for prospect in index:
        # the AUTO tier requires the strict company name: 'Acme Landscaping' and
        # 'Acme Hardscape' are two different companies, and a shared owner first
        # name is not evidence that they are one (finding C1)
        if not prospect["company_strict"] or prospect["company_strict"] != strict:
            continue
        name_ok = bool(first) and first == prospect["first_name"]
        phone_ok = bool(digits) and digits in phones.get(prospect["id"], set())
        if name_ok or phone_ok:
            detail = "first_name" if name_ok else "phone"
            hits.append((prospect, "name_phone_match", f"company={strict}+{detail}"))
    if hits:
        return hits

    # Anything the loose form can reach — same company after trade words are stripped,
    # or a strict match nothing corroborated — is a proposal for a human, never a
    # booking.
    weak = []
    for prospect in index:
        if strict and prospect["company_strict"] == strict:
            weak.append((prospect, "manual_confirm",
                         f"company={strict} (nothing corroborated it)"))
        elif loose and prospect["company_norm"] == loose:
            weak.append((prospect, "manual_confirm",
                         f"company~{loose} (trade words stripped — "
                         f"'{raw_company}' vs a different trade)"))
    return weak


def match(conn, ghl_export_path: Path, cfg=None, actor: str = "engine") -> MatchResult:
    """Match a GHL export against messaged prospects. Contract: PLAN.md §12.6.

    Writes `outcomes` rows for `exact`/`high` matches only, and only for contacts that
    carry an explicit booking signal. Ambiguous matches, weak matches, and bookings
    that cannot be placed in the attribution window come back with
    `method='manual_confirm'` and, when `cfg` is supplied, are written as DASHBOARD
    approvals cards for the operator to confirm with `dmctl book`.

    Returns a `MatchResult` — a list of candidates (the historical contract) that also
    accounts for every contact producing none, so no bucket is inferred by subtraction.

    `cfg` is optional so the positional contract `match(conn, path)` still holds; the
    only thing it enables is card writing.
    """
    index, phones = _load_prospect_index(conn)
    result = MatchResult()
    for contact in load_ghl_export(ghl_export_path):
        result.read += 1
        contact_id = str(contact.get("id") or contact.get("contactId") or "").strip()
        if not contact_id:
            result.unusable += 1
            continue

        signal = booking_signal(contact)
        hits = _candidates_for(contact, index, phones)
        if signal is None:
            # Not a booking at all. It may well BE one of our prospects — that is not
            # the question. §8.3: a booking is a form submission or an appointment.
            result.no_booking_signal.append(
                (contact_id, bool(hits)))
            continue
        if not hits:
            # a real booking by someone we never messaged (§9 edge 37)
            result.organic.append(contact_id)
            continue

        added = contact_added_at(contact)
        in_window = [hit for hit in hits if _in_window(hit[0], added)]
        if not in_window:
            if added is not None and all(_predates(hit[0], added) for hit in hits):
                # Causally impossible: the contact existed before the earliest moment
                # any message to this prospect could have left. `anchor` is
                # `stats.send_floor`, not `sent_at`, so the reconcile lag that finding
                # H4 describes cannot push a real booking into this branch. Counted,
                # never carded — with ~1,600 contacts in GHL, carding every historical
                # one that happens to match would bury the real confirms.
                result.predates.append(contact_id)
                continue
            # A booking we cannot place in time is NOT organic and must not be
            # reported as one (finding H4): an export with no `dateAdded` used to
            # zero the entire run silently, and a booking just past the window may
            # still be ours. Neither is a machine's call to make.
            reason = ("the export carries no dateAdded/createdAt, so it cannot be "
                      "placed in the attribution window"
                      if added is None else
                      f"created {added.isoformat()}, outside the "
                      f"{ATTRIBUTION_WINDOW_DAYS}-day window after the send")
            result.append(_manual(hits, contact_id,
                                  f"booking signal {signal}; unattributed: {reason}"))
            continue
        if len(in_window) > 1:
            # two prospects, one form: never auto-pick (§9 edge 38)
            result.append(_manual(in_window, contact_id,
                                  f"booking signal {signal}; ambiguous between "
                                  f"{len(in_window)} messaged prospects"))
            continue
        prospect, method, evidence = in_window[0]
        result.append(MatchCandidate(
            prospect["id"], contact_id, method, CONFIDENCE_BY_METHOD[method],
            f"{evidence}; booking signal {signal}"))

    for candidate in result:
        if candidate.auto:
            book(conn, candidate.prospect_id, candidate.method,
                 f"ghl:{candidate.ghl_contact_id} {candidate.evidence}", actor,
                 ghl_contact_id=candidate.ghl_contact_id)
    conn.commit()

    if cfg is not None and result.cards:
        _write_manual_cards(conn, cfg, result.cards)
    return result


def _manual(hits: list[tuple[dict, str, str]], contact_id: str,
            evidence: str) -> MatchCandidate:
    """A candidate no machine may resolve. Carries EVERY prospect it could be.

    `prospect_id` is the lowest id purely because the index is ordered by id, which is
    import order — so it must never read as a nomination. `alternatives` is what the
    card renders, and the card names all of them (finding M2).
    """
    ids = tuple(hit[0]["id"] for hit in hits)
    return MatchCandidate(ids[0], contact_id, "manual_confirm", "manual",
                          evidence, alternatives=ids)


def _write_manual_cards(conn, cfg, manual: list[MatchCandidate]) -> None:
    from . import report  # noqa: PLC0415 — avoids an import cycle at module load

    for candidate in manual:
        row = conn.execute(
            "SELECT p.company_name, p.market, a.variant_id FROM prospects p "
            "LEFT JOIN assignments a ON a.prospect_id = p.id WHERE p.id = ?",
            (candidate.prospect_id,)).fetchone()
        company = row["company_name"] if row else f"prospect {candidate.prospect_id}"
        variant = (row["variant_id"] if row else None) or "unassigned"
        market = (row["market"] if row else None) or "unknown market"
        report.write_approvals_card(
            cfg,
            card_id=f"dmengine-book-{candidate.ghl_contact_id}",
            title=f"Confirm demo booking: {company}",
            summary=(
                f"GHL contact {candidate.ghl_contact_id} may be prospect "
                f"#{candidate.prospect_id} ({company}, {variant}, {market}). "
                f"Evidence: {candidate.evidence}. Nothing has been booked — confidence "
                f"was below the automatic bar, and a fabricated attribution would make "
                f"a variant look like a winner. Confirm with: dmctl book "
                f"{candidate.prospect_id} --method manual_confirm --evidence "
                f"{candidate.ghl_contact_id}"
            ),
            link=f"ghl:contact:{candidate.ghl_contact_id}",
        )


def book(conn, prospect_id: int, method: str, evidence: str, actor: str,
         outcome: str = "demo_booked", ghl_contact_id: str | None = None) -> None:
    """Record a booking with its attribution method and confidence (SC-7).

    Every booking carries a method and evidence — there is no path that writes a
    booking without saying how it was established.
    """
    if method not in CONFIDENCE_BY_METHOD:
        raise AttributionError(
            f"unknown attribution method {method!r}; allowed: "
            f"{sorted(CONFIDENCE_BY_METHOD)}")
    if outcome not in ("demo_booked", "call_booked"):
        raise AttributionError(f"book() records bookings, not {outcome!r}")
    if not evidence:
        raise AttributionError("a booking without evidence is a guess — refusing")
    confidence = CONFIDENCE_BY_METHOD[method]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT outcome, attribution_method FROM outcomes WHERE prospect_id = ?",
        (prospect_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO outcomes (prospect_id, outcome, demo_booked_at, "
            "attribution_method, attribution_confidence, ghl_contact_id, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (prospect_id, outcome, now, method, confidence, ghl_contact_id, evidence))
        from_state = None
    else:
        if existing["outcome"] in ("demo_booked", "call_booked"):
            return  # already booked; never overwrite a recorded attribution
        conn.execute(
            "UPDATE outcomes SET outcome = ?, demo_booked_at = ?, attribution_method = ?, "
            "attribution_confidence = ?, ghl_contact_id = COALESCE(?, ghl_contact_id), "
            "evidence = ?, updated_at = datetime('now') WHERE prospect_id = ?",
            (outcome, now, method, confidence, ghl_contact_id, evidence, prospect_id))
        from_state = f"outcome:{existing['outcome']}"
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("prospect", str(prospect_id), from_state, f"outcome:{outcome}", actor,
         f"attribution:{method}/{confidence}", evidence))
    conn.commit()


__all__ = [
    "ATTRIBUTION_WINDOW_DAYS", "AUTO_METHODS", "AttributionError",
    "CONFIDENCE_BY_METHOD", "MatchCandidate", "book", "extract_utm_content",
    "load_ghl_export", "match", "normalize_company", "normalize_phone",
    "normalize_website",
]
