"""Prospect intake: normalize, dedup, identity, timezone. Contract: PLAN.md §12.3.

Identity is the load-bearing idea here. `prospect_key` is what makes a company the
same company across markets, batches and scrapers, and it is also the variant
assignment key (§4.3) — so a sloppy key silently corrupts the experiment, not just
the address book. Two rules:

  * a company WITH a website is keyed by its normalized domain
  * a company WITHOUT one is keyed `name:<company-slug>:<market-slug>`

Accounting invariant (§10 row (i)): every record in the input file lands in exactly
one of `added`, `dupes`, `skips`, `errors`. Nothing is silently dropped, and
`added + dupes + len(skips) + len(errors) == len(records)` always holds.

Interface notes for other owners:
  * `hook_fact` has no column in the schema. Per §5.3 it is stored on the tail of
    `prospects.icp_fit_note` as a final `hook_fact: <text>` line. Read it back with
    `parse_hook_fact(row['icp_fit_note'])` — compose.py [C] needs this.
  * `qualify()`/`exclude()` write their own `state_log` rows with plain SQL rather
    than calling `state.transition()` [D]: prospects.py must import cleanly while D
    is still being built, and state.py's transition table is message-shaped.
"""

import json
import re
import socket
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as _timezone   # `timezone` is a local name below
from pathlib import Path
from urllib.parse import urlsplit

from . import suppression

#: 50 states + DC. Territory is national (§9 edge 31), so a missing entry means a
#: message could be queued for 4am prospect-local — the map raises rather than
#: guessing. Multi-zone states map to the zone holding most of the population.
US_STATE_TZ: dict[str, str] = {
    "AL": "America/Chicago",     "AK": "America/Anchorage",
    "AZ": "America/Phoenix",     "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York",    "DE": "America/New_York",
    "DC": "America/New_York",    "FL": "America/New_York",
    "GA": "America/New_York",    "HI": "Pacific/Honolulu",
    "ID": "America/Boise",       "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",     "KS": "America/Chicago",
    "KY": "America/New_York",    "LA": "America/Chicago",
    "ME": "America/New_York",    "MD": "America/New_York",
    "MA": "America/New_York",    "MI": "America/Detroit",
    "MN": "America/Chicago",     "MS": "America/Chicago",
    "MO": "America/Chicago",     "MT": "America/Denver",
    "NE": "America/Chicago",     "NV": "America/Los_Angeles",
    "NH": "America/New_York",    "NJ": "America/New_York",
    "NM": "America/Denver",      "NY": "America/New_York",
    "NC": "America/New_York",    "ND": "America/Chicago",
    "OH": "America/New_York",    "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York",    "SC": "America/New_York",
    "SD": "America/Chicago",     "TN": "America/Chicago",
    "TX": "America/Chicago",     "UT": "America/Denver",
    "VT": "America/New_York",    "VA": "America/New_York",
    "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago",     "WY": "America/Denver",
}

#: Address provenance classes the schema allows (contacts.address_source CHECK).
VALID_ADDRESS_SOURCES = {
    "website", "reply", "linkedin", "directory", "apollo", "manual", "format_guess",
}
#: An unrecognised provenance string is coerced to the most distrusted class rather
#: than dropped — G9 then blocks a RISK verdict from it, which is the safe direction.
FALLBACK_ADDRESS_SOURCE = "format_guess"

#: Marker for the hook fact stashed on icp_fit_note (§5.3).
HOOK_FACT_PREFIX = "hook_fact: "

#: Legal-suffix noise stripped before name comparison and name-slug keys. Same set
#: §8.2 uses for GHL name matching, so the two agree on what "the same company" is.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|co|corp|corporation|ltd|limited|llp|pllc|pc)\b\.?",
    re.IGNORECASE,
)
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))
#: Control characters and internal whitespace are never part of an address. A
#: newline in particular survives all the way to `suppression.append_to_file`,
#: where it would add a second bare line to the shared list and silently suppress
#: an unrelated address for create_drafts.py.
_UNSAFE_ADDRESS_RE = re.compile(r"[\x00-\x20\x7f]")


class ProspectFileError(ValueError):
    """The import file itself is unusable (unparseable, or not a JSON array)."""


