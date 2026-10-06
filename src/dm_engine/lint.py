"""Mechanical message gates — the law that copy must survive. Contract: PLAN.md §12.4.

Everything upstream of this module is a language model writing persuasive copy.
This module is the only thing that makes the operator's copy rules *enforceable* rather
than aspirational (PLAN.md §5.4, the operator's global outreach rules cold-email framework).

Two properties matter more than the rest:

* **Normalization runs first.** `normalize()` folds NFKC, strips zero-width and tag
  characters, folds every non-Latin letter onto its ASCII *skeleton* (a table
  generated from the Unicode character database, not typed by hand — see the note
  above `_GREEK_SKELETON`), and folds curly quotes — *before* any rule sees the text.
  Without it, `fdаi` (Cyrillic а), `fdαi` (Greek α) or `lead​flow` sail past every
  string check (PLAN.md §10 row (l)). Ingest stores the normalized text, so the SQL
  belt (trigger G6) sees ASCII too. Leet swaps (`FD41`) are *not* folded here — that
  would corrupt real copy — they are folded into the L2 patterns instead.
* **False positives are as damaging as misses.** A linter that rejects good copy
  trains its owner to bypass it. Every rule below has a "does not fire on clean
  copy" test, and several rules are deliberately narrowed (see `_OBF_SPACED`,
  `brightflow`, company-name masking in L3) to keep that true.

Rule IDs are PLAN.md §5.4's L1–L12 verbatim; `RULES` is the machine-readable list.

This module imports nothing from `dm_engine.channels` (Owner D, parallel build):
per-channel limits arrive as the `constraints=` parameter, and `DEFAULT_CONSTRAINTS`
is the fallback. Any object exposing the ChannelConstraints attribute names works.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace

# --------------------------------------------------------------------------- #
# data types
# --------------------------------------------------------------------------- #


@dataclass
class MessageDraft:
    """One message as a copywriting agent returns it (PLAN.md §12.8)."""

    prospect_key: str
    channel: str
    sequence_step: int = 1
    subject: str | None = None
    body: str = ""


@dataclass
class LintReport:
    """`violations` items are `{'rule': 'L1', 'detail': str, 'where': str}`.

    `to_json()` MUST emit a real JSON boolean for `ok`: trigger G5 tests
    `json_extract(lint_report,'$.ok') IS NOT 1`, and SQLite maps JSON `true` to 1.
    A string `"true"` (or a missing key) is refused by the database, correctly.
    """

    ok: bool
    violations: list[dict] = field(default_factory=list)

    @classmethod
    def build(cls, violations: list[dict]) -> "LintReport":
        return cls(ok=not violations, violations=list(violations))

    def to_json(self) -> str:
        return json.dumps({"ok": bool(self.ok), "violations": self.violations},
                          ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> "LintReport":
        data = json.loads(text)
        return cls(ok=bool(data.get("ok")), violations=list(data.get("violations", [])))

    def rules(self) -> list[str]:
        """Rule ids that fired, in order, de-duplicated."""
        seen: list[str] = []
        for violation in self.violations:
            rule = violation.get("rule")
            if rule and rule not in seen:
                seen.append(rule)
        return seen

    def summary(self) -> str:
        if self.ok:
            return "lint ok"
        return "; ".join(f"{v.get('rule')}: {v.get('detail')}" for v in self.violations)


@dataclass(frozen=True)
class Constraints:
    """Local mirror of `channels/base.py::ChannelConstraints` plus two extras.

    Deliberately NOT imported from `dm_engine.channels` — Owner D writes that module
    in parallel, and lint must never depend on a half-written import. The caller
    supplies the real object; anything with these attribute names is accepted.
    """

    max_words: int | None = None
    max_sentences: int | None = None
    max_chars: int | None = None
    has_subject: bool = False
    max_subject_chars: int | None = 60
    requires_signature: bool = False


#: Per-channel fallbacks (PLAN.md §5.4 L6/L7). Used only when the caller passes none.
#: `has_subject` is what routes a channel to L6 (long-form: word cap, lowercase
#: subject, `the operator` signature) or L7 (short-form: sentence + character caps, no
#: subject). Every DM channel is L7. These MUST agree with the adapters'
#: `ChannelConstraints` — tests/test_channels.py asserts it for all four channels.
DEFAULT_CONSTRAINTS: dict[str, Constraints] = {
    "email": Constraints(max_words=120, max_sentences=None, max_chars=None,
                         has_subject=True, max_subject_chars=60, requires_signature=True),
    "linkedin": Constraints(max_words=None, max_sentences=3, max_chars=300,
                            has_subject=False, max_subject_chars=None,
                            requires_signature=False),
    "whatsapp": Constraints(max_words=None, max_sentences=3, max_chars=400,
                            has_subject=False, max_subject_chars=None,
                            requires_signature=False),
    "messenger": Constraints(max_words=None, max_sentences=4, max_chars=500,
                             has_subject=False, max_subject_chars=None,
                             requires_signature=False),
}

#: The demo link, step-2 only (PLAN.md §5.4 L11).
#: DEVIATION (logged): PLAN says "imported constant DEMO_LINK from create_drafts.py".
#: Importing it would drag `googleapiclient` into every lint run — which the test
#: conftest blocks by design (PLAN.md §13). The literal is asserted against
#: gmail_compat.DEMO_LINK at integration time instead; it has one canonical value.
DEMO_LINK = "brightdesk.brightflowcure.com/demo-ai-voice"

#: The one signature the operator signs with; nothing else is allowed. Configure it to your own first name.
SIGNATURE = "Sam"

RULES: dict[str, str] = {
    "L1": "no URLs, domains, IPs or dial-out URIs (step 2: the demo link only)",
    "L2": "no product identity: bdai / bright desk ai / brightflow(cure) / brightdesk (step 1)",
    "L3": "no banned phrases / agency-speak",
    "L4": "no emoji",
    "L5": "no unresolved placeholders, token-shaped holes, null greetings or filler hooks",
    "L6": "long-form (email) shape: word cap, subject present+lowercase+capped, signed "
          "'Sam', bounded footer",
    "L7": "short-form (DM) shape: sentence cap, character cap, no subject "
          "(linkedin / whatsapp / messenger)",
    "L8": "exactly one CTA — one closing question, no phone number",
    "L9": "subject unique within the batch and against the last 30 days",
    "L10": "company_casual is a usable casual name",
    "L11": "step 2 carries the demo link exactly once, clean, and greets by real first name",
    "L12": "no fake urgency",
}


class LintError(ValueError):
    """The draft cannot be linted at all (unknown channel, bad type)."""


# --------------------------------------------------------------------------- #
# normalization — runs before every rule (PLAN.md §5.4 preamble, §10 row (l))
# --------------------------------------------------------------------------- #

#: Invisible characters a model (or an attacker) can hide inside a banned token.
_INVISIBLE = {
    0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D,
    0x180E, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063,
    0x2064, 0x2066, 0x2067, 0x2068, 0x2069, 0x206A, 0x206B, 0x206C, 0x206D, 0x206E,
    0x206F, 0x3164, 0xFEFF, 0xFFA0, 0xFFF9, 0xFFFA, 0xFFFB,
}
_INVISIBLE.update(range(0xFE00, 0xFE10))          # variation selectors 1-16
_INVISIBLE.update(range(0xE0000, 0xE0080))        # tag characters (invisible smuggling)
_INVISIBLE.update(range(0xE0100, 0xE01F0))        # variation selectors supplement

# --- homoglyph folding: a GENERATED skeleton, not a hand-typed confusables list ---
#
# The hand-maintained dict this replaces *was* the bug: it listed the Greek letters
# somebody thought of and omitted alpha U+03B1, so `fdαi` sailed through L2. A hand
# list is always one entry behind the next lookalike, so the table below is derived
# at import time from the Unicode character database Python already ships:
#
#   * a character whose NFKD base is ASCII is a legitimately accented Latin letter
#     ("José") and is left exactly as it is;
#   * a character whose NFKD base is NOT ASCII is foreign to this corpus, and folds
#     to the ASCII letter its own Unicode NAME identifies — "LATIN LETTER SMALL
#     CAPITAL F" -> f, "CYRILLIC SMALL LETTER ES" -> c, "GREEK SMALL LETTER ALPHA"
#     -> a. Accents on a foreign base are dropped with it, so `fdάi` folds too.
#
# The two maps below are *complete alphabets* (all 24 Greek names, all 33 Cyrillic
# names plus the non-Russian letters that are pure Latin lookalikes), not a shortlist
# of remembered confusables: a letter cannot be omitted the way α was, and every
# codepoint a future Python UCD adds inside the scanned ranges folds automatically.
# `None` means "this letter has no ASCII lookalike" — it is left alone, which is
# correct: nobody reads ж as a Latin letter.
#
# A value may be a (small, capital) pair where the two cases resolve differently:
# ν reads as `v` but Ν reads as `N`; η reads as `n` but Η reads as `H`.

_GREEK_SKELETON: dict[str, str | tuple[str, str] | None] = {
    "ALPHA": "a", "BETA": "b", "GAMMA": None, "DELTA": None, "EPSILON": "e",
    "ZETA": "z", "ETA": ("n", "H"), "THETA": None, "IOTA": "i", "KAPPA": "k",
    "LAMDA": None, "MU": ("u", "M"), "NU": ("v", "N"), "XI": None, "OMICRON": "o",
    "PI": None, "RHO": "p", "SIGMA": None, "FINAL SIGMA": None, "TAU": "t",
    "UPSILON": ("u", "Y"), "PHI": None, "CHI": "x", "PSI": None, "OMEGA": None,
}

_CYRILLIC_SKELETON: dict[str, str | tuple[str, str] | None] = {
    "A": "a", "BE": None, "VE": "b", "GHE": "r", "DE": None, "IE": "e", "ZHE": None,
    "ZE": "3", "I": None, "SHORT I": None, "KA": "k", "EL": None, "EM": "m",
    "EN": ("h", "H"), "O": "o", "PE": None, "ER": "p", "ES": "c", "TE": "t",
    "U": "y", "EF": None, "HA": "x", "TSE": None, "CHE": None, "SHA": None,
    "SHCHA": None, "HARD SIGN": None, "YERU": None, "SOFT SIGN": None, "E": None,
    "YU": None, "YA": None,
    # non-Russian Cyrillic whose glyphs are pure Latin lookalikes
    "BYELORUSSIAN-UKRAINIAN I": "i", "JE": "j", "DZE": "s", "SHHA": "h",
    "QA": "q", "WE": "w", "PALOCHKA": "I",
}

#: Glyphs whose Unicode name carries no letter token at all — the residue the
#: name rule provably cannot derive. Keep this list *small*; anything derivable
#: belongs to the generator, not here.
_EXTRA_FOLD = {"ǀ": "l"}

#: Blocks scanned for foldable letters: Latin-1 supplement through Cyrillic
#: supplement, phonetic extensions (small capitals), Latin extended additional/C/D/E.
_FOLD_RANGES = ((0x00A0, 0x058F), (0x1D00, 0x1DBF), (0x1E00, 0x1EFF),
                (0x2C60, 0x2C7F), (0xA640, 0xA7FF), (0xAB30, 0xAB6F))

_NAME_LETTER = re.compile(r"^(LATIN|GREEK|COPTIC|CYRILLIC)\b.*?\bLETTER\s+(.+)$")


def _skeleton_of(char: str) -> str | None:
    """The ASCII letter `char` renders as, or None if it has no Latin lookalike."""
    name = unicodedata.name(char, "")
    match = _NAME_LETTER.match(name)
    if not match:
        return None
    script, rest = match.groups()
    rest = re.split(r"\s+WITH\s+", rest)[0].strip()      # "O WITH STROKE" -> "O"
    upper = unicodedata.category(char) == "Lu"
    # 1. the name is an alphabet letter-name: ALPHA, ES, ER, OMICRON, ... A `None`
    #    entry is a decision ("no Latin lookalike"), so it stops the lookup here.
    table = _CYRILLIC_SKELETON if script == "CYRILLIC" else _GREEK_SKELETON
    if rest in table:
        value = table[rest]
        if value is None:
            return None
        if isinstance(value, tuple):
            return value[1] if upper else value[0]
        return value.upper() if upper else value.lower()
    # 2. the name names an ASCII letter outright: "DOTLESS I", "SMALL CAPITAL F"
    for token in reversed(rest.split()):
        if len(token) == 1 and token.isascii() and token.isalpha():
            return token if upper else token.lower()
    return None


def _build_fold_table() -> dict[int, str]:
    """Codepoint -> ASCII skeleton, derived from the UCD (see the note above)."""
    table: dict[int, str] = {}
    for low, high in _FOLD_RANGES:
        for code in range(low, high + 1):
            char = chr(code)
            if not unicodedata.category(char).startswith("L"):
                continue
            base = unicodedata.normalize("NFKD", char)[:1]
            if not base or base.isascii():
                continue                    # ASCII base = accented Latin, leave alone
            skeleton = _skeleton_of(base)
            if skeleton:
                table[code] = skeleton
    table.update({ord(k): v for k, v in _EXTRA_FOLD.items()})
    return table


_CONFUSABLE_TABLE = _build_fold_table()

#: Typographic folds NFKC leaves alone (curly quotes) plus dashes.
_TYPOGRAPHIC = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-", "−": "-",
    "‐": "-", "‑": "-",
}
_TYPOGRAPHIC_TABLE = str.maketrans(_TYPOGRAPHIC)

_INVISIBLE_TABLE = dict.fromkeys(_INVISIBLE, None)


def normalize(text: str) -> str:
    """NFKC + invisible strip + homoglyph fold + quote fold. Idempotent.

    This is the single most bypassable point in the module, so it is deliberately
    aggressive: anything that *renders* as ASCII is folded to ASCII before a rule
    ever runs, and the folded text is what ingest stores.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        raise LintError(f"cannot normalize {type(text).__name__}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_INVISIBLE_TABLE)
    text = text.translate(_CONFUSABLE_TABLE)
    text = text.translate(_TYPOGRAPHIC_TABLE)
    return text


def normalize_draft(draft: MessageDraft) -> MessageDraft:
    """Draft with normalized subject/body — what ingest persists."""
    return replace(
        draft,
        subject=None if draft.subject is None else normalize(draft.subject),
        body=normalize(draft.body or ""),
    )


# --------------------------------------------------------------------------- #
# L1 — links, in every form a model or an adversary might emit
# --------------------------------------------------------------------------- #

#: Suffixes that are certainly TLDs. This is NOT the gate — L1 flags any
#: `label.label` token whatever its suffix, because an allowlist of TLDs is always
#: one entry behind whatever a model emits (red-team F1: 50 of 60 sampled TLDs, from
#: `bit.ly` to `voiceclip.contractors`, walked straight through the old 27-entry
#: list). The set below is used only to REFUSE a prose exemption: `st.com` is a
#: domain even though `st.` is a legitimate abbreviation.
_TLD_HINT = frozenset(
    "com net org io ai co us biz info dev app xyz site online link page pro shop "
    "club live me tv cc to gd sh ru de at ly gl uk ca eu in it fr jp nl es se no "
    "services solutions construction build homes email tech contractors design "
    "company agency works today".split()
)
_OBFUSCATED_TLD = rf"(?:{'|'.join(sorted(_TLD_HINT, key=len, reverse=True))}|[A-Za-z]{{2,3}})"

# The spaced-dot pattern is case-sensitive on the TLD on purpose: a capitalized
# ". Com" is a sentence boundary, a lowercase ". com" is an obfuscated domain.
_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("scheme", re.compile(r"\b(?:https?|ftp|file)://", re.I)),
    ("mailto", re.compile(r"\bmailto:", re.I)),
    # tel:/sms:/whatsapp: are links AND dial-outs. sms: in particular routes a cold
    # prospect into the one channel this build excluded on TCPA grounds (SPEC I-10).
    ("dial-out uri", re.compile(r"\b(?:tel|sms|mms|callto|whatsapp|facetime):", re.I)),
    ("www", re.compile(r"\bwww\d?\s*\.", re.I)),
    ("markdown link", re.compile(r"\[[^\]\n]{0,120}\]\(\s*[^)\s]{2,}\s*\)")),
    ("angle-bracketed url", re.compile(r"<\s*(?:https?://|www\.)[^>\s]{1,300}\s*>", re.I)),
    # The obfuscated forms keep a suffix filter the attached form does not need: a
    # known TLD or a short (<=3 char) suffix. Unfiltered, "polka dot pattern" would
    # read as a domain. The miss this leaves — "x dot contractors"-style spelling
    # with a long, unlisted suffix — is the cheaper side of that trade.
    ("obfuscated domain (bracketed dot)",
     re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]{1,61}\s*"
                r"(?:\[\s*(?:\.|dot)\s*\]|\(\s*(?:\.|dot)\s*\))\s*"
                rf"{_OBFUSCATED_TLD}\b", re.I)),
    ("obfuscated domain (spelled dot)",
     re.compile(rf"\b[A-Za-z0-9][A-Za-z0-9-]{{1,61}}\s+dot\s+{_OBFUSCATED_TLD}\b", re.I)),
    # Spaced literal dot. `ai` is excluded here and only here: ". ai is not the
    # answer" is real lowercase prose, and a false positive on it would be worse
    # than the miss (the attached form `x.ai` is still caught by _DOMAIN_SHAPED).
    ("obfuscated domain (spaced dot)",
     re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]{1,61}(?:\s+\.\s*|\s*\.\s+)(?:com|net|org|io)\b")),
    ("punycode domain", re.compile(r"\bxn--[A-Za-z0-9-]{2,}\b", re.I)),
]

