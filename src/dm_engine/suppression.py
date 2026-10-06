"""Suppression: the list that keeps "no, stop emailing me" enforced. §12.3, §9 edge 34.

This is a safety system, not a feature, so four properties are non-negotiable:

1. **Fail closed.** `is_suppressed()` never answers "not suppressed" because it could
   not tell. A bad channel, a missing address, a database error — every one of those
   returns a reason string (truthy), which callers read as "do not contact".
2. **The shared file is IMPORTED, not just written.** `sync()` runs before every
   gate (staging, approval, review, import) via `require_sync()`, and refuses the
   run outright when the file cannot be read or has lost addresses since the last
   sync. An unreadable list must never be read as "nobody is suppressed" — that is
   the exact failure that lets a person who said stop get emailed again.
3. **Match the person and the company, not just the mailbox.** A human who said no
   at one company is still the same human at the next one; a company that
   unsubscribed is still that company under a second market key. So `add()` with a
   consent-withdrawal reason also writes a domain-scope row and moves every
   matching prospect (and its website-less name-slug twins) to state SUPPRESSED,
   where trigger G12 blocks any further message.
4. **Match the mailbox, not the spelling of it.** A `+tag` suffix on any provider,
   and dots in the local part on gmail.com / googlemail.com, address the same
   mailbox as the bare form. `canonical_email()` folds both, and migration 004
   folds them inside trigger G8 too.
5. **Match the identity the CHANNEL uses.** `canonical_email()` is meaningless for a
   phone number: `+1 (602) 555-0134`, `602-555-0134` and `16025550134` are one human,
   and a hard-no that misses on punctuation means messaging someone who told the operator
   to stop. `canonical_address(channel, address)` dispatches — digits for WhatsApp,
   the page handle for Messenger, the mailbox for everything else — and migration 005
   mirrors all three inside `contacts.address_canon` so the SQL belt agrees.

The file side (`cfg.suppression_file`) is SHARED with the gmail-helper skill, whose
`create_drafts.load_suppression()` reads a line as an address verbatim — it does NOT
strip trailing comments. So appended address lines are bare, and the reason lives in
a `# --- dm-engine: <reason> ... ---` sub-header above them. Writes are append-only:
lines this engine did not create are never rewritten.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import phx_date

CHANNELS = {"email", "linkedin", "instagram", "sms", "whatsapp", "messenger", "all"}
SCOPES = {"address", "domain", "person"}
REASONS = {"hard_no", "unsubscribe", "bounce", "dead_mailbox", "legal", "manual"}

#: A WhatsApp address is a phone number; a Messenger address is a Page/profile URL.
#: Neither has a domain, so a `scope='domain'` row can never legitimately match one —
#: `add()` refuses to write one and `is_suppressed()` refuses to read one. Left
#: unguarded, `_domain_hit`'s suffix fallback (the branch that exists for LinkedIn
#: URLs) would let a short domain row match the tail of a phone number by accident.
PHONE_CHANNELS = frozenset({"whatsapp"})
HANDLE_CHANNELS = frozenset({"messenger"})
DOMAINLESS_CHANNELS = PHONE_CHANNELS | HANDLE_CHANNELS

#: Reasons that mean a HUMAN withdrew consent, as opposed to a delivery failure.
#: Only these propagate to the company (domain row + prospect state SUPPRESSED): a
#: hard bounce says the mailbox is dead, not that the company said stop.
CONSENT_WITHDRAWAL_REASONS = frozenset({"hard_no", "unsubscribe", "legal"})

#: Suppressing an @-domain is right for a company mailbox and catastrophic for a
#: consumer provider — a contractor on gmail.com must not take gmail.com with them.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "yahoo.co.uk",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "aim.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.net", "mail.com",
    "zoho.com", "yandex.com", "fastmail.com", "hey.com", "juno.com", "netzero.net",
    "comcast.net", "cox.net", "sbcglobal.net", "verizon.net", "att.net",
    "bellsouth.net", "earthlink.net", "charter.net", "rr.com", "roadrunner.com",
    "embarqmail.com", "windstream.net", "frontier.com", "optonline.net", "q.com",
})

#: Providers where folding dots in the local part is a documented provider rule
#: rather than a guess. Kept in lockstep with migration 004's addr_host CASE.
DOT_FOLDING_DOMAINS = frozenset({"gmail.com"})
#: Host aliases that are the same mailbox namespace.
HOST_ALIASES = {"googlemail.com": "gmail.com"}

#: Every line this engine writes to the shared file sits under a header containing
#: this token, so a future reader can tell our lines from hand-curated ones.
MARKER_TOKEN = "dm-engine"
_HEADER_RE = re.compile(rf"^#.*\b{MARKER_TOKEN}:\s*([a-z_]+)", re.IGNORECASE)

#: Reason recorded for a hand-curated line in the shared file. The file's own header
#: describes it as "confirmed-dead / hard-bounced addresses".
FILE_DEFAULT_REASON = "dead_mailbox"

#: Most specific first, so the returned reason is the most informative match.
_SCOPE_ORDER = ("address", "person", "domain")

#: A control character in an address line cannot have come from `append_to_file`
#: (which writes one bare address per line). It CAN inject an extra line into the
#: shared file, silently suppressing an unrelated address for create_drafts.py.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

__all__ = [
    "CHANNELS", "SCOPES", "REASONS", "CONSENT_WITHDRAWAL_REASONS",
    "FREEMAIL_DOMAINS", "MARKER_TOKEN", "FILE_DEFAULT_REASON",
    "PHONE_CHANNELS", "HANDLE_CHANNELS", "DOMAINLESS_CHANNELS",
    "SuppressionSyncError", "SyncReport",
    "canonical_email", "canonical_phone", "canonical_messenger", "canonical_address",
    "is_suppressed", "add", "sync_from_file", "sync",
    "require_sync", "last_sync", "file_addresses", "append_to_file",
]


class SuppressionSyncError(RuntimeError):
    """The shared suppression list cannot be trusted, so nothing may proceed.

    Raised only for the states where "carry on" would mean acting as though the
    list were empty: unreadable file, a file that has disappeared after a previous
    non-empty sync, or a line carrying a control character.
    """


@dataclass
class SyncReport:
    """What one file->database sync actually saw. `warnings` are loud, not fatal."""
    path: Path
    present: bool = False
    file_addresses: int = 0
    added: int = 0
    prospects_suppressed: int = 0
    warnings: list[str] = field(default_factory=list)


def _fold(value) -> str:
    return str(value or "").strip().lower()


# --------------------------------------------------------------------------- #
# canonical form (matching only — never what we send to)
# --------------------------------------------------------------------------- #

def canonical_email(address) -> str:
    """The mailbox an address actually reaches, for MATCHING only.

    Mirrors `contacts.address_canon` in migration 004 exactly; the two must agree
    or the Python pre-check and the SQL trigger disagree about who is suppressed.

      owner+quote@example.com   -> owner@example.com     (+tag, every provider)
      O.Wner@googlemail.example -> owner@gmail.example   (dots + host alias, gmail only)
      https://linkedin.com/in/x -> unchanged (no '@' — not an email)

    Deliberately NOT used to rewrite `contacts.address`: the address we put on a
    draft stays the address a human saw on the website.
    """
    folded = _fold(address)
    if "@" not in folded:
        return folded
    local, _, host = folded.partition("@")
    host = HOST_ALIASES.get(host, host)
    local = local.split("+", 1)[0]
    if host in DOT_FOLDING_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{host}"


#: Punctuation a phone number is written with. Removed in this exact order by both
#: `canonical_phone` and `contacts.addr_stripped` (migration 005) — SQLite has no
#: regex, so the strip has to be a fixed chain of replacements on both sides.
_PHONE_PUNCT = ("+", "-", " ", "(", ")", ".", "/")

#: Prefix strips that reduce every spelling of a Messenger destination to its page
#: handle. Applied in order, after a trailing-slash trim; mirrored character for
#: character by `contacts.addr_handle` (migration 005). The slash trim comes first
#: on purpose: a page called '.../pinelandscapemgmt/' must not lose its last two
#: characters to the 't/' strip.
_MESSENGER_SCHEMES = ("https://", "http://", "www.")
_MESSENGER_HOSTS = ("m.me/", "fb.me/", "fb.com/", "messenger.com/", "facebook.com/",
                    "messages/", "t/")

#: `facebook.com/profile.php?id=<N>` is the only Messenger spelling where the PATH
#: is the same for everybody and the identity lives in the query string. Stripping
#: the query (right for '?ref=...') would fold every such profile onto the literal
#: handle 'profile.php' — one hard-no suppressing every unrelated prospect whose
#: page is a profile.php URL.
_PROFILE_PATH = "profile.php"
_PROFILE_ID = re.compile(r"(?:^|&)id=([0-9]+)")


def canonical_phone(address) -> str:
    """The digits a WhatsApp number reaches, for MATCHING only.

      '+1 (602) 555-0134' -> '6025550134'
      '602-555-0134'      -> '6025550134'
      '16025550134'       -> '6025550134'

    Same convention as `attribution.normalize_phone` — digits only, US country code
    dropped — restated here rather than imported so that the identical operation can
    be written in SQLite (which has no regex) for `contacts.addr_phone`. The two are
    held in lockstep by a test that runs both over the same numbers.

    A value that is NOT a phone number comes back folded but otherwise untouched. The
    whatsapp channel also carries person keys, and stripping the punctuation out of
    one ('alex-x' -> 'alexx') would stop a person-scope row from matching the
    contact it was written for.
    """
    folded = _fold(address)
    digits = folded
    for char in _PHONE_PUNCT:
        digits = digits.replace(char, "")
    if not digits or not (digits.isascii() and digits.isdigit()):
        return folded
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def canonical_messenger(address) -> str:
    """The Page/profile handle a Messenger destination reaches, for MATCHING only.

      'https://m.me/example-hardscape/'          -> 'example-hardscape'
      'https://www.facebook.com/example-hardscape' -> 'example-hardscape'
      'https://facebook.com/messages/t/12345'    -> '12345'
      'https://www.facebook.com/example#'        -> 'example'
      'https://www.facebook.com/example?ref=bookmarks' -> 'example'
      'https://facebook.com/profile.php?id=100093'     -> '100093'
      'https://facebook.com/profile.php'               -> '' (names nobody)

    m.me, fb.me and facebook.com are three spellings of one destination, so a
    hard-no recorded against any of them has to block the others.

    The fragment and query strip is not cosmetic. A profile URL copied out of a
    browser carries a trailing '#', and a scraper hands back '?ref=...'; without
    this, those canonicalize to a DIFFERENT identity than the bare URL, and a
    hard-no recorded against one spelling silently fails to block the other. Same
    failure class as unfolded '+tag' on an email.

    `profile.php?id=<N>` is the one spelling where the query IS the identity, so
    it is read before the strip: every such URL used to fold onto the literal
    handle 'profile.php', which made one hard-no suppress every unrelated
    prospect whose page is a profile.php URL. A profile.php URL with no usable
    `id=` canonicalizes to '' — it names nobody, and `prospects.messenger_handle`
    turns that into an "unusable messenger url" warning rather than a contact.

    NOT yet mirrored by the `contacts.address_canon` generated column: migration
    005's `addr_handle` still folds profile.php URLs onto 'profile.php'. See the
    note in tests/test_suppression.py — a migration 006 has to re-state this CASE
    (over `addr_lc`, not `addr_nofrag`) before the SQL belt agrees again.
    """
    handle = _fold(address)
    for scheme in _MESSENGER_SCHEMES:
        handle = handle.replace(scheme, "")
    handle = handle.split("#", 1)[0]
    path, _, query = handle.partition("?")
    if path.rstrip("/").endswith(_PROFILE_PATH):
        found = _PROFILE_ID.search(query)
        return found.group(1) if found else ""
    handle = path.rstrip("/")
    for host in _MESSENGER_HOSTS:
        handle = handle.replace(host, "")
    return handle


def canonical_address(channel, address) -> str:
    """The identity `address` reaches on `channel`. Mirrors `contacts.address_canon`.

    Dispatching on the channel is the whole point: an email folds by mailbox rules,
    a WhatsApp number folds to digits, a Messenger URL folds to its page handle.
    Folding a phone number with `canonical_email` (the pre-005 behaviour) left
    `602-555-0134` and `16025550134` as two unrelated strings, which is a suppression
    miss — i.e. a message to somebody who said stop.
    """
    chan = _fold(channel)
    if chan in PHONE_CHANNELS:
        return canonical_phone(address)
    if chan in HANDLE_CHANNELS:
        return canonical_messenger(address)
    return canonical_email(address)


def _host_of(address: str) -> str:
    canon = canonical_email(address)
    return canon.rsplit("@", 1)[1] if "@" in canon else ""


def _domain_hit(address: str, target: str) -> bool:
    """Domain-scope match: the host itself or any subdomain of it.

    A plain `endswith` was both too loose (`@notexample.com` ends with
    `example.com`) and too tight (`@example.com` misses a `mail.example.com` box).
    """
    wanted = target.lstrip("@").strip(".").lower()
    if not wanted:
        return False
    host = _host_of(address)
    if not host:
        folded = _fold(address)
        if _looks_like_phone(folded):
            # There is no domain in a phone number, and the suffix rule below would
            # let a short domain row match its tail by coincidence.
            return False
        # LinkedIn URLs and other non-email addresses keep the old suffix rule.
        return folded.endswith(wanted)
    return host == wanted or host.endswith("." + wanted)


def _looks_like_phone(folded: str) -> bool:
    """True when the address is nothing but a written-out phone number."""
    return bool(folded) and canonical_phone(folded).isdigit() and \
        set(folded) <= set("0123456789" + "".join(_PHONE_PUNCT))


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

def is_suppressed(conn: sqlite3.Connection, channel: str, address: str,
                  person_key: str | None = None) -> str | None:
    """Return the suppression reason, or None when the contact is genuinely clear.

    Any inability to answer the question returns a `fail_closed: ...` reason — a
    truthy value — because the cost of a false "clear" is messaging someone who
    told us to stop.

    Two channel rules, both mirrored by trigger G8 (migration 005):

      * a `person`-scope row matches on ANY channel. It is a fact about a human, not
        about one app, and the whole point of the DM build is that the operator reaches the
        same owner on WhatsApp, Messenger and email — a hard-no taken on one of them
        that left the other two open is a hole nobody would guess was there.
      * a `domain`-scope row never matches a WhatsApp or Messenger address. There is
        no domain in a phone number or a page handle.
    """
    chan = _fold(channel)
    addr = _fold(address)
    person = _fold(person_key) or None

    if not addr and not person:
        return "fail_closed: no address or person key to check"
    if chan not in CHANNELS:
        return f"fail_closed: unknown channel {channel!r}"

    addr_canon = canonical_address(chan, addr) if addr else ""
    person_canon = canonical_address(chan, person) if person else None

    try:
        if chan == "all":
            rows = conn.execute("SELECT scope, address, reason FROM suppression").fetchall()
        else:
            rows = conn.execute(
                "SELECT scope, address, reason FROM suppression "
                "WHERE channel = ? OR channel = 'all' OR scope = 'person'",
                (chan,),
            ).fetchall()
    except Exception as exc:                                  # noqa: BLE001 — fail closed
        return f"fail_closed: suppression lookup failed ({exc})"

    identities = {value for value in (addr, addr_canon, person, person_canon) if value}
    for scope in _SCOPE_ORDER:
        for row in rows:
            if row["scope"] != scope:
                continue
            target = _fold(row["address"])
            if not target:
                continue
            if scope in ("address", "person"):
                if target in identities or canonical_address(chan, target) in identities:
                    return row["reason"]
            elif (scope == "domain" and addr and chan not in DOMAINLESS_CHANNELS
                    and _domain_hit(addr, target)):
                return row["reason"]
    return None


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def add(conn: sqlite3.Connection, channel: str, address: str, scope: str,
        reason: str, source: str) -> int:
    """Record a suppression. Returns the number of suppression rows written.

    Idempotent — UNIQUE(channel, address) keeps the first row for an address.

    A CONSENT-WITHDRAWAL reason (hard_no / unsubscribe / legal) is not just an
    address fact, it is a fact about a person and a company, so it also:

      * writes a `scope='domain'` row for the company domain (never for a
        consumer provider — see FREEMAIL_DOMAINS), which is what stops the same
        company being reached at a different mailbox; and
      * moves every prospect reachable at that address — and every website-less
        name-slug TWIN of those prospects — to state SUPPRESSED, where trigger
        G12 refuses any further message.

    That propagation lives here rather than in the caller because `replies.py`
    routes every suppression through this function; putting it anywhere else
    leaves the "unsubscribed company gets a fresh Message 1 via its twin row"
    path open.
    """
    chan, sc, rs = _fold(channel), _fold(scope), _fold(reason)
    addr = _fold(address)
    if chan not in CHANNELS:
        raise ValueError(f"channel must be one of {sorted(CHANNELS)}, got {channel!r}")
    if sc not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}, got {scope!r}")
    if rs not in REASONS:
        raise ValueError(f"reason must be one of {sorted(REASONS)}, got {reason!r}")
    if not addr:
        raise ValueError("suppression address must not be empty")
    if _CONTROL_RE.search(addr):
        raise ValueError(
            "suppression address contains a control character; it would inject an "
            "extra line into the shared suppression_list.txt")
    if sc == "domain" and chan in DOMAINLESS_CHANNELS:
        raise ValueError(
            f"scope 'domain' is meaningless on {chan!r} — a phone number and a "
            "Messenger page have no domain. Use scope 'address' for the one "
            "destination, or 'person' for the human across every channel.")

    stored = canonical_address(chan, addr) if sc != "domain" else addr
    if not stored:
        raise ValueError(
            f"{address!r} carries no identity on channel {chan!r} — a Messenger "
            "profile.php URL with no `id=` names nobody, and a row with an empty "
            "address matches nothing. Suppress the numeric profile id or the page "
            "handle instead.")
    written = 0
    with conn:
        written += _insert_row(conn, chan, stored, sc, rs, source)
        if rs in CONSENT_WITHDRAWAL_REASONS:
            written += _propagate_withdrawal(conn, chan, stored, sc, rs, source)
    return written


def _insert_row(conn, channel: str, address: str, scope: str, reason: str,
                source) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO suppression (channel, address, scope, reason, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel, address, scope, reason, _fold(source) or "dmctl"),
    )
    return cur.rowcount or 0


def _propagate_withdrawal(conn, channel: str, address: str, scope: str, reason: str,
                          source) -> int:
    """Domain row + prospect SUPPRESSED for a consent withdrawal. Returns rows added.

    Domain scope is EMAIL-SHAPED and deliberately does nothing on a DM channel: a
    phone number has no domain, and a Messenger handle's "domain" is facebook.com —
    writing either would either be a no-op or, worse, suppress every prospect on the
    platform from one hard-no. Reaching the company's other mailboxes is not lost:
    `_suppress_matching_prospects` below still moves the whole prospect (and its
    website-less twins) to SUPPRESSED, where trigger G12 blocks every channel.
    """
    written = 0
    if channel not in DOMAINLESS_CHANNELS:
        host = _host_of(address) if scope != "domain" else address.lstrip("@").strip(".")
        if host and host not in FREEMAIL_DOMAINS:
            written += _insert_row(conn, channel, f"@{host}", "domain", reason, source)
    _suppress_matching_prospects(conn, channel, address, scope, reason)
    return written


def _suppress_matching_prospects(conn, channel: str, address: str, scope: str,
                                 reason: str) -> int:
    """Move every prospect reachable at `address` (and its twins) to SUPPRESSED."""
    try:
        prospect_ids = _prospects_for(conn, channel, address, scope)
        prospect_ids |= _twins_of(conn, prospect_ids)
    except sqlite3.Error:
        return 0                       # pre-004 schema; the address row still stands
    moved = 0
    for prospect_id in sorted(prospect_ids):
        row = conn.execute("SELECT state FROM prospects WHERE id = ?",
                           (prospect_id,)).fetchone()
        if row is None or row["state"] in ("SUPPRESSED", "EXCLUDED"):
            continue
        conn.execute(
            "UPDATE prospects SET state = 'SUPPRESSED', updated_at = datetime('now') "
            "WHERE id = ?", (prospect_id,))
        conn.execute(
            "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, "
            "reason, evidence) VALUES ('prospect', ?, ?, 'SUPPRESSED', 'engine', ?, ?)",
            (str(prospect_id), row["state"],
             f"suppressed: {reason}", f"suppression:{scope}"),
        )
        moved += 1
    return moved


def _prospects_for(conn, channel: str, address: str, scope: str) -> set[int]:
    if scope == "domain":
        wanted = address.lstrip("@").strip(".")
        rows = conn.execute(
            "SELECT DISTINCT prospect_id FROM contacts "
            "WHERE (channel = ? OR ? = 'all') "
            "  AND (addr_host = ? OR addr_host LIKE '%.' || ?)",
            (channel, channel, wanted, wanted)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT prospect_id FROM contacts "
            "WHERE (channel = ? OR ? = 'all') "
            "  AND (address_canon = ? OR addr_lc = ? OR lower(person_key) = ?)",
            (channel, channel, address, address, address)).fetchall()
    return {row["prospect_id"] for row in rows}


def _twins_of(conn, prospect_ids: set[int]) -> set[int]:
    """Rows that are the SAME company under a different key (§9 edge 4 / edge 34).

    Two shapes: a shared domain, and the market-scoped `name:<slug>:<market>` key a
    website-less company gets — which is exactly how an unsubscribed company came
    back with a fresh prospect row and a fresh Message 1.
    """
    twins: set[int] = set()
    for prospect_id in prospect_ids:
        row = conn.execute("SELECT prospect_key, domain FROM prospects WHERE id = ?",
                           (prospect_id,)).fetchone()
        if row is None:
            continue
        if row["domain"]:
            twins |= {r["id"] for r in conn.execute(
                "SELECT id FROM prospects WHERE domain = ? AND id != ?",
                (row["domain"], prospect_id))}
        key = row["prospect_key"] or ""
        if key.startswith("name:"):
            prefix = key.rsplit(":", 1)[0]
            twins |= {r["id"] for r in conn.execute(
                "SELECT id FROM prospects WHERE prospect_key LIKE ? AND id != ?",
                (f"{prefix}:%", prospect_id))}
    return twins - prospect_ids


# --------------------------------------------------------------------------- #
# the shared file -> database direction (§10 row (e), §2.3)
# --------------------------------------------------------------------------- #

def parse_file(path: Path) -> list[tuple[str, str]]:
    """[(address, reason)] from the shared file. Raises SuppressionSyncError.

    Parsing matches `create_drafts.load_suppression()` exactly (strip, skip blanks
    and `#` lines) so the two tools can never disagree about what the file says.
    A `# --- dm-engine: <reason> ---` header carries its reason to the addresses
    below it, which is what makes the file round-trip.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuppressionSyncError(
            f"cannot read the shared suppression list {path}: {exc}. Refusing to "
            "continue — an unreadable list is not an empty one.") from exc
    except UnicodeDecodeError as exc:
        raise SuppressionSyncError(
            f"the shared suppression list {path} is not valid UTF-8 ({exc}). "
            "Refusing to continue — a list we cannot parse is not an empty one."
        ) from exc

    out: list[tuple[str, str]] = []
    reason = FILE_DEFAULT_REASON
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            match = _HEADER_RE.match(stripped)
            candidate = match.group(1).lower() if match else None
            reason = candidate if candidate in REASONS else FILE_DEFAULT_REASON
            continue
        if _CONTROL_RE.search(stripped):
            raise SuppressionSyncError(
                f"{path}:{number} contains a control character. create_drafts.py "
                "reads a line as an address verbatim, so this can suppress an "
                "unrelated address. Fix the line by hand before running again.")
        out.append((stripped.lower(), reason))
    return out