@dataclass
class ImportResult:
    added: int = 0
    dupes: int = 0
    skips: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: non-fatal, sub-record observations (a dropped address, a coerced source).
    #: Additive to the §12.3 contract; the four fields above keep their order.
    notes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.dupes + len(self.skips) + len(self.errors)


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

def normalize_domain(url_or_domain: str | None) -> str | None:
    """'https://WWW.X.com/services?utm_source=z' -> 'x.com'. None when not a domain.

    Handles the shapes prospecting actually produces: bare domains, scheme-less URLs
    with paths, tracking suffixes, uppercase, trailing dots, ports, credentials, and
    plain email addresses (the domain part wins). A company name with no website
    returns None — that is the signal to fall back to a name-slug key.
    """
    if url_or_domain is None:
        return None
    raw = str(url_or_domain).translate(_ZERO_WIDTH).strip().strip("<>").strip()
    if not raw:
        return None
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    if "://" not in raw:
        raw = "//" + raw.lstrip("/")
    try:
        host = urlsplit(raw).netloc
    except ValueError:
        return None
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0].strip().strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or not _HOST_RE.fullmatch(host):
        return None
    labels = host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return None
    if len(labels[-1]) < 2 or not labels[-1].isalpha():
        return None
    return host


def slugify(text: str) -> str:
    """Lowercase ASCII slug: 'Pinecrest Patio Co.' -> 'pinecrest-patio-co'."""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", folded)).strip("-")


def normalize_company_name(company_name: str) -> str:
    """Comparison form: lowercased, punctuation and legal suffixes removed."""
    stripped = _LEGAL_SUFFIX_RE.sub(" ", str(company_name))
    stripped = re.sub(r"[^A-Za-z0-9]+", " ", stripped).strip().lower()
    return re.sub(r"\s+", " ", stripped) or str(company_name).strip().lower()


def prospect_key(company_name: str, domain: str | None, market: str) -> str:
    """THE identity key: normalized domain, else 'name:<company>:<market>'."""
    normalized = normalize_domain(domain)
    if normalized:
        return normalized
    company_slug = slugify(normalize_company_name(company_name)) or slugify(company_name)
    if not company_slug:
        raise ValueError(f"cannot build a prospect_key from company_name={company_name!r}")
    return f"name:{company_slug}:{slugify(market)}"


def normalize_email(address: str) -> str | None:
    """Lowercase + trim. The STORED form stays exactly what we would send to.

    No dot/plus folding here on purpose: the address on the draft must be the
    address a human saw on the website. Folding for SUPPRESSION MATCHING is a
    different question and lives in `suppression.canonical_email` (and, for the
    SQL belt, `contacts.address_canon`).

    Rejects control characters and embedded whitespace outright: an address with
    an embedded newline used to pass through, and one such address reaching the shared
    suppression_list.txt injects a second bare line into a file another tool
    reads as "one address per line".
    """
    cleaned = str(address or "").translate(_ZERO_WIDTH).strip().strip("<>").strip().lower()
    if cleaned.startswith("mailto:"):
        cleaned = cleaned[7:]
    if _UNSAFE_ADDRESS_RE.search(cleaned):
        return None
    if cleaned.count("@") != 1:
        return None
    local, _, host = cleaned.partition("@")
    if not local or not normalize_domain(host):
        return None
    return f"{local}@{normalize_domain(host)}"


def linkedin_slug(profile_url: str) -> str | None:
    """'https://www.linkedin.com/in/alex-x/' -> 'alex-x' (the person key)."""
    raw = str(profile_url or "").translate(_ZERO_WIDTH).strip().lower().rstrip("/")
    match = re.search(r"/(?:in|pub)/([^/?#]+)", raw)
    if match:
        return match.group(1)
    return raw.rsplit("/", 1)[-1] or None


#: Shortest number that can be a real phone. Matches attribution.normalize_phone,
#: which returns '' below it.
MIN_PHONE_DIGITS = 7