#: `label.label(.label)*` with an optional path. Deliberately suffix-blind (F1).
#: The lookbehind excludes `.` and `-` (mid-token) but NOT `@`, so the host half of
#: `alex@acmehardscapeaz.com` is still reached.
_DOMAIN_SHAPED = re.compile(
    r"(?<![\w.\-])"
    r"([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+)"
    r"(/\S*)?"
)
#: A bare IPv4 host, with or without a path — `143.198.22.7/demo` is a link.
_IPV4 = re.compile(
    r"(?<![\w.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})"
    r"(/\S*)?(?!\d)"
)
#: A hostname's last label: 2-24 letters. Anything else ("3.5", "no.2", "a.m") is prose.
_HOSTNAME_TAIL = re.compile(r"[A-Za-z]{2,24}\Z")


def _is_prose_dotted(labels: list[str]) -> bool:
    """True for dotted prose that only *looks* like a host.

    The broad net of `_DOMAIN_SHAPED` is the point (F1), but contractor copy really
    does contain `St. Louis`, `Mt. Lemmon`, `2.5 acre`, `Stoneline Inc.` and plain
    sentence boundaries. Those survive because a dot with a space after it is never
    matched at all; these two exemptions cover the remaining unspaced forms — and
    neither applies when the last label is a known TLD, so `st.com` stays a domain.
    (`_ABBREVIATIONS` lives with the sentence splitter below.)
    """
    if labels[-1].lower() in _TLD_HINT:
        return False
    if all(len(part) == 1 for part in labels[:-1]):
        return True                              # a.m / e.g / U.S.A
    return len(labels) == 2 and labels[0].lower() in _ABBREVIATIONS   # Mt.Lemmon