def sync_from_file(conn: sqlite3.Connection, path) -> int:
    """Import the shared suppression file into the DB. Returns rows newly added.

    A missing file is 0 here; deciding whether a MISSING file is safe belongs to
    `sync()`, which can compare it against what the last sync saw.

    The address is stored CANONICAL, exactly as `add()` stores it. A raw file line
    is invisible to every SQL-side matcher — trigger G8 and its mirror
    `channels.base._SUPPRESSION_MATCH` canonicalize the contact and compare it
    against `suppression.address` verbatim — so a line carrying a '+tag' or
    gmail dots never matched the bare contact at the stage gate, which is where a
    message already APPROVED is caught.
    """
    path = Path(path)
    if not path.exists():
        return 0
    added = 0
    with conn:
        for address, reason in parse_file(path):
            added += _insert_row(conn, "email", canonical_email(address), "address",
                                 reason, "file_sync")
    return added


def last_sync(conn: sqlite3.Connection):
    try:
        return conn.execute(
            "SELECT path, file_addresses, status, synced_at FROM suppression_sync "
            "WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None


def _record_sync(conn, path: Path, count: int, status: str) -> None:
    try:
        with conn:
            conn.execute(
                "INSERT INTO suppression_sync (id, path, file_addresses, status, synced_at) "
                "VALUES (1, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET path = excluded.path, "
                "file_addresses = excluded.file_addresses, status = excluded.status, "
                "synced_at = excluded.synced_at",
                (str(path), count, status))
    except sqlite3.Error:
        pass                            # pre-004 schema: the import itself still ran


def sync(conn: sqlite3.Connection, cfg) -> SyncReport:
    """Import `cfg.suppression_file`, fail closed on anything ambiguous.

    Refuses (SuppressionSyncError):
      * the file exists but cannot be read or decoded;
      * a line carries a control character;
      * the file is GONE and a previous sync imported addresses from it — the
        list did not become empty, it disappeared, and continuing would mean
        emailing everyone who was on it.

    Warns (loudly, but continues):
      * no file at all and no history — a fresh machine has nothing to import;
      * the file shrank since the last sync — suppression is append-only by
        design, so this is a hand edit worth seeing.
    """
    path = Path(cfg.suppression_file)
    previous = last_sync(conn)
    report = SyncReport(path=path)

    if not path.exists():
        if previous is not None and previous["file_addresses"] > 0:
            raise SuppressionSyncError(
                f"the shared suppression list {path} is GONE — the last sync "
                f"imported {previous['file_addresses']} address(es) from it "
                f"({previous['synced_at']}). Refusing to run: a missing list is not "
                "an empty one. Restore the file (it is shared with the gmail-helper "
                "skill) and try again.")
        report.warnings.append(
            f"no shared suppression list at {path} — nothing to import. If this "
            "machine should have one, stop and restore it before staging anything.")
        _record_sync(conn, path, 0, "absent")
        return report

    entries = parse_file(path)
    report.present = True
    report.file_addresses = len({address for address, _ in entries})

    if previous is not None and report.file_addresses < previous["file_addresses"]:
        report.warnings.append(
            f"the shared suppression list shrank from {previous['file_addresses']} "
            f"to {report.file_addresses} address(es) since {previous['synced_at']}. "
            "Suppression is append-only by design — someone edited the file by hand.")

    before = _prospect_suppressed_count(conn)
    added = 0
    with conn:
        for address, reason in entries:
            # canonical, exactly as `add()` stores it — see `sync_from_file`
            stored = canonical_email(address)
            added += _insert_row(conn, "email", stored, "address", reason, "file_sync")
            if reason in CONSENT_WITHDRAWAL_REASONS:
                _propagate_withdrawal(conn, "email", stored, "address", reason,
                                      "file_sync")
    report.added = added
    report.prospects_suppressed = _prospect_suppressed_count(conn) - before
    _record_sync(conn, path, report.file_addresses, "ok")
    return report


def require_sync(conn: sqlite3.Connection, cfg, gate: str) -> SyncReport:
    """`sync()` at a gate, with the gate named in the failure. Use before staging.

    Every path that can put a message in front of a human — import, review,
    approve, stage — calls this. It is the wiring PLAN.md §10(e) claimed and the
    engine did not have: without it `sync_from_file` had zero callers and every
    address on the operator's real list was invisible to the engine.
    """
    try:
        return sync(conn, cfg)
    except SuppressionSyncError as exc:
        raise SuppressionSyncError(f"{gate}: {exc}") from exc


def _prospect_suppressed_count(conn) -> int:
    try:
        return conn.execute(
            "SELECT count(*) FROM prospects WHERE state = 'SUPPRESSED'").fetchone()[0]
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------- #
# the database -> shared file direction
# --------------------------------------------------------------------------- #

def _file_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def file_addresses(path) -> set[str]:
    """Bare addresses currently in the shared file (same parse as the skill)."""
    return {
        line.strip().lower()
        for line in _file_lines(Path(path))
        if line.strip() and not line.lstrip().startswith("#")
    }


def append_to_file(path, address: str, reason: str) -> None:
    """Append `address` under a `# dm-engine` header. Idempotent, append-only.

    Never rewrites the file: the shared list is hand-curated by the operator and read by
    other tooling, so this only ever opens it in append mode.
    """
    path = Path(path)
    addr = _fold(address)
    rs = _fold(reason)
    if not addr:
        raise ValueError("cannot suppress an empty address")
    if _CONTROL_RE.search(addr):
        raise ValueError(
            "cannot append an address containing a control character — it would "
            "add a second bare line to the shared list and suppress an unrelated "
            "address for create_drafts.py")
    if rs not in REASONS:
        raise ValueError(f"reason must be one of {sorted(REASONS)}, got {reason!r}")

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    if addr in {ln.strip().lower() for ln in lines
                if ln.strip() and not ln.lstrip().startswith("#")}:
        return

    header = f"# --- {MARKER_TOKEN}: {rs} (from {phx_date()}) ---"
    chunk = "" if _header_is_open(lines, header) else f"\n{header}\n"
    prefix = "" if (not text or text.endswith("\n")) else "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{chunk}{addr}\n")


def _header_is_open(lines: list[str], header: str) -> bool:
    """True when `header` is the last comment in the file and only addresses follow."""
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped == header
    return False