def normalize_whatsapp(number: str) -> str | None:
    """'+1 (602) 555-0134' -> '+16025550134'. None when it is not a phone number.

    Unlike an email address — where the STORED form is deliberately the exact string
    a human read off a website — a phone number has one canonical written form, and
    storing it is what makes `UNIQUE (channel, address)` dedup two spellings of the
    same number. A bare 10-digit US number gains the country code; anything longer is
    assumed to already carry one. Matching still goes through
    `suppression.canonical_phone` / `contacts.address_canon`, so a number stored in
    some other shape is still caught.
    """
    digits = suppression.canonical_phone(
        str(number or "").translate(_ZERO_WIDTH).strip())
    if not digits.isdigit() or len(digits) < MIN_PHONE_DIGITS:
        return None
    return f"+1{digits}" if len(digits) == 10 else f"+{digits}"


def messenger_handle(page_url: str) -> str | None:
    """'https://m.me/example-hardscape/' -> 'example-hardscape' (the person key)."""
    handle = suppression.canonical_messenger(
        str(page_url or "").translate(_ZERO_WIDTH).strip())
    return handle or None


def us_state_tz(us_state: str) -> str:
    """2-letter US state (or DC) -> IANA timezone. Raises KeyError on anything else."""
    key = str(us_state or "").strip().upper()
    return US_STATE_TZ[key]


def parse_hook_fact(icp_fit_note: str | None) -> str | None:
    """Read back the hook fact stored on the tail of icp_fit_note (§5.3)."""
    for line in reversed(str(icp_fit_note or "").splitlines()):
        if line.startswith(HOOK_FACT_PREFIX):
            return line[len(HOOK_FACT_PREFIX):].strip() or None
    return None


def _compose_note(icp_fit_note: str | None, hook_fact: str | None) -> str | None:
    note = (icp_fit_note or "").strip()
    fact = (hook_fact or "").strip()
    if not fact:
        return note or None
    return f"{note}\n{HOOK_FACT_PREFIX}{fact}".strip()


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #

def _load_records(path: Path) -> list:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProspectFileError(f"cannot read prospect file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProspectFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ProspectFileError(f"{path} must contain a JSON array of prospects")
    return data


def _record_label(record, index: int) -> str:
    if isinstance(record, dict):
        name = record.get("company_name") or record.get("website_url")
        if name:
            return str(name)
    return f"record[{index}]"


def _collect_addresses(record: dict) -> tuple[list[dict], list[str]]:
    """Return ([{channel, address, person_key, source}], [warnings])."""
    out, warnings = [], []
    for entry in record.get("emails") or []:
        raw = entry.get("address") if isinstance(entry, dict) else entry
        source = entry.get("source") if isinstance(entry, dict) else None
        address = normalize_email(raw)
        if not address:
            warnings.append(f"unusable email {raw!r}")
            continue
        out.append({"channel": "email", "address": address, "person_key": address,
                    "source": _address_source(source, warnings)})
    for entry in record.get("linkedin") or []:
        raw = entry.get("address") if isinstance(entry, dict) else entry
        source = entry.get("source") if isinstance(entry, dict) else "linkedin"
        slug = linkedin_slug(raw)
        if not slug:
            warnings.append(f"unusable linkedin url {raw!r}")
            continue
        out.append({"channel": "linkedin", "address": str(raw).strip(), "person_key": slug,
                    "source": _address_source(source, warnings)})
    # The two DM channels. `source` is NOT defaulted to a verified class here: an
    # absent source falls through `_address_source` to `format_guess`, the most
    # distrusted value, which is the safe direction.
    for entry in record.get("whatsapp") or []:
        raw = entry.get("address") if isinstance(entry, dict) else entry
        source = entry.get("source") if isinstance(entry, dict) else None
        number = normalize_whatsapp(raw)
        if not number:
            warnings.append(f"unusable whatsapp number {raw!r}")
            continue
        out.append({"channel": "whatsapp", "address": number,
                    "person_key": suppression.canonical_phone(number),
                    "source": _address_source(source, warnings)})
    for entry in record.get("messenger") or []:
        raw = entry.get("address") if isinstance(entry, dict) else entry
        source = entry.get("source") if isinstance(entry, dict) else None
        handle = messenger_handle(raw)
        if not handle:
            warnings.append(f"unusable messenger url {raw!r}")
            continue
        out.append({"channel": "messenger", "address": str(raw).strip(),
                    "person_key": handle,
                    "source": _address_source(source, warnings)})
    return out, warnings


def _address_source(source: str | None, warnings: list[str]) -> str:
    value = str(source or "").strip().lower()
    if value in VALID_ADDRESS_SOURCES:
        return value
    if value:
        warnings.append(f"unknown address source {source!r} -> {FALLBACK_ADDRESS_SOURCE}")
    return FALLBACK_ADDRESS_SOURCE


def _insert_contacts(conn: sqlite3.Connection, prospect_id: int,
                     addresses: list[dict]) -> int:
    added = 0
    for item in addresses:
        cur = conn.execute(
            "INSERT OR IGNORE INTO contacts (prospect_id, person_key, channel, address, "
            "address_source) VALUES (?, ?, ?, ?, ?)",
            (prospect_id, item["person_key"], item["channel"], item["address"],
             item["source"]),
        )
        added += cur.rowcount or 0
    return added


def import_prospects(conn: sqlite3.Connection, path: Path, market: str,
                     *, source: str = "manual",
                     force_keys: set[str] | None = None) -> ImportResult:
    """Import the §12.7 prospect file. Update-not-insert on a known prospect_key.

    `force_keys` resolves edge 4 (two companies sharing one domain): a key listed
    there merges into the existing row despite the company-name mismatch.
    """
    records = _load_records(path)
    force_keys = force_keys or set()
    result = ImportResult()

    for index, record in enumerate(records):
        label = _record_label(record, index)
        if not isinstance(record, dict):
            result.errors.append((label, "record is not a JSON object"))
            continue
        company_name = str(record.get("company_name") or "").strip()
        if not company_name:
            result.errors.append((label, "missing required field company_name"))
            continue
        us_state = str(record.get("us_state") or "").strip()
        if not us_state:
            result.errors.append((label, "missing required field us_state"))
            continue
        try:
            timezone = us_state_tz(us_state)
        except KeyError:
            result.errors.append(
                (label, f"us_state {us_state!r} is not a US state — no timezone mapping")
            )
            continue

        website_url = record.get("website_url") or None
        domain = normalize_domain(website_url)
        if website_url and not domain:
            result.notes.append((label, f"website_url {website_url!r} is not a usable domain"))
        try:
            key = prospect_key(company_name, domain, market)
        except ValueError as exc:
            result.errors.append((label, str(exc)))
            continue

        addresses, warnings = _collect_addresses(record)
        for warning in warnings:
            result.notes.append((label, warning))

        blocked = _first_suppressed(conn, addresses)
        if blocked:
            result.skips.append((label, blocked))
            continue
        blocked = _suppressed_company(conn, key, domain)
        if blocked:
            result.skips.append((label, blocked))
            continue

        try:
            with conn:
                existing = conn.execute(
                    "SELECT * FROM prospects WHERE prospect_key = ?", (key,)
                ).fetchone()
                if existing is None:
                    _insert_prospect(conn, record, key, company_name, domain, website_url,
                                     market, us_state, timezone, source)
                    outcome = "added"
                else:
                    outcome = _merge_prospect(conn, existing, record, company_name,
                                              domain, website_url, market, key, force_keys)
                prospect_id = conn.execute(
                    "SELECT id FROM prospects WHERE prospect_key = ?", (key,)
                ).fetchone()["id"]
                new_contacts = _insert_contacts(conn, prospect_id, addresses)
        except _MergeConflict as exc:
            result.errors.append((label, str(exc)))
            continue
        except sqlite3.DatabaseError as exc:
            result.errors.append((label, f"database refused the row: {exc}"))
            continue

        if outcome == "added":
            result.added += 1
            twin = _name_key_twin(conn, key, domain)
            if twin:
                result.notes.append((label, twin))
        else:
            result.dupes += 1
        if not addresses:
            result.notes.append((label, "no usable contact address — prospect stored unreachable"))
        elif not new_contacts and outcome != "added":
            result.notes.append((label, "contacts already known"))

    return result


class _MergeConflict(ValueError):
    """Two different companies claim one prospect_key (§9 edge 4)."""


def _name_key_twin(conn: sqlite3.Connection, key: str, domain: str | None) -> str | None:
    """Flag a website-less company that already exists under a different market.

    A name-slug key is deliberately market-scoped (§3.2) so two unrelated 'Pinecrest
    Patio' companies in different cities stay separate — the cost is that ONE company
    re-imported under a different market label lands twice, and G11's one-Message-1
    guard is per prospect row. The engine will not guess which case it is, so it says
    so out loud and leaves the merge to a human.
    """
    if domain or not key.startswith("name:"):
        return None
    prefix = key.rsplit(":", 1)[0]
    twin = conn.execute(
        "SELECT id, market FROM prospects WHERE prospect_key LIKE ? AND prospect_key != ?",
        (f"{prefix}:%", key),
    ).fetchone()
    if twin is None:
        return None
    return (
        f"no website, so the key is market-scoped ({key}); prospect #{twin['id']} in "
        f"{twin['market']} shares the company slug — merge them by hand if it is the "
        "same company"
    )


def _first_suppressed(conn: sqlite3.Connection, addresses: list[dict]) -> str | None:
    """Fail closed: ONE suppressed address blocks the whole company."""
    for item in addresses:
        reason = suppression.is_suppressed(
            conn, item["channel"], item["address"], item["person_key"]
        )
        if reason:
            return f"suppressed: {item['address']} ({reason})"
    return None


def _suppressed_company(conn: sqlite3.Connection, key: str,
                        domain: str | None) -> str | None:
    """Refuse a record whose company — or the same company's TWIN row — said stop.

    The address check above only sees the addresses in THIS record. A company that
    unsubscribed from `alex@` and is re-imported with a freshly scraped `info@`
    passes it, and if the company has no website it lands on a second,
    market-scoped `name:<slug>:<market>` row where G11 sees a clean slate. That is
    the exact path a reviewer walked to get a Message 1 to a company that had
    already unsubscribed. Suppression is recorded on the company, so this reads it
    back on the company.
    """
    candidates: list[sqlite3.Row] = []
    row = conn.execute(
        "SELECT id, company_name, state FROM prospects WHERE prospect_key = ?",
        (key,)).fetchone()
    if row is not None:
        candidates.append(row)
    if domain:
        candidates += conn.execute(
            "SELECT id, company_name, state FROM prospects WHERE domain = ? "
            "AND prospect_key != ?", (domain, key)).fetchall()
    elif key.startswith("name:"):
        prefix = key.rsplit(":", 1)[0]
        candidates += conn.execute(
            "SELECT id, company_name, state FROM prospects WHERE prospect_key LIKE ? "
            "AND prospect_key != ?", (f"{prefix}:%", key)).fetchall()
    for candidate in candidates:
        if candidate["state"] == "SUPPRESSED":
            return (f"suppressed company: prospect #{candidate['id']} "
                    f"({candidate['company_name']}) is SUPPRESSED — this company "
                    "asked to stop. Importing it under a second key would hand it a "
                    "fresh Message 1.")
    return None


def _insert_prospect(conn, record, key, company_name, domain, website_url,
                     market, us_state, timezone, source) -> None:
    conn.execute(
        "INSERT INTO prospects (prospect_key, company_name, company_casual, domain, "
        "website_url, market, us_state, timezone, owner_first_name, owner_last_name, "
        "icp_fit_note, source, source_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, company_name, _clean(record.get("company_casual")), domain,
         _clean(website_url), market, us_state.upper(), timezone,
         _clean(record.get("owner_first_name")), _clean(record.get("owner_last_name")),
         _compose_note(record.get("icp_fit_note"), record.get("hook_fact")),
         _clean(record.get("source")) or source, _clean(record.get("source_detail"))),
    )