def _domain_hits(text: str, *, prose_exempt: bool = True) -> list[re.Match]:
    """Every domain-shaped token in `text` that is not dotted prose.

    `prose_exempt=False` drops that forgiveness, for callers scanning a fragment that
    cannot be prose in the first place (the halves of an address). The exemption is
    tuned for contractor copy — `St. Louis`, `a.m`, `e.g` — and off in that context it
    is pure loss: it reads a single-letter first label as an abbreviation, which is
    also the shape of every one-letter shortener.
    """
    hits = []
    for match in _DOMAIN_SHAPED.finditer(text):
        host, path = match.group(1), match.group(2)
        labels = host.split(".")
        if not _HOSTNAME_TAIL.match(labels[-1]):
            continue
        if prose_exempt and path is None and _is_prose_dotted(labels):
            continue
        hits.append(match)
    return hits


def _snippet(text: str, match: re.Match, limit: int = 80) -> str:
    """The match plus the rest of its non-whitespace run, so 'https://' reads as a URL."""
    end = match.end()
    while end < len(text) and not text[end].isspace() and end - match.start() < limit:
        end += 1
    return text[match.start():end].strip()


def _blank(text: str, allowed: tuple[re.Pattern, ...]) -> str:
    """`text` with each allowed URL blanked to spaces — offsets stay valid."""
    for pattern in allowed:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _iter_links(text: str, *, prose_exempt: bool = True) -> list[tuple[str, re.Match]]:
    """Every URL-shaped token in `text`, left to right."""
    hits = [(label, match) for label, pattern in _URL_PATTERNS
            for match in pattern.finditer(text)]
    hits += [("bare domain", match)
             for match in _domain_hits(text, prose_exempt=prose_exempt)]
    hits += [("bare ip address", match) for match in _IPV4.finditer(text)]
    hits.sort(key=lambda pair: pair[1].start())
    return hits


def _check_links(text: str, where: str, out: list[dict],
                 allowed: tuple[re.Pattern, ...] = ()) -> None:
    """L1 over one field. `allowed` blanks a permitted URL (step 2's demo link)."""
    hits = _iter_links(_blank(text, allowed))
    if hits:
        label, match = hits[0]
        out.append({"rule": "L1", "where": where,
                    "detail": f"{label}: {_snippet(text, match)!r} — "
                              "Message 1 carries no links (standing outreach rule)"})


# --------------------------------------------------------------------------- #
# L2 — product identity
# --------------------------------------------------------------------------- #

_SEP = r"[\s._\-*+~]{0,2}"

#: Leet substitutions, so `FD41` and `l3adflow` are the product name too (red-team
#: F2). Digits are folded HERE and not in `normalize()` on purpose: normalize's
#: output is what ingest stores and what the human reads, and rewriting `602-555-0134`
#: into letters would corrupt real copy.
_LEET = {"a": "[a4@]", "e": "[e3]", "i": "[i1!]", "o": "[o0]", "s": "[s5$]",
         "l": "[l1|]", "t": "[t7]", "g": "[g9]", "b": "[b8]"}


def _leet(literal: str, sep: str = "") -> str:
    """Regex source for a product token: leet-tolerant, `sep` between characters."""
    return sep.join(_LEET.get(ch, re.escape(ch)) for ch in literal)


_PRODUCT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("BDAI", re.compile(rf"\b{_leet('bdai', _SEP)}\b", re.I)),
    ("bright desk ai", re.compile(rf"\b{_leet('bright')}{_SEP}{_leet('desk')}{_SEP}"
                                 rf"{_leet('a')}{_SEP}{_leet('i')}\b", re.I)),
    # `brightdesk` only unspaced — "your front desk" is legitimate English.
    ("brightdesk", re.compile(rf"\b{_leet('brightdesk')}\b", re.I)),
    # `brightflow` only unspaced, or spaced when followed by "cure" — "bright flow" is
    # a legitimate (if bland) phrase and must not trip the linter.
    ("brightflow", re.compile(rf"\b{_leet('brightflow')}\b", re.I)),
    ("brightflow cure", re.compile(rf"\b{_leet('bright')}{_SEP}{_leet('flow')}{_SEP}"
                                 rf"{_leet('cure')}\b", re.I)),
]


def _check_product(text: str, where: str, out: list[dict]) -> None:
    for label, pattern in _PRODUCT_PATTERNS:
        match = pattern.search(text)
        if match:
            out.append({"rule": "L2", "where": where,
                        "detail": f"product name {label!r} appears as {match.group(0)!r} — "
                                  "Message 1 names no product"})
            return


# --------------------------------------------------------------------------- #
# L3 / L12 — banned phrases and fake urgency
# --------------------------------------------------------------------------- #

# (label, pattern, scope) — scope: 'any' | 'subject' | 'step1'
_BANNED: list[tuple[str, re.Pattern, str]] = [
    ("i'd love to connect", re.compile(r"\bi'?d love to connect\b", re.I), "any"),
    ("synergy", re.compile(r"\bsynerg\w*\b", re.I), "any"),
    ("circle back", re.compile(r"\bcircle back\b", re.I), "any"),
    ("touch base", re.compile(r"\btouch base\b", re.I), "any"),
    ("just checking in", re.compile(r"\bjust checking in\b", re.I), "any"),
    ("hope this finds you well", re.compile(r"\bhope (?:this|it) finds you well\b", re.I), "any"),
    ("quick question", re.compile(r"\bquick question\b", re.I), "subject"),
    ("revolutionize", re.compile(r"\brevolutioni[sz]\w*\b", re.I), "any"),
    ("game-chang", re.compile(r"\bgame[\s-]?chang\w*\b", re.I), "any"),
    ("cutting-edge", re.compile(r"\bcutting[\s-]?edge\b", re.I), "any"),
    ("streamline", re.compile(r"\bstreamlin\w*\b", re.I), "any"),
    # NARROWED (red-team P1): `solution` used to be banned outright, which rejected
    # "the drainage solution you engineered under that patio" — the ICP's own words
    # for its own work, and the company-name mask only hid it when the name matched
    # exactly. What is actually banned is the agency-speak sense, and that sense is
    # marked by a generic qualifier sitting immediately in front of the noun. A
    # trade modifier ("drainage solution", "grading solution") is not a violation.
    ("solution", re.compile(r"\b(?:an?|the|our|my|your|this|that|one|some|any|every"
                            r"|perfect|simple|complete|total|right|best|ideal|real"
                            r"|smart|turnkey|custom|innovative|scalable|powerful"
                            r"|end.to.end|all.in.one|cutting.edge|game.changing)"
                            r"\s+solutions?\b", re.I), "step1"),
    ("hey there", re.compile(r"\bhey there\b", re.I), "any"),
]

_URGENCY: list[tuple[str, re.Pattern]] = [
    ("act now", re.compile(r"\bact now\b", re.I)),
    ("act fast", re.compile(r"\bact fast\b", re.I)),
    ("limited time", re.compile(r"\blimited[- ]time\b", re.I)),
    ("last chance", re.compile(r"\blast chance\b", re.I)),
    # "last call" / "only 3 spots left" were both unlisted (red-team F5).
    ("last call", re.compile(r"\blast call\b", re.I)),
    ("final chance/notice/call", re.compile(r"\bfinal (?:chance|notice|call)\b", re.I)),
    ("only N spots left",
     re.compile(r"\bonly\s+\d+\s+(?:spots?|slots?|seats?|spaces?|openings?)\s*"
                r"(?:left|remaining|available)?\b", re.I)),
    ("spots filling up",
     re.compile(r"\b(?:spots?|slots?|openings?)\s+(?:are\s+)?(?:filling|going)\b", re.I)),
    ("today only", re.compile(r"\btoday only\b", re.I)),
    ("expires soon", re.compile(r"\bexpires?\s+(?:today|tonight|tomorrow|soon|in\s+\d)", re.I)),
    ("before it's too late", re.compile(r"\bbefore it'?s too late\b", re.I)),
    ("don't miss", re.compile(r"\bdon'?t miss\b", re.I)),
]


def _mask_company(text: str, company_casual: str | None) -> str:
    """Blank out the prospect's own name so 'Sunset Solutions' is not a violation.

    Separator-tolerant (red-team P1): a `company_casual` of "cutting edge" must mask
    "Cutting-Edge Landscape" in the body, or the hyphen alone re-opens the L3 hit.
    Blanks are same-length so offsets stay comparable with the unmasked text.
    """
    if not company_casual:
        return text
    casual = normalize(company_casual).strip()
    if len(casual) < 3:
        return text
    pattern = r"[\s\-_.]+".join(re.escape(word) for word in casual.split())
    return re.sub(pattern, lambda m: " " * len(m.group(0)), text, flags=re.I)


def _check_banned(subject: str, body: str, step: int, company_casual: str | None,
                  out: list[dict]) -> None:
    subject_m = _mask_company(subject, company_casual)
    body_m = _mask_company(body, company_casual)
    for label, pattern, scope in _BANNED:
        if scope == "subject":
            targets = [("subject", subject_m)]
        elif scope == "step1":
            targets = [] if step != 1 else [("subject", subject_m), ("body", body_m)]
        else:
            targets = [("subject", subject_m), ("body", body_m)]
        for where, text in targets:
            match = pattern.search(text)
            if match:
                out.append({"rule": "L3", "where": where,
                            "detail": f"banned phrase {label!r} (as {match.group(0)!r})"})
                break


def _check_urgency(subject: str, body: str, out: list[dict]) -> None:
    for label, pattern in _URGENCY:
        for where, text in (("subject", subject), ("body", body)):
            match = pattern.search(text)
            if match:
                out.append({"rule": "L12", "where": where,
                            "detail": f"fake urgency {label!r} (as {match.group(0)!r})"})
                break


# --------------------------------------------------------------------------- #
# L4 — emoji
# --------------------------------------------------------------------------- #

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),   # all emoji blocks incl. flags, skin tones, symbols
    (0x2600, 0x26FF),     # miscellaneous symbols
    (0x2700, 0x27BF),     # dingbats
    (0x2B00, 0x2BFF),     # arrows/stars used as emoji
    (0x1F1E6, 0x1F1FF),   # regional indicators
    (0x2100, 0x214F),     # letterlike symbols (ℹ) — red-team F8
    (0x2190, 0x21FF),     # arrows (↗)
    (0x25A0, 0x25FF),     # geometric shapes (▶)
    (0x2934, 0x2935),     # curved arrows
    (0x3297, 0x3299),     # circled ideographs used as emoji
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x2049, 0x2049),
    (0x203C, 0x203C),
)


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _EMOJI_RANGES)


def _check_emoji(subject: str, body: str, out: list[dict]) -> None:
    for where, text in (("subject", subject), ("body", body)):
        for char in text:
            if _is_emoji(char):
                out.append({"rule": "L4", "where": where,
                            "detail": f"emoji {char!r} (U+{ord(char):04X}) — no emoji "
                                      "unless the recipient used one first"})
                break


# --------------------------------------------------------------------------- #
# L5 — unresolved placeholders and token-shaped holes
# --------------------------------------------------------------------------- #

_PLACEHOLDER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("brace placeholder", re.compile(r"[{}]")),
    ("bracket placeholder", re.compile(r"[\[\]]")),
    ("angle placeholder", re.compile(r"<\s*[A-Za-z][A-Za-z0-9 _.\-]{0,40}\s*>")),
    ("percent placeholder", re.compile(r"%{1,2}[A-Za-z_][A-Za-z0-9_]{1,40}%{1,2}")),
    ("dollar placeholder", re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]{1,40}\}?")),
    ("literal token name",
     re.compile(r"\b(?:first_name|firstname|last_name|lastname|company_name|companyname"
                r"|company_casual|hook_fact|hookfact|merge_field|owner_name)\b", re.I)),
]

_GREETING = r"(?:hey|hi|hello|good (?:morning|afternoon|evening))"
_GREETING_NO_NAME = re.compile(rf"^\s*{_GREETING}\s*[,.!:;]", re.I)
_GREETING_LINE = re.compile(rf"^{_GREETING}\b[^.?!]{{0,40}}[,:\-]?$", re.I)
_GREETING_NAME = re.compile(rf"^{_GREETING}\b[ \t]+(.+?)[ \t]*[,.!:;\-]*$", re.I)

#: What a dropped merge token renders as. Shipping "Hey None," is the loudest
#: automation tell there is, delivered to exactly the skeptic this system exists to
#: convert (red-team F3). Scoped to the greeting because that is the slot the token
#: lands in — bare "none"/"null" elsewhere are ordinary English.
_RENDERED_NULL = {"none", "null", "nil", "nan", "n/a", "na", "undefined", "unknown",
                  "0", "-", "--", "?", "tbd", "xx", "xxx", "false", "true", "[]",
                  "{}", "()", "first name", "firstname", "name"}

#: Inbox-role words that are not a person's name. "there" is absent on purpose:
#: L3 already bans "hey there" and two rules firing on one defect reads as noise.
_ROLE_GREETING = {"owner", "info", "sales", "team", "admin", "administrator",
                  "contact", "manager", "office", "support", "service", "billing",
                  "webmaster", "customer", "client", "friend", "sir", "madam",
                  "boss", "buddy", "folks", "guys", "everyone"}
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+[,;.](?:\s|$)")
_DOUBLE_COMMA = re.compile(r",\s*,")
_LEADING_COMMA = re.compile(r"^\s*,", re.M)
_DOUBLE_SPACE = re.compile(r"(?<![.!?:])[ ]{2,}")
_MID_CLAUSE_START = re.compile(r"^\s*(?:[,;:.\-]|and\b|but\b|or\b)", re.I)