def _merge_prospect(conn, existing, record, company_name, domain, website_url,
                    market, key, force_keys) -> str:
    """Enrich an existing row without ever overwriting a known value."""
    if (normalize_company_name(existing["company_name"]) != normalize_company_name(company_name)
            and key not in force_keys):
        raise _MergeConflict(
            f"prospect_key {key!r} already belongs to {existing['company_name']!r}; "
            f"this record says {company_name!r}. Resolve deliberately with force_keys."
        )

    updates: dict[str, object] = {}
    for column, value in (
        ("company_casual", _clean(record.get("company_casual"))),
        ("domain", domain),
        ("website_url", _clean(website_url)),
        ("owner_first_name", _clean(record.get("owner_first_name"))),
        ("owner_last_name", _clean(record.get("owner_last_name"))),
    ):
        if value and not existing[column]:
            updates[column] = value

    merged_note = _compose_note(record.get("icp_fit_note"), record.get("hook_fact"))
    if merged_note and not existing["icp_fit_note"]:
        updates["icp_fit_note"] = merged_note
    elif merged_note and not parse_hook_fact(existing["icp_fit_note"]):
        fact = parse_hook_fact(merged_note)
        if fact:
            updates["icp_fit_note"] = _compose_note(existing["icp_fit_note"], fact)

    # Original market is authoritative; a second sighting is provenance, not a move.
    detail_parts = [p for p in [existing["source_detail"]] if p]
    trail = f"also seen in {market}" if market != existing["market"] else None
    new_detail = _clean(record.get("source_detail"))
    for part in (trail, new_detail):
        if part and part not in detail_parts:
            detail_parts.append(part)
    joined = " | ".join(detail_parts) or None
    if joined != existing["source_detail"]:
        updates["source_detail"] = joined

    if updates:
        assignment = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE prospects SET {assignment}, updated_at = datetime('now') WHERE id = ?",
            (*updates.values(), existing["id"]),
        )
    return "dupe"


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