#: A hook this short is an empty slot where a fact should be (PLAN.md §5.3).
MIN_HOOK_CHARS = 20
#: Length alone let `nice work out there.` and `aaaaaaaaaaaaaaaaaaaa` through as
#: "hooks" (red-team F3), so the hook also has to carry content: enough words, and
#: enough *different* words, to be a sentence about this prospect rather than filler.
MIN_HOOK_WORDS = 6
MIN_HOOK_DISTINCT_WORDS = 5

#: Praise with nothing concrete attached. Bounded to the whole hook and to a short
#: tail, so "nice work on the flagstone patio off Camelback last month" — praise
#: plus a fact — is untouched.
_FILLER_HOOK = re.compile(
    r"^(?:nice|great|good|solid|awesome|amazing|impressive|cool|love (?:the|your)"
    r"|just saw|saw (?:the|your))\s+"
    r"(?:work|stuff|job|jobs|site|website|page|photos?|pics?|pictures?|projects?"
    r"|posts?|feed|business|company)\b[^.?!]{0,20}[.!]?$", re.I)


def _hook_text(body: str) -> str:
    """First substantive line: the personalization hook, greeting line removed."""
    lines = body.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    first = lines[index].strip() if index < len(lines) else ""
    if first and _GREETING_LINE.match(first):
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        first = lines[index].strip() if index < len(lines) else ""
    return first


def _greeting_name(body: str) -> str | None:
    """Whatever sits in the first-name slot of the opening line, or None."""
    for line in body.splitlines():
        if not line.strip():
            continue
        match = _GREETING_NAME.match(line.strip())
        name = match.group(1).strip() if match else ""
        return name or None
    return None


def _check_greeting(body: str, company_casual: str | None, out: list[dict]) -> None:
    """The first-name slot holds a person's name, or the message is not sendable."""
    name = _greeting_name(body)
    if name is None:
        return
    folded = name.casefold().strip(".,!:; ")
    if folded in _RENDERED_NULL:
        out.append({"rule": "L5", "where": "body",
                    "detail": f"greeting reads {name!r} — a merge token rendered to a "
                              "null value instead of failing"})
        return
    if folded in _ROLE_GREETING:
        out.append({"rule": "L5", "where": "body",
                    "detail": f"greeting reads {name!r} — an inbox role, not a person; "
                              "use the owner's first name or a name-free opener"})
        return
    suffix = _SUFFIX_NOISE.search(name)
    casual = normalize(company_casual or "").strip().casefold()
    if suffix or len(name.split()) > 2 or (casual and len(casual) >= 3
                                           and casual in folded):
        out.append({"rule": "L5", "where": "body",
                    "detail": f"greeting reads {name!r} — that is the company in the "
                              "first-name slot, not a person"})


def _check_hook(hook: str, out: list[dict]) -> None:
    """The hook must be a specific sentence about this prospect, not filler."""
    if len(hook) < MIN_HOOK_CHARS:
        out.append({"rule": "L5", "where": "body",
                    "detail": f"hook is {len(hook)} chars ({hook!r}) — under "
                              f"{MIN_HOOK_CHARS}, the personalization is missing"})
        return
    if _MID_CLAUSE_START.match(hook):
        out.append({"rule": "L5", "where": "body",
                    "detail": f"hook starts mid-clause: {hook[:40]!r}"})
    words = hook.split()
    if len(words) < MIN_HOOK_WORDS or len({w.casefold() for w in words}) < MIN_HOOK_DISTINCT_WORDS:
        out.append({"rule": "L5", "where": "body",
                    "detail": f"hook carries {len(words)} words ({hook[:40]!r}) — that is "
                              "filler, not a fact about this prospect"})
    elif _FILLER_HOOK.match(hook):
        out.append({"rule": "L5", "where": "body",
                    "detail": f"hook is generic praise with nothing concrete in it "
                              f"({hook[:40]!r}) — name the project, the year, the crew"})


def _check_placeholders(subject: str, body: str, main: str,
                        company_casual: str | None, out: list[dict]) -> None:
    """`body` includes any footer block; `main` is the message above it."""
    for where, text in (("subject", subject), ("body", body)):
        for label, pattern in _PLACEHOLDER_PATTERNS:
            match = pattern.search(text)
            if match:
                out.append({"rule": "L5", "where": where,
                            "detail": f"unresolved {label}: {match.group(0)!r} — a literal "
                                      "token shipping marks the message as automated"})
                break

    # semantic empties: the token was dropped, not left literal
    if main and main.splitlines() and not main.splitlines()[0].strip() and main.strip():
        out.append({"rule": "L5", "where": "body",
                    "detail": "body starts with an empty line — dropped opener"})
    if _GREETING_NO_NAME.search(main):
        out.append({"rule": "L5", "where": "body",
                    "detail": "greeting has no name ('Hi ,') — first_name did not render"})
    else:
        _check_greeting(main, company_casual, out)
    for label, pattern in (("space before punctuation", _SPACE_BEFORE_PUNCT),
                           ("double comma", _DOUBLE_COMMA),
                           ("line starts with a comma", _LEADING_COMMA),
                           ("double space", _DOUBLE_SPACE)):
        match = pattern.search(main)
        if match:
            out.append({"rule": "L5", "where": "body",
                        "detail": f"{label} at {match.group(0)!r} — token-shaped hole"})
    hook = _hook_text(main)
    if not hook:
        out.append({"rule": "L5", "where": "body", "detail": "no hook sentence at all"})
    else:
        _check_hook(hook, out)


# --------------------------------------------------------------------------- #
# sentence splitting (shared by L7 and L8)
# --------------------------------------------------------------------------- #

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "ave", "blvd", "rd", "inc", "co", "corp", "ltd",
    "llc", "jr", "sr", "vs", "etc", "approx", "dept", "est", "eg", "ie", "us", "am",
    "pm", "no", "fig", "min", "max", "sq", "ft", "yr",
}


def split_sentences(text: str) -> list[str]:
    """Split on `.!?` with an abbreviation and decimal guard."""
    text = (text or "").strip()
    if not text:
        return []
    sentences: list[str] = []
    start = index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char not in ".!?":
            index += 1
            continue
        end = index
        while end + 1 < length and text[end + 1] in ".!?":
            end += 1
        nxt = text[end + 1] if end + 1 < length else ""
        prev = re.search(r"([A-Za-z0-9.]+)$", text[start:index])
        word = prev.group(1).lower().replace(".", "") if prev else ""
        decimal = char == "." and nxt.isdigit()
        abbrev = char == "." and (word in _ABBREVIATIONS or len(word) == 1)
        if not decimal and not abbrev:
            chunk = text[start:end + 1].strip()
            if chunk:
                sentences.append(chunk)
            start = end + 1
        index = end + 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


#: "thanks," / "cheers," on the line above the name. These are the two commonest
#: sign-offs in cold email; leaving them in made L8 report "the question is not the
#: closing sentence (closes with 'thanks,')" — blaming the wrong rule (red-team P2).
_CLOSER_LINE = re.compile(
    r"(?:thanks(?:\s+again)?|thank you|cheers|best|all the best|regards|"
    r"best regards|talk soon|appreciate it|appreciate you|either way)[,.!]?", re.I)
#: The signature itself: `the operator`, or `- the operator` (normalize folds any dash to `-`).
_SIGNATURE_LINE = re.compile(rf"-{{0,2}}\s*{SIGNATURE}", re.I)


def _strip_signature(body: str) -> str:
    """Body without its trailing sign-off block — a sign-off is not a sentence.

    Deliberately tolerant of a *wrong* sign-off ("Sam, Founder"): L6 already
    reports that, and a single defect should not also make L8 complain that the
    question is not the closing sentence.
    """
    lines = body.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.fullmatch(r"[-\s]*sam\b.{0,60}", lines[-1].strip(), re.I):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _CLOSER_LINE.fullmatch(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# the CAN-SPAM footer block
# --------------------------------------------------------------------------- #

#: Cold B2B email is a commercial message under 15 U.S.C. §7704(a)(5), which wants a
#: postal address and a working opt-out. L1 (no links), L6 (last line is exactly
#: `the operator`) and L8 (the question closes the message) interlocked so that no such
#: footer could pass — nobody decided that, it was emergent. So: an OPTIONAL,
#: clearly delimited footer block, introduced by the RFC 3676 `--` line, exempt from
#: L6/L8 — and from L6/L8 ONLY, which is exactly what decision D-24 authorises. It is
#: never added, never required, and stays bounded — the size cap is what stops `--` on
#: line two from exempting the whole message.
#:
#: L1 keeps running inside the footer. The single narrow carve-out is the opt-out
#: mechanism itself, and it is earned by matching the LINK (`_OPT_OUT_LINK`), never by
#: finding the word "unsubscribe" on a neighbouring line: keying the exemption off a
#: nearby keyword let *any* single link ride into Message 1 behind an opt-out line,
#: which is the thing hard rule 5 exists to stop. A plain reply-to address
#: (`_FOOTER_EMAIL`) is not a second carve-out: it is not a link in the first place.
#:
#: L2/L3/L4/L12 still apply inside it. That is deliberate: trigger G6 greps the WHOLE
#: body for the product name, so exempting L2 here would only move the failure from a
#: readable lint violation to a database abort. G6 is *not* a backstop for this
#: though — its LIKE list is `brightflowcure`/`BDAI`/`bright desk ai`, which misses
#: `BrightFlow Cure` (spaced), `brightdesk` and `bright-desk ai`. Lint is the only layer
#: that sees those, so L2 has to actually run here.
_FOOTER_DELIMITER = re.compile(r"^[ \t]*--[ \t]*$", re.M)
MAX_FOOTER_LINES = 4
MAX_FOOTER_WORDS = 40
#: The domains the engine sends from, and so the only hosts a footer opt-out link may
#: point at. Without a host check the carve-out is not "our opt-out mechanism", it is
#: "any host wearing an opt-out-shaped path" — `bit.ly/unsubscribe`,
#: `evil-pay.xyz/unsubscribe` and `143.198.22.7/unsubscribe` all wear one.
#: `brightflowcure.com` is the registrable domain under `DEMO_LINK`'s host and
#: `northwindmedia.com` is the business the CAN-SPAM postal address belongs to; nothing
#: else is ours. (In Message 1 the `brightflowcure.com` entry is moot — L2 refuses the
#: product name in the footer too — it is there for the step-2 footer.)
SENDING_DOMAINS = ("northwindmedia.com", "brightflowcure.com")

_SENDING_HOST = (r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)*"
                 r"(?:" + "|".join(re.escape(d) for d in SENDING_DOMAINS) + r")")

#: The one link a footer may carry: OUR opt-out mechanism, on a sending host, with the
#: opt-out token as a whole path segment. Scheme-OPTIONAL on purpose —
#: `northwindmedia.com/unsubscribe` is a link that neither a scheme-anchored pattern nor
#: trigger G6 (`LIKE '%http%'`, `'%www.%'`) can see, and a bare domain was how the old
#: keyword exemption laundered arbitrary links.
#:
#: Held to the bar `_DEMO_DIRTY` already holds the Message 2 demo link to, because it
#: is the same attack: NO query string, NO fragment, no `&`/`=`, no `:` and no dot
#: inside a path segment, and the link has to END at whitespace. A tail that does not
#: fit means this pattern does not match AT ALL — the host then falls through to
#: `_iter_links` and the footer is an L1 violation. That is the whole design: a
#: permissive tail (`[^\s<>\]):]*`) swallowed a second link into the blank left by the
#: first, so `/unsubscribe?to=evil-pay.xyz/inv`, `/unsubscribe#evil-pay.xyz/inv` and
#: `/unsubscribe,evil-pay.xyz/inv` all shipped. The scheme-ful `?next=https://…` form
#: was stopped only by the `:` in `https`, which is not a rule, it is a coincidence.
#: `opt[-_]?out` must be a segment, not a prefix of one: `/optoutlet/deals-now` is a
#: pitch. The lookbehind stops `evilnorthwindmedia.com/unsubscribe` matching on its tail.
_OPT_OUT_LINK = re.compile(
    r"(?<![^\s<>(\[])"                       # a token of its own, not a host suffix
    r"(?:https?://)?" + _SENDING_HOST +
    r"(?:/[A-Za-z0-9_~%+-]+)*"                # path: no dot, no ':', no '?#&='
    r"/(?:unsubscribe|opt[-_]?out)"           # the token, as a WHOLE segment
    r"(?:/[A-Za-z0-9_~%+-]+)*/?"              # opaque per-recipient token segments
    r"(?=[.,;!)\]>]*(?:\s|\Z))", re.I)       # and it ends there

#: A reply-to address is a normal CAN-SPAM footer element, not a link at all.
#: `_DOMAIN_SHAPED`'s lookbehind reaches through `@` on purpose — in the MESSAGE an
#: address is a contact channel and hard rule 5 forbids it (and a test pins that) —
#: but that same reach read the host half of a plain reply-to address in a FOOTER as
#: a bare-domain link, which is a false positive on the most ordinary footer there is.
#: `_blank_addresses` blanks the address there, and ONLY where it is an address and
#: nothing more: no path, no query, no fragment, nothing glued on, held to the same
#: end-at-whitespace bar as `_OPT_OUT_LINK`. So an address with `/inv` welded onto it
#: does not match here at all, its host falls through to `_iter_links`, and it stays
#: an L1 violation — an address is not permission to carry a destination. The host
#: must end in a letter TLD, which leaves an address on a bare IP host a bare-IP link.
#: Both halves are captured, because both have to be judged separately below.
_LOCAL_LABEL = r"[A-Za-z0-9_%+\-]+"
_FOOTER_EMAIL = re.compile(
    r"(?<![^\s<>(\[])"                                        # a token of its own
    rf"({_LOCAL_LABEL}(?:\.{_LOCAL_LABEL})*)@"                # local part, captured
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+"      # host, captured
    r"[A-Za-z]{2,24})"                                        # letter TLD, never an IP
    r"(?=[.,;!)\]>]*(?:\s|\Z))", re.I)                        # and it ends there


def _address_half_links(half: str) -> list[tuple[str, re.Match]]:
    """L1's own detection over one half of an address, judged as a host not as prose.

    Neither half of an address is contractor copy, so `_domain_hits`' prose exemption
    is switched off here: with it on, a single-letter first label reads as `a.m`/`e.g`
    and every one-letter shortener rides in behind that.
    """
    return _iter_links(half, prose_exempt=False)


def _host_is_only_a_host(host: str) -> bool:
    """True when `host` is a plain bare domain and nothing more link-shaped than that.

    Being a bare domain is the ONE thing the carve-out forgives — that is the entire
    false positive it exists to fix. Every other shape L1 sees in a host is one it
    sees everywhere else in the module, and an `@` in front does not make it stop
    being one: a `www.` prefix, punycode, an embedded bare IP. So the test is not "is
    there a hit" (a host always has one) but "is that single hit the bare domain, and
    does it span the whole host".
    """
    hits = _address_half_links(host)
    return (len(hits) == 1 and hits[0][0] == "bare domain"
            and hits[0][1].group(0) == host)


def _blank_addresses(footer: str, refused: list | None = None) -> str:
    """`footer` with each plain reply-to address blanked — offsets stay valid.

    The blank covers the WHOLE token, so both halves ride in behind it and both have
    to earn it. The local half has to be clean of link shapes outright — a local part
    has no reason to be one, and a shortener, a live-gTLD domain, a bare IP or
    punycode in front of the `@` is the same reachable destination it would be
    anywhere else in the message. Guarding it with a TLD ALLOWLIST instead was exactly
    the red-team F1 failure `_TLD_HINT`'s own docstring warns about: the gTLD space is
    unbounded, so every suffix off the list walked straight through.

    Asking L1's own patterns is what keeps this from drifting — there is no second
    list to maintain, and a shape added to `_URL_PATTERNS` is covered for free. The
    price is that a dotted local part whose last label is hostname-shaped is refused
    the exemption too, `firstname.lastname@` included: it is indistinguishable from a
    shortener by construction, and the safe side of that trade is one optional footer
    line the author rewrites, not a live link in a Message 1 that reached READY.

    Judging the halves is not quite enough on its own, because a link can straddle the
    token boundary and so belong to neither half: in `bit dot ly@` the local part is
    `ly`, which is nothing, while the spelled-dot domain the blank destroys started
    two words earlier. So the last condition is the general form of all of the above —
    blanking may only remove link hits that lie WHOLLY INSIDE the address, which for a
    real address is exactly one, its own host.

    An address that fails any of these is left standing, and every refusal is also
    appended to `refused` when a list is passed. Leaving the token in place is NOT on
    its own enough to make it visible: the caller's reporting pass is the ordinary
    `prose_exempt=True` one, and a refused address whose halves are BOTH prose-exempt is
    rediscovered by nobody (a one-letter first label reads as `e.g`, and so can the host),
    so the refusal is silent and the message ships. The judging pass here is the strict one,
    so it is the only pass that knows; it has to say so rather than trust a more forgiving
    scan to find the same token again. See `tests/test_lint.py` for the worked examples —
    a literal address in this file trips the no-hardcoded-address adversarial gate.
    """
    spans = [match.span() for _label, match in _iter_links(footer)]

    def judge(match: re.Match) -> str:
        local, host = match.group(1), match.group(2)
        start, end = match.span()
        if _address_half_links(local) or not _host_is_only_a_host(host):
            if refused is not None:
                refused.append(match)
            return match.group(0)
        if any(hit_start < start or hit_end > end for hit_start, hit_end in spans
               if hit_start < end and hit_end > start):
            if refused is not None:
                refused.append(match)
            return match.group(0)
        return " " * len(match.group(0))
    return _FOOTER_EMAIL.sub(judge, footer)