def _log_state(conn, entity_id, from_state, to_state, actor, reason=None) -> None:
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason) "
        "VALUES ('prospect', ?, ?, ?, ?, ?)",
        (str(entity_id), from_state, to_state, actor, reason),
    )


def _require_prospect(conn, prospect_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if row is None:
        raise KeyError(f"no prospect with id {prospect_id}")
    return row


@dataclass
class VerifyResult:
    """Outcome of writing a batch of verification verdicts onto `contacts`."""
    recorded: int = 0
    unknown: list[str] = field(default_factory=list)
    blocklisted: int = 0
    #: verdict written, but lower than the file claimed (unattested / stale input).
    degraded: list[tuple[str, str]] = field(default_factory=list)
    #: verdict NOT written — the input was not good enough to clear an existing FAIL.
    refused: list[tuple[str, str]] = field(default_factory=list)


#: Only these three verdicts exist (contacts.verify_verdict CHECK constraint).
VERIFY_VERDICTS = ("PASS", "RISK", "FAIL")
#: Strength order, so "never let an import raise a verdict above X" is expressible.
_VERDICT_RANK = {"FAIL": 0, "RISK": 1, "PASS": 2}
#: An attestation older than this cannot be treated as current (matches
#: channels.base.VERIFY_STALE_DAYS — domains die between prospecting and sending).
VERIFY_ATTESTATION_STALE_DAYS = 30
#: The envelope `dmctl verify --emit` writes and `--import` trusts.
VERIFY_EXPORT_TOOL = "dm-engine/verify"


@dataclass
class VerifyAttestation:
    """Where a batch of verdicts came from, and therefore how far to trust it.

    The documented workflow genuinely is "verify on the VPS, import the file
    here", so an import cannot be forbidden — but it also cannot be believed on
    sight. `dmctl verify --import` used to `json.loads` a file and trust it
    outright: a hand-written FAIL->PASS made a refused address stageable, and
    `verified_at = datetime('now')` made a six-month-old file read as checked
    today, silencing the staleness warning too.

    So: an attested, fresh envelope is trusted. Anything else is DEGRADED —
    it can never raise a verdict above RISK and can never clear an existing FAIL.
    RISK from a guessed source is still blocked by G9, so a forged file buys
    nothing it did not already have.
    """
    attested: bool = False
    host: str | None = None
    generated_at: str | None = None       # ISO-8601, from the file
    tool: str | None = None
    source: str = "live"                  # 'live' | 'import:<path>'
    stale: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.attested and not self.stale

    @property
    def ceiling(self) -> str:
        """The best verdict this input is allowed to write."""
        return "PASS" if self.trusted else "RISK"

    def describe(self) -> list[str]:
        lines = [f"source={self.source}",
                 f"attested={'yes' if self.attested else 'NO'}"
                 + (f" host={self.host}" if self.host else "")
                 + (f" at={self.generated_at}" if self.generated_at else "")
                 + (f" tool={self.tool}" if self.tool else "")]
        if not self.trusted:
            lines.append(f"UNTRUSTED — no verdict above {self.ceiling} will be written, "
                         "and an existing FAIL will not be cleared")
        lines.extend(self.problems)
        return lines

    def stamp(self) -> str | None:
        """What to write into `contacts.verified_at`. Never 'now' for an import."""
        return self.generated_at


class VerifyImportError(ValueError):
    """The verdict file cannot be read at all."""


def _parse_iso(value) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_timezone.utc)