def split_footer(body: str) -> tuple[str, str]:
    """(message, footer) around the `--` delimiter line. Footer is '' when absent."""
    match = _FOOTER_DELIMITER.search(body)
    if not match:
        return body, ""
    return body[:match.start()].rstrip(), body[match.end():].strip()


def _check_footer(footer: str, step: int, out: list[dict]) -> None:
    if not footer:
        return
    lines = [line for line in footer.splitlines() if line.strip()]
    words = len(footer.split())
    if len(lines) > MAX_FOOTER_LINES:
        out.append({"rule": "L6", "where": "footer",
                    "detail": f"footer is {len(lines)} lines, limit {MAX_FOOTER_LINES} — "
                              "the block below '--' is an address and an opt-out, "
                              "not more message"})
    if words > MAX_FOOTER_WORDS:
        out.append({"rule": "L6", "where": "footer",
                    "detail": f"footer is {words} words, limit {MAX_FOOTER_WORDS}"})
    if step == 1:
        _check_product(footer, "footer", out)
    opt_outs = _OPT_OUT_LINK.findall(footer)
    refused: list = []
    hits = _iter_links(_blank_addresses(_blank(footer, (_OPT_OUT_LINK,)), refused))
    if not hits and refused:
        # The strict judging pass refused to blank this address because a half of it is
        # link-shaped, but the forgiving reporting pass above cannot see it. Report the
        # refusal itself, or the refusal is silent and the link ships.
        hits = [("address carrying a link", refused[0])]
    if hits:
        label, match = hits[0]
        out.append({"rule": "L1", "where": "footer",
                    "detail": f"{label}: {_snippet(footer, match)!r} — the only link a "
                              "footer may carry is a clean opt-out path on a sending "
                              f"domain {SENDING_DOMAINS!r}: no query string, no "
                              "fragment, nothing glued on after it"})
    elif len(opt_outs) > 1:
        out.append({"rule": "L1", "where": "footer",
                    "detail": f"{len(opt_outs)} opt-out links in the footer — one is the "
                              "most Message 1 may carry"})


# --------------------------------------------------------------------------- #
# L6 / L7 — channel shape, driven by the supplied constraints
# --------------------------------------------------------------------------- #


def resolve_constraints(channel: str, constraints=None) -> Constraints:
    """Merge a caller-supplied constraints object over the channel default.

    Any attribute the caller's object declares wins (even when it is None, which
    means "no limit"); anything it omits falls back to `DEFAULT_CONSTRAINTS`.
    """
    base = DEFAULT_CONSTRAINTS.get(channel)
    if base is None and constraints is None:
        raise LintError(f"unknown channel {channel!r} and no constraints supplied")
    if base is None:
        base = Constraints()
    if constraints is None:
        return base
    values = {}
    for name in ("max_words", "max_sentences", "max_chars", "has_subject",
                 "max_subject_chars", "requires_signature"):
        values[name] = getattr(constraints, name) if hasattr(constraints, name) \
            else getattr(base, name)
    return Constraints(**values)


def _check_shape(subject: str | None, body: str, limits: Constraints,
                 out: list[dict]) -> None:
    """Length/shape rules. Rule id follows the channel form, not the channel name."""
    long_form = bool(limits.has_subject)
    rule = "L6" if long_form else "L7"

    if limits.max_words is not None:
        words = len(body.split())
        if words > limits.max_words:
            out.append({"rule": rule, "where": "body",
                        "detail": f"{words} words, limit {limits.max_words}"})
    if limits.max_chars is not None and len(body) > limits.max_chars:
        out.append({"rule": rule, "where": "body",
                    "detail": f"{len(body)} characters, limit {limits.max_chars}"})
    if limits.max_sentences is not None:
        count = len(split_sentences(_strip_signature(body)))
        if count > limits.max_sentences:
            out.append({"rule": rule, "where": "body",
                        "detail": f"{count} sentences, limit {limits.max_sentences}"})

    if long_form:
        if subject is None or not subject.strip():
            out.append({"rule": "L6", "where": "subject", "detail": "subject is required"})
        else:
            if subject != subject.lower():
                out.append({"rule": "L6", "where": "subject",
                            "detail": f"subject must be entirely lowercase: {subject!r}"})
            if limits.max_subject_chars is not None and len(subject) > limits.max_subject_chars:
                out.append({"rule": "L6", "where": "subject",
                            "detail": f"subject is {len(subject)} chars, limit "
                                      f"{limits.max_subject_chars}"})
    elif subject is not None and subject.strip():
        out.append({"rule": "L7", "where": "subject",
                    "detail": f"this channel carries no subject, got {subject!r}"})

    if limits.requires_signature:
        lines = [line.strip() for line in body.strip().splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        if not _SIGNATURE_LINE.fullmatch(last):
            out.append({"rule": "L6", "where": "body",
                        "detail": f"must be signed {SIGNATURE!r} (or '- {SIGNATURE}') on "
                                  f"its own last line — no title, phone, or link; "
                                  f"found {last!r}"})


# --------------------------------------------------------------------------- #
# L8 — one CTA
# --------------------------------------------------------------------------- #


#: A phone number is a stronger CTA than the soft question beside it, and it was
#: uncounted (red-team F4). NANP shapes, 10 digits minimum, separators optional.
_PHONE = re.compile(r"(?<!\w)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}"
                    r"[\s.\-]?\d{4}(?!\d)")

#: How much statement has to precede a question inside its own paragraph before the
#: question reads as rhetorical rather than as a second ask (red-team P3):
#: "saw the 40-ton flagstone patio you set in nine days - how do you even move that
#: much stone?" is one CTA; a paragraph that IS the question is another CTA.
MIN_RHETORICAL_PREAMBLE = 20
_CLAUSE_BREAK = re.compile(r"[-;:,.!?]")


def _is_rhetorical(text: str, index: int) -> bool:
    """True when the question at `index` trails a statement in the same paragraph."""
    start = text.rfind("\n\n", 0, index)
    start = 0 if start < 0 else start + 2
    breaks = list(_CLAUSE_BREAK.finditer(text, start, index))
    clause_start = breaks[-1].end() if breaks else start
    return len(text[start:clause_start].strip()) >= MIN_RHETORICAL_PREAMBLE


def _check_phone_cta(body: str, out: list[dict]) -> None:
    match = _PHONE.search(body)
    if match:
        out.append({"rule": "L8", "where": "body",
                    "detail": f"phone number {match.group(0)!r} is a second CTA — one "
                              "CTA per message, and the reply is the CTA"})


def _check_one_cta(body: str, out: list[dict]) -> None:
    stripped = _strip_signature(body)
    sentences = split_sentences(stripped)
    closing = sentences[-1] if sentences else ""
    tail = stripped.rfind(closing) if closing else len(stripped)

    if not closing.rstrip().endswith("?"):
        detail = (f"the question is not the closing sentence (closes with "
                  f"{closing[:60]!r})") if "?" in stripped else \
                 "no question at all — one soft question closes Message 1"
        out.append({"rule": "L8", "where": "body", "detail": detail})
    extra = [m for m in re.finditer(r"\?", stripped)
             if m.start() < tail and not _is_rhetorical(stripped, m.start())]
    if extra:
        out.append({"rule": "L8", "where": "body",
                    "detail": f"{len(extra) + 1} question marks — exactly one soft "
                              "question closes Message 1 (one CTA per message); a "
                              "rhetorical question inside the hook does not count"})
    _check_phone_cta(stripped, out)


def _check_step2_cta(body: str, out: list[dict]) -> None:
    """Message 2's CTA is the demo link, so it obeys the same one-CTA discipline."""
    stripped = _strip_signature(body)
    count = stripped.count("?")
    if count > 1:
        out.append({"rule": "L8", "where": "body",
                    "detail": f"{count} question marks — Message 2's CTA is the demo "
                              "link, so at most one question sits beside it"})
    _check_phone_cta(stripped, out)


# --------------------------------------------------------------------------- #
# L9 — subject uniqueness
# --------------------------------------------------------------------------- #


def subject_key(subject: str | None) -> str:
    """Comparison form for uniqueness: normalized, depunctuated, singularized, folded.

    Casefolding and whitespace alone were beaten by a trailing full stop (red-team
    F6): `missing calls.` / `missing calls!` / `missing calls -` / `missing call` all
    read as distinct from `missing calls`, so a 20-message batch could ship 20
    subjects differing by one character — exactly the bulk-mail fingerprint L9
    exists to prevent. Punctuation is dropped wherever it sits and a trailing plural
    `s` with it; the key only ever over-collapses, which fails toward "vary it more".
    """
    text = re.sub(r"[^\w\s]+", " ", normalize(subject or "").casefold())
    words = [word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss")
             else word for word in text.split()]
    return " ".join(words)


def _check_subject_unique(subject: str | None, batch_subjects, recent_subjects,
                          out: list[dict]) -> None:
    key = subject_key(subject)
    if not key:
        return
    if key in {subject_key(s) for s in (batch_subjects or ())}:
        out.append({"rule": "L9", "where": "subject",
                    "detail": f"subject {subject!r} is already used in this batch — "
                              "cadence rule 1: vary the subject per prospect"})
        return
    if key in {subject_key(s) for s in (recent_subjects or ())}:
        out.append({"rule": "L9", "where": "subject",
                    "detail": f"subject {subject!r} was staged within the last 30 days"})


# --------------------------------------------------------------------------- #
# L10 — casualization sanity
# --------------------------------------------------------------------------- #

_SUFFIX_NOISE = re.compile(r"\b(?:llc|l\.l\.c|inc|incorporated|corp|corporation|ltd"
                           r"|limited|co\.|pllc|lp|llp)\b", re.I)


def _check_company_casual(company_casual: str | None, out: list[dict]) -> None:
    if company_casual is None or not company_casual.strip():
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": "company_casual is required before compose (run /casualize-names)"})
        return
    casual = normalize(company_casual).strip()
    if not casual:
        # A zero-width-only value survives `.strip()` and is emptied by normalize().
        # The old code then indexed casual[-1] and raised IndexError *inside* the
        # ingest loop, aborting a batch mid-commit (red-team F7). It is a lint
        # violation, not a crash.
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": f"{company_casual!r} normalizes to nothing — the "
                              "casual name is invisible characters only"})
        return
    match = _SUFFIX_NOISE.search(casual)
    if match:
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": f"legal suffix {match.group(0)!r} left in {casual!r}"})
    letters = [c for c in casual if c.isalpha()]
    if len(letters) >= 3 and casual == casual.upper():
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": f"{casual!r} is ALL CAPS — reads as shouting"})
    words = casual.split()
    if len(words) > 4:
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": f"{casual!r} is {len(words)} words — casual names are <=4"})
    if casual[-1] in ".,;:!?":
        out.append({"rule": "L10", "where": "company_casual",
                    "detail": f"{casual!r} ends in punctuation"})


# --------------------------------------------------------------------------- #
# L11 — step 2
# --------------------------------------------------------------------------- #


#: The one URL Message 2 may carry, in the one form it may take. Blanked out of the
#: body before L1 runs, so everything else in a Message 2 is still a link violation.
DEMO_URL = f"https://{DEMO_LINK}"
_DEMO_ALLOWED = (re.compile(re.escape(DEMO_URL), re.I),)
#: The demo host wearing a query string, a fragment or an appended label —
#: `...demo-ai-voice?next=https://evil-pay.xyz/inv` is an open redirect, not the demo.
_DEMO_DIRTY = re.compile(re.escape(DEMO_LINK) + r"(?:[?#&=]|\.[A-Za-z])\S*", re.I)


def _check_step2(body: str, first_name: str | None, out: list[dict]) -> None:
    hits = body.lower().count(DEMO_LINK.lower())
    if hits != 1:
        out.append({"rule": "L11", "where": "body",
                    "detail": f"demo link {DEMO_LINK!r} appears {hits} times — Message 2 "
                              "carries it exactly once"})
    elif not re.search(re.escape(DEMO_URL), body, re.I):
        out.append({"rule": "L11", "where": "body",
                    "detail": f"the demo link must be the exact TLS form {DEMO_URL!r}"})
    dirty = _DEMO_DIRTY.search(body)
    if dirty:
        out.append({"rule": "L11", "where": "body",
                    "detail": f"the demo link carries {dirty.group(0)!r} — no query "
                              "string, fragment or appended host on the demo link"})
    if first_name and first_name.strip():
        head = body[:80].lower()
        if first_name.strip().lower() not in head:
            out.append({"rule": "L11", "where": "body",
                        "detail": f"greeting must use the real first name "
                                  f"{first_name.strip()!r}"})


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #


def lint_message(draft: MessageDraft,
                 batch_subjects: list[str] | None = None,
                 recent_subjects: list[str] | None = None,
                 company_casual: str | None = None,
                 *,
                 constraints=None,
                 first_name: str | None = None) -> LintReport:
    """Run every applicable rule over a normalized draft (PLAN.md §5.4).

    Positional parameters are exactly PLAN.md §12.4. `constraints` and `first_name`
    are additive keyword-only extensions: constraints because per-channel limits are
    Owner D's `ChannelConstraints` and must arrive as data rather than an import;
    first_name because L11's greeting check cannot be done without it.
    """
    if not isinstance(draft, MessageDraft):
        raise LintError("lint_message expects a MessageDraft")
    step = int(draft.sequence_step or 1)
    if step not in (1, 2):
        # Fail closed (red-team F9). The sequence is two messages; a step this module
        # has no rule set for used to return ok=True with every content rule skipped.
        # Unreachable today (schema CHECK + compose._validate_row), and it stays that
        # way because the law refuses inputs it does not understand.
        raise LintError(f"sequence_step {step!r} is not 1 or 2 — lint has no rule set "
                        "for it and will not pass a message it did not check")
    limits = resolve_constraints(draft.channel, constraints)
    subject = None if draft.subject is None else normalize(draft.subject)
    body = normalize(draft.body or "")
    # An optional CAN-SPAM footer below a `--` line is exempt from L6/L8 (D-24) and
    # from nothing else; the message above it is what the copy rules judge.
    message, footer = split_footer(body)

    out: list[dict] = []

    # L1: step 1 carries no links at all; step 2 carries the demo link and nothing
    # else — the old `if step == 1` skipped the check entirely and let a second URL,
    # an open redirect and a non-TLS demo link through (red-team F5).
    allowed = () if step == 1 else _DEMO_ALLOWED
    for where, text in (("subject", subject or ""), ("body", message)):
        _check_links(text, where, out, allowed)
        if step == 1:
            _check_product(text, where, out)
    _check_banned(subject or "", body, step, company_casual, out)
    _check_emoji(subject or "", body, out)
    _check_placeholders(subject or "", body, message, company_casual, out)
    _check_shape(subject, message, limits, out)
    _check_footer(footer, step, out)
    if step == 1:
        _check_one_cta(message, out)
    else:
        _check_step2_cta(message, out)
    if limits.has_subject:
        _check_subject_unique(subject, batch_subjects, recent_subjects, out)
    _check_company_casual(company_casual, out)
    if step == 2:
        _check_step2(body, first_name, out)
    _check_urgency(subject or "", body, out)

    out.sort(key=lambda v: (int(v["rule"][1:]) if v["rule"][1:].isdigit() else 99))
    return LintReport.build(out)