def load_verify_import(path: Path) -> tuple[list, VerifyAttestation]:
    """Read a verdict file and say how far it may be trusted.

    Two accepted shapes:

      {"tool": "...", "host": "...", "generated_at": "...", "results": [...]}
          attested — this is what `dmctl verify --emit` writes on the VPS.
      [ {...}, {...} ]
          a bare list. Accepted (the raw verify_emails.py output has this shape)
          but UNATTESTED: nothing in it says where or when it was produced.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerifyImportError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VerifyImportError(f"{path} is not valid JSON: {exc}") from exc

    attestation = VerifyAttestation(source=f"import:{path}")
    if isinstance(payload, list):
        attestation.problems.append(
            "bare list with no envelope: no host, no timestamp, nothing that says "
            "where these verdicts came from. Produce it with `dmctl verify --emit "
            "<path>` on the VPS to have it trusted.")
        return payload, attestation

    if not isinstance(payload, dict):
        raise VerifyImportError(
            f"{path} must be a JSON array of verdicts or a verdict envelope object")

    results = payload.get("results")
    if not isinstance(results, list):
        raise VerifyImportError(f"{path}: envelope has no `results` array")

    attestation.host = payload.get("host") or None
    attestation.tool = payload.get("tool") or None
    attestation.generated_at = payload.get("generated_at") or None
    generated = _parse_iso(attestation.generated_at)
    missing = [name for name, value in (("host", attestation.host),
                                        ("tool", attestation.tool),
                                        ("generated_at", generated))
               if not value]
    if missing:
        attestation.problems.append(
            f"envelope is missing {', '.join(missing)} — treating it as unattested")
    else:
        attestation.attested = True
        age = datetime.now(_timezone.utc) - generated
        if age > timedelta(days=VERIFY_ATTESTATION_STALE_DAYS):
            attestation.stale = True
            attestation.problems.append(
                f"produced {age.days} days ago (>{VERIFY_ATTESTATION_STALE_DAYS}) — "
                "domains die between prospecting and sending; re-verify on the VPS")
        elif generated > datetime.now(_timezone.utc) + timedelta(days=1):
            attestation.attested = False
            attestation.problems.append(
                f"generated_at {attestation.generated_at} is in the future — "
                "treating it as unattested")
    return results, attestation


def write_verify_export(path: Path, results) -> Path:
    """Write the attested envelope `--import` trusts. Host + time, no secrets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "tool": VERIFY_EXPORT_TOOL,
        "host": socket.gethostname(),
        "generated_at": datetime.now(_timezone.utc).isoformat(timespec="seconds"),
        "results": list(results or []),
    }, indent=2) + "\n", encoding="utf-8")
    return path


def record_verify_verdicts(conn: sqlite3.Connection, results,
                           attestation: VerifyAttestation | None = None) -> VerifyResult:
    """Write verifier verdicts onto the matching `contacts` rows.

    Owns the DB side of `dmctl verify`: `gmail_compat.verify_batch_smtp` [D] produces
    the verdicts (it is the only module allowed to touch the gmail-helper skill), and
    this writes them, because `contacts` is a prospects.py table. PLAN.md §12 assigned
    this write to no module; it lands here so `cli.py` stays thin dispatch (§12.6).

    `attestation=None` means a LIVE local run (the verifier just produced these in
    this process), which is trusted and stamped now. A `VerifyAttestation` from
    `load_verify_import` may be untrusted, in which case two rules apply:

      * the verdict written is capped at `attestation.ceiling` (RISK), so a file
        cannot manufacture a PASS; and
      * an existing FAIL is never overwritten — a refused address stays refused
        until an attested, fresh run says otherwise.

    `verified_at` comes from the attestation, never `datetime('now')`, so an old
    file cannot read as checked today and silence the staleness warning.

    Addresses are matched case-insensitively — a verifier may echo back a different
    case than the one stored. An address with no contact row is reported in
    `unknown` rather than silently ignored, so importing a stale verification file
    cannot look like a clean run (the §10 row (i) accounting rule).
    """
    attestation = attestation or VerifyAttestation(attested=True, source="live")
    out = VerifyResult()
    for entry in results or []:
        address = entry.get("email") or entry.get("address")
        verdict = entry.get("verdict")
        if not address or not verdict:
            continue
        verdict = str(verdict).strip().upper()
        if verdict not in VERIFY_VERDICTS:
            raise ValueError(
                f"unknown verdict {verdict!r} for {address} — expected one of "
                f"{', '.join(VERIFY_VERDICTS)}")
        reason = entry.get("reason")

        effective = verdict
        if _VERDICT_RANK[verdict] > _VERDICT_RANK[attestation.ceiling]:
            effective = attestation.ceiling
            out.degraded.append(
                (address, f"{verdict} downgraded to {effective}: "
                          f"{'stale' if attestation.stale else 'unattested'} "
                          f"{attestation.source}"))
            reason = (f"{reason or 'no reason recorded'} "
                      f"[{verdict} downgraded to {effective}: untrusted "
                      f"{attestation.source}]")
        elif attestation.source != "live":
            reason = f"{reason or 'no reason recorded'} [via {attestation.source}]"

        if not attestation.trusted and _existing_verdict(conn, address) == "FAIL":
            out.refused.append(
                (address, f"already FAIL in the database; an untrusted "
                          f"{attestation.source} may not clear it. Re-verify on the "
                          "VPS and import with `dmctl verify --emit`."))
            continue

        stamp = attestation.stamp()
        if stamp:
            cur = conn.execute(
                "UPDATE contacts SET verify_verdict = ?, verify_reason = ?, "
                "verified_at = ? WHERE lower(address) = lower(?)",
                (effective, reason, stamp, str(address).strip()),
            )
        else:
            cur = conn.execute(
                "UPDATE contacts SET verify_verdict = ?, verify_reason = ?, "
                "verified_at = datetime('now') WHERE lower(address) = lower(?)",
                (effective, reason, str(address).strip()),
            )
        if cur.rowcount:
            out.recorded += cur.rowcount
        else:
            out.unknown.append(address)
        if reason and "blocklist" in str(reason).lower():
            out.blocklisted += 1
    return out


def _existing_verdict(conn, address) -> str | None:
    row = conn.execute(
        "SELECT verify_verdict FROM contacts WHERE lower(address) = lower(?)",
        (str(address).strip(),)).fetchone()
    return row["verify_verdict"] if row else None


def qualify(conn: sqlite3.Connection, prospect_id: int, actor: str) -> None:
    """NEW -> QUALIFIED. Idempotent; refuses the terminal states."""
    row = _require_prospect(conn, prospect_id)
    if row["state"] == "QUALIFIED":
        return
    if row["state"] != "NEW":
        raise ValueError(
            f"prospect {prospect_id} is {row['state']} — only NEW prospects can be qualified"
        )
    with conn:
        conn.execute(
            "UPDATE prospects SET state = 'QUALIFIED', updated_at = datetime('now') WHERE id = ?",
            (prospect_id,),
        )
        _log_state(conn, prospect_id, row["state"], "QUALIFIED", actor)


def set_casual(conn: sqlite3.Connection, prospect_id: int, casual: str) -> None:
    """Fix a badly casualized company name (§9 edge 6), then re-compose.

    No structural policing here — lint rule L10 [C] owns what a good casual name
    looks like, and the review file shows every one to a human before approval.
    """
    row = _require_prospect(conn, prospect_id)
    cleaned = str(casual or "").strip()
    if not cleaned:
        raise ValueError("company_casual must not be empty")
    with conn:
        conn.execute(
            "UPDATE prospects SET company_casual = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (cleaned, row["id"]),
        )


def exclude(conn: sqlite3.Connection, prospect_id: int, reason: str, actor: str) -> None:
    """Any state -> EXCLUDED (terminal). A reason is mandatory: this is a judgement call."""
    row = _require_prospect(conn, prospect_id)
    if not str(reason or "").strip():
        raise ValueError("exclude() requires a reason")
    if row["state"] == "EXCLUDED":
        return
    with conn:
        conn.execute(
            "UPDATE prospects SET state = 'EXCLUDED', updated_at = datetime('now') WHERE id = ?",
            (prospect_id,),
        )
        _log_state(conn, prospect_id, row["state"], "EXCLUDED", actor, reason)
