"""The approval gate: review file out, human marks, approval in.

Contract: PLAN.md §12.5. Behaviour: §7.

This is the gate that decides what may leave the database. Nothing reaches a
Gmail draft or a LinkedIn outbox card without a human passing through here, and
there is deliberately no way around it:

  - no `--yes`, no `--force`, no env override on approval;
  - BOTH approval paths require a TTY. `--from-review` used to be the exception,
    and a review file is a file an agent can write: `dmctl review` -> flip the
    boxes -> `dmctl approve --from-review` was a complete, human-free path from
    draft to Gmail. A terminal is the one thing a headless agent does not have;
  - `approve_from_review()` only ever moves READY -> APPROVED / REJECTED, and
    trigger G2 aborts the write anyway if this code is ever wrong;
  - the file is bound to the CONTENT it approves, not to its own bytes. Every
    block carries `content: sha256:<hex>` over the message's subject, body,
    recipient and verification verdict; a block without one, or with one that no
    longer matches the database, refuses the whole file. A hand-written review
    file approves nothing, and a body swapped after the human read it cannot be
    approved by touching the file's mtime;
  - that same content digest — not just the file digest — lands in
    `state_log.evidence`, so the audit trail says what was approved rather than
    which bytes happened to be on disk;
  - the contact is re-checked at approval time against the suppression list, so
    an address that became suppressed between review and approval is refused.

Approval is gate ONE. Gate TWO is outside the engine entirely — the operator pressing
send in Gmail, or APPROVE + ARM in the DASHBOARD EXEC send-queue. The engine cannot
send, so approval only unlocks *staging*.

DASHBOARD review lane (2026-08-09): `parse_review_blocks()` and `set_review_mark()`
below exist so a browser click can do what a hand-edit of the review file does —
flip one message's checkbox — without touching anything `approve_from_review()`
itself checks. `cli.py`'s `decide` command calls both, then calls
`approve_from_review()` unchanged: same content-digest binding, same staleness
check, same suppression re-check. Nothing about the GATE gets weaker. What's
different is the PROOF: `dmctl approve` proves a human by requiring a TTY;
`dmctl decide` proves nothing by itself and instead requires an honest
`--actor` (no default) — a DASHBOARD-lane approval is only as trustworthy as
whatever sits between the click and the bearer token, which is a DASHBOARD
architecture question, not a dm-engine one. See MANUAL.md "DASHBOARD review lane".
"""

import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .channels.base import send_window_line, stagger_plan, verify_refusal
from .state import IllegalTransition, transition

__all__ = [
    "ApproveResult",
    "NotATTY",
    "StaleReviewFile",
    "ReviewFileError",
    "UnboundReviewFile",
    "content_digest",
    "write_review_file",
    "approve_from_review",
    "approve_interactive",
    "parse_review_blocks",
    "set_review_mark",
]


class NotATTY(Exception):
    """Interactive approval was attempted without a terminal."""


class ReviewFileError(Exception):
    """The review file cannot be trusted (unparseable, or covers nothing)."""


class StaleReviewFile(ReviewFileError):
    """Messages changed after the review file was written."""


class UnboundReviewFile(ReviewFileError):
    """A review file that does not bind itself to the content it claims to approve.

    Raised for a file this engine did not generate: no `- batch:` header, or a
    `## message` block with no `content: sha256:` line. Such a file is a claim
    about what a human read, with nothing behind it.
    """


@dataclass
class ApproveResult:
    approved: int = 0
    rejected: int = 0
    skipped: int = 0
    notes: list[tuple[int, str]] = field(default_factory=list)


_HEADER = """# review — {batch_id}

- batch: {batch_id}
- generated: {generated}
- ready messages: {count}
- approve-all: no

Read every block, then mark ONE box per message:

    - [x] approve            stage this message
    - [x] reject: too salesy  kill it (recompose as a new message)

Leaving both boxes unmarked skips that message — decide it later. Setting
`approve-all: yes` in the header above takes every block below.

Then run, AT A TERMINAL:

    dmctl approve --from-review {path}

Do not edit the `content:` line under a message. It binds this file to the exact
subject, body, recipient and verification verdict in the database; change the
copy and approval refuses the file rather than approving text nobody read.

Approval lets the engine STAGE these (Gmail draft / LinkedIn card). It does not
send anything — you still release each one yourself, 3-5 minutes apart, 10 per
window.

"""

_BLOCK = """
---

## message {message_id} — {company_name} [{variant_id}]

- to: {address}
- verify: {verify}
- prospect: {prospect_key} · {market}
- company_casual: {company_casual}
- first_name: {first_name}
- send window: {send_window}
- planned release: {planned_release}
- channel: {channel} · step {sequence_step}
- content: sha256:{content}
- subject: {subject}

~~~
{body}
~~~

- [ ] approve
- [ ] reject:
"""

_MESSAGE_HEADING = re.compile(r"^##\s+message\s+(\d+)\b", re.MULTILINE)
_APPROVE_MARK = re.compile(r"^-\s*\[([ xX])\]\s*approve\s*$", re.MULTILINE)
_REJECT_MARK = re.compile(r"^-\s*\[([ xX])\]\s*reject\s*:?\s*(.*)$", re.MULTILINE)
_APPROVE_ALL = re.compile(r"^-?\s*approve-all\s*:\s*(\w+)\s*$", re.MULTILINE)
_CONTENT_MARK = re.compile(r"^-\s*content\s*:\s*sha256:([0-9a-f]{64})\s*$", re.MULTILINE)
_BATCH_MARK = re.compile(r"^-\s*batch\s*:\s*(\S+)\s*$", re.MULTILINE)

_READY_SELECT = """
SELECT m.id, m.state, m.channel, m.subject, m.body, m.sequence_step, m.variant_id,
       m.batch_id, m.updated_at,
       c.address, c.address_source, c.verify_verdict, c.verify_reason, c.verified_at,
       p.prospect_key, p.company_name, p.company_casual, p.market, p.timezone,
       p.owner_first_name
FROM messages m
JOIN contacts  c ON c.id = m.contact_id
JOIN prospects p ON p.id = m.prospect_id
WHERE m.batch_id = ?
"""


#: Everything a human is actually judging when they tick the box. Change any of
#: it after the file was written and the digest no longer matches, which is what
#: makes approval a statement about CONTENT rather than about a file's bytes.
_DIGEST_COLUMNS = ("id", "batch_id", "channel", "sequence_step", "variant_id",
                   "subject", "body", "address", "verify_verdict", "address_source")


def content_digest(row) -> str:
    """sha256 over the message content a reviewer sees. Order-fixed, NUL-separated."""
    parts = []
    for column in _DIGEST_COLUMNS:
        value = row[column] if column in row.keys() else None
        parts.append("" if value is None else str(value))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def write_review_file(conn, cfg, batch_id: str) -> Path:
    """Write `data/review/<batch>-review.md` — one block per READY message.

    Everything a human needs to judge the message is in the block: the exact
    recipient and its verification provenance, the casualized company name (the
    token most likely to read badly), the prospect-local send window, and the
    full body. Messages that are not READY are listed at the bottom with their
    state so nothing disappears silently.
    """
    rows = conn.execute(_READY_SELECT + " ORDER BY m.id", (batch_id,)).fetchall()
    ready = [row for row in rows if row["state"] == "READY"]
    other = [row for row in rows if row["state"] != "READY"]

    directory = Path(cfg.review_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{batch_id}-review.md"

    parts = [_HEADER.format(
        batch_id=batch_id,
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        count=len(ready),
        path=path,
    )]
    plan = stagger_plan(len(ready))
    for index, row in enumerate(ready):
        parts.append(_BLOCK.format(
            message_id=row["id"],
            company_name=row["company_name"],
            variant_id=row["variant_id"],
            address=row["address"],
            verify=_verify_line(row),
            prospect_key=row["prospect_key"],
            market=row["market"],
            company_casual=row["company_casual"] or "(missing)",
            first_name=row["owner_first_name"] or "(unknown — copy must be name-free)",
            send_window=send_window_line(row["timezone"]),
            planned_release=plan[index],
            channel=row["channel"],
            sequence_step=row["sequence_step"],
            content=content_digest(row),
            subject=row["subject"] or "(none — DM channel)",
            body=row["body"],
        ))
    if other:
        parts.append("\n---\n\n## not included in this review\n\n")
        for row in other:
            detail = f"state {row['state']}"
            refusal = verify_refusal(row) if row["channel"] == "email" else None
            if refusal:
                detail += f"; {refusal}"
            parts.append(f"- message {row['id']} — {row['company_name']} "
                         f"({row['address']}): {detail}\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def approve_from_review(conn, cfg, review_path: Path, actor: str = "operator") -> ApproveResult:
    """Apply the marks in a review file. READY -> APPROVED / REJECTED, nothing else.

    The file must bind itself to what it approves, or the whole thing is refused:

      * `- batch: <id>` in the header, and every message must belong to it. A
        review file generated for B-ONE cannot approve a message in B-TWO, even
        with a hand-appended block;
      * `- content: sha256:<hex>` in every block, matching the message's current
        subject, body, recipient and verification verdict. A block without one is
        a forgery (`UnboundReviewFile`); a block whose digest no longer matches is
        stale (`StaleReviewFile`) — including the case where the body was swapped
        without touching `updated_at`, which the old mtime check could not see and
        a single `os.utime` defeated.

    `approve-all: yes` is a shortcut for ticking every box, never a shortcut past
    those two checks: a block it would approve is bound exactly like a block a
    human ticked by hand.

    Per message it then re-checks the suppression list, because a suppression
    added between review and approval must win.
    """
    review_path = Path(review_path)
    if not review_path.exists():
        raise ReviewFileError(f"review file not found: {review_path}")
    raw = review_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()   # hash what is on disk
    text = raw.replace("\r\n", "\n")                            # parse line-ending-agnostic
    approve_all = _approve_all(text)

    marks = _parse_marks(text)
    if not marks:
        raise ReviewFileError(
            f"no `## message <id>` blocks found in {review_path} — "
            "is this a review file?"
        )
    batch_id = _declared_batch(text)
    if not batch_id:
        raise UnboundReviewFile(
            f"{review_path} has no `- batch: <id>` header line, so it is not a file "
            "`dmctl review` produced. Approval only ever applies a file this engine "
            "generated — regenerate it with `dmctl review --batch <B>`.")
    _assert_not_stale(conn, review_path, list(marks))
    rows = _bind(conn, review_path, batch_id, marks, approve_all)

    result = ApproveResult()
    for message_id, mark in marks.items():
        row = rows.get(message_id)
        if row is None:
            result.skipped += 1
            result.notes.append((message_id, "unknown message id — ignored"))
            continue
        decision = "approve" if (approve_all and mark["decision"] is None) else mark["decision"]
        if decision is None:
            result.skipped += 1
            result.notes.append((message_id, "no box marked — left for a later pass"))
            continue
        if decision == "conflict":
            result.skipped += 1
            result.notes.append(
                (message_id, "both approve and reject are marked — refusing to guess"))
            continue
        if row["state"] != "READY":
            result.skipped += 1
            result.notes.append(
                (message_id, f"not READY (state={row['state']}) — nothing to approve"))
            continue

        # Evidence names the CONTENT, not only the bytes on disk. The file digest
        # alone said nothing about which body was approved.
        evidence = (f"review_file:{review_path} sha256:{digest} "
                    f"content_sha256:{mark['content']}")

        if decision == "approve":
            blocked = _contact_refusal(conn, row)
            if blocked:
                result.skipped += 1
                result.notes.append((message_id, blocked))
                continue
            _apply(conn, result, message_id, "APPROVED", actor,
                   reason="approved in review file",
                   evidence=evidence, approved_via=f"review_file:{review_path}")
        else:
            _apply(conn, result, message_id, "REJECTED", actor,
                   reason=f"rejected in review file: {mark['reason'] or 'no reason given'}",
                   evidence=evidence)
    return result


def _declared_batch(text: str) -> str | None:
    first_block = _MESSAGE_HEADING.search(text)
    header = text[:first_block.start()] if first_block else text
    match = _BATCH_MARK.search(header)
    return match.group(1) if match else None


def _bind(conn, review_path: Path, batch_id: str, marks: dict,
          approve_all: bool = False) -> dict:
    """Check every block that can produce a decision against the database.

    Refuses the whole file. Returns {message_id: row} for the ids that exist. Ids
    that do not exist are left out and reported per message; everything else is
    fatal, because a file that is wrong about one message is not evidence about
    any of them.

    `approve_all` is the header's `approve-all: yes`, and it is a convenience for
    the per-item MARK only: an unmarked block that approve-all will approve is
    checked exactly like an explicitly ticked one. It used to skip this function
    entirely, which meant a hand-written file could approve copy nobody read.
    """
    rows: dict[int, sqlite3.Row] = {}
    for message_id, mark in marks.items():
        row = conn.execute(
            "SELECT m.id, m.state, m.batch_id, m.channel, m.sequence_step, "
            "       m.variant_id, m.subject, m.body, m.prospect_id, m.contact_id, "
            "       c.address, c.verify_verdict, c.address_source, c.person_key "
            "FROM messages m JOIN contacts c ON c.id = m.contact_id "
            "WHERE m.id = ?", (message_id,)).fetchone()
        if row is None:
            continue
        rows[message_id] = row
        # These two run for EVERY block, marked or not. A block with no content
        # line, or one naming another batch, is evidence the file is not the one
        # `dmctl review` generated — which is true whatever box is ticked. It is
        # also what guarantees `mark["content"]` is set for every row returned
        # here, so approval evidence can never read `content_sha256:None`.
        if not mark["content"]:
            raise UnboundReviewFile(
                f"{review_path}: the block for message {message_id} carries no "
                "`- content: sha256:` line. Only a file `dmctl review` generated can "
                "approve anything — a block appended by hand approves nothing, "
                "because nothing ties it to the copy that would actually ship.")
        if row["batch_id"] != batch_id:
            raise ReviewFileError(
                f"{review_path} was generated for batch {batch_id} but message "
                f"{message_id} belongs to {row['batch_id']}. A review file approves "
                "its own batch and nothing else — run `dmctl review --batch "
                f"{row['batch_id']}` and read that batch too.")
        if mark["decision"] is None and not approve_all:
            continue                     # an unmarked block decides nothing
        actual = content_digest(row)
        if actual != mark["content"]:
            raise StaleReviewFile(
                f"{review_path}: message {message_id} no longer matches the file. "
                f"The block was written for content sha256:{mark['content'][:16]}… "
                f"and the database now holds sha256:{actual[:16]}… — the subject, "
                "body, recipient or verification verdict changed after the file was "
                "read. Regenerate it with `dmctl review` and read it again.")
    return rows


def _contact_refusal(conn, row) -> str | None:
    """Re-check the recipient at approval time (the file may be minutes old)."""
    from . import suppression

    reason = suppression.is_suppressed(conn, row["channel"], row["address"],
                                       row["person_key"])
    if reason:
        return (f"refused at approval: {row['address']} is suppressed ({reason}). "
                "The suppression list is re-read on every approve.")
    state = conn.execute("SELECT state FROM prospects WHERE id = ?",
                         (row["prospect_id"],)).fetchone()
    if state is not None and state["state"] == "SUPPRESSED":
        return ("refused at approval: the prospect is SUPPRESSED — this company "
                "asked to stop.")
    return None


def approve_interactive(conn, cfg, batch_id: str, actor: str = "operator") -> ApproveResult:
    """TTY-only approval. Raises NotATTY when there is no terminal.

    Deliberately has no non-interactive path: an agent cannot approve outreach.
    """
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        raise NotATTY(
            "interactive approval needs a terminal. A headless caller must use "
            "`dmctl approve --from-review <file>` and mark the file by hand — "
            "there is no --yes and no env override on this command."
        )
    rows = conn.execute(_READY_SELECT + " AND m.state = 'READY' ORDER BY m.id",
                        (batch_id,)).fetchall()
    result = ApproveResult()
    for row in rows:
        print(f"\n--- message {row['id']} — {row['company_name']} [{row['variant_id']}]")
        print(f"to      : {row['address']}  ({_verify_line(row)})")
        print(f"window  : {send_window_line(row['timezone'])}")
        print(f"subject : {row['subject']}")
        print(f"\n{row['body']}\n")
        answer = input("type APPROVE to approve, REJECT <reason> to reject, "
                       "anything else to skip: ").strip()
        if answer == "APPROVE":
            _apply(conn, result, row["id"], "APPROVED", actor,
                   reason="approved at interactive TTY",
                   evidence="interactive_tty", approved_via="interactive_tty")
        elif answer.startswith("REJECT"):
            _apply(conn, result, row["id"], "REJECTED", actor,
                   reason=f"rejected at TTY: {answer[len('REJECT'):].strip() or 'no reason'}",
                   evidence="interactive_tty")
        else:
            result.skipped += 1
            result.notes.append((row["id"], "skipped at TTY"))
    return result


_FIELD_LINE = re.compile(
    r"^- (to|verify|prospect|company_casual|first_name|send window|"
    r"planned release|channel|subject): (.*)$", re.MULTILINE)
_HEADING_DETAIL = re.compile(
    r"^##\s+message\s+\d+\s+—\s+(.*?)\s+\[([^\]]+)\]\s*$", re.MULTILINE)
_BODY_FENCE = re.compile(r"~~~\n(.*?)\n~~~", re.DOTALL)
#: Stricter than _APPROVE_MARK/_REJECT_MARK on purpose. Those exist to PARSE
#: whatever a human typed, so their `\s*` may cross a line on purpose. This
#: pair REWRITES a line via `.sub()`, where a `\s*` that can absorb `\n`
#: silently eats the blank line + `---` separator after an EMPTY `reject:`
#: line (found by a round-trip test, not by inspection) — horizontal
#: whitespace only, never crosses into the next line.
_APPROVE_LINE = re.compile(r"^-[ \t]*\[[ xX]\][ \t]*approve[ \t]*$", re.MULTILINE)
_REJECT_LINE = re.compile(r"^-[ \t]*\[[ xX]\][ \t]*reject[ \t]*:?[ \t]*[^\n]*$", re.MULTILINE)


def parse_review_blocks(review_path: Path) -> list[dict]:
    """The same blocks `approve_from_review` would act on, structured for a UI.

    Read-only, no database access — parses exactly the file `dmctl review`
    wrote. Each dict carries what a reviewer needs to render a card (company,
    variant, recipient, verify verdict, casual name, send window, subject,
    body) plus the block's CURRENT mark: `mark` is 'approve' / 'reject' /
    'conflict' / None, mirroring `_parse_marks()`'s own decision values.
    """
    review_path = Path(review_path)
    text = review_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    batch_id = _declared_batch(text)
    headings = list(_MESSAGE_HEADING.finditer(text))
    details = list(_HEADING_DETAIL.finditer(text))
    blocks = []
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[start:end]
        approved = any(m.group(1) in "xX" for m in _APPROVE_MARK.finditer(block))
        reject_hits = [m for m in _REJECT_MARK.finditer(block) if m.group(1) in "xX"]
        if approved and reject_hits:
            mark = "conflict"
        elif approved:
            mark = "approve"
        elif reject_hits:
            mark = "reject"
        else:
            mark = None
        fields = dict(_FIELD_LINE.findall(block))
        body_match = _BODY_FENCE.search(block)
        detail = details[index] if index < len(details) else None
        # the `- channel:` line is "{channel} · step {sequence_step}" (_BLOCK
        # template) — split it rather than hand the raw line to a UI as-is.
        channel_raw = fields.get("channel") or ""
        channel_name, _, step_part = channel_raw.partition("·")
        step_match = re.search(r"\d+", step_part)
        blocks.append({
            "message_id": int(heading.group(1)),
            "batch_id": batch_id,
            "company_name": detail.group(1) if detail else None,
            "variant_id": detail.group(2) if detail else None,
            "to": fields.get("to"),
            "verify": fields.get("verify"),
            "prospect": fields.get("prospect"),
            "company_casual": fields.get("company_casual"),
            "first_name": fields.get("first_name"),
            "send_window": fields.get("send window"),
            "planned_release": fields.get("planned release"),
            "channel": channel_name.strip() or None,
            "sequence_step": int(step_match.group()) if step_match else None,
            "subject": fields.get("subject"),
            "body": body_match.group(1) if body_match else "",
            "mark": mark,
            "reject_reason": reject_hits[0].group(2).strip() if reject_hits else "",
        })
    return blocks


def set_review_mark(review_path: Path, message_id: int, decision: str,
                    reason: str = "") -> None:
    """Flip ONE message's checkbox in-place. Nothing else in the file moves.

    `decision` is 'approve' or 'reject'. The `- content: sha256:` line is never
    touched, so this can never invalidate what a reviewer already read — it can
    only ever change which box is ticked. `approve_from_review()` re-derives
    that digest from the live database on every call regardless of what this
    function did, so a rewritten checkbox cannot smuggle rewritten copy.
    """
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    review_path = Path(review_path)
    text = review_path.read_text(encoding="utf-8")
    headings = list(_MESSAGE_HEADING.finditer(text))
    target = next((h for h in headings if int(h.group(1)) == message_id), None)
    if target is None:
        raise ReviewFileError(f"{review_path}: no message {message_id} block found")
    idx = headings.index(target)
    start = target.end()
    end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
    block = text[start:end]

    if decision == "approve":
        new_block = _APPROVE_LINE.sub("- [x] approve", block, count=1)
        new_block = _REJECT_LINE.sub("- [ ] reject:", new_block, count=1)
    else:
        clean_reason = " ".join(reason.split())
        new_block = _APPROVE_LINE.sub("- [ ] approve", block, count=1)
        mark_line = f"- [x] reject: {clean_reason}" if clean_reason else "- [x] reject:"
        new_block = _REJECT_LINE.sub(mark_line, new_block, count=1)

    review_path.write_text(text[:start] + new_block + text[end:], encoding="utf-8")


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _apply(conn, result, message_id, to_state, actor, reason, evidence,
           approved_via=None) -> None:
    """One message, one transaction. A DB gate becomes a readable skip, not a crash."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        if approved_via:
            conn.execute("UPDATE messages SET approved_via = ? WHERE id = ?",
                         (approved_via, message_id))
        transition(conn, "message", message_id, to_state, actor,
                   reason=reason, evidence=evidence)
        conn.execute("COMMIT")
    except IllegalTransition as exc:
        conn.rollback()
        result.skipped += 1
        result.notes.append((message_id, str(exc)))
        return
    except sqlite3.Error as exc:
        # A G-trigger fired (G6 link in Message 1, G7 unearned Message 2, G9
        # verification). Surface it as a refusal for this message; the rest of
        # the batch is unaffected.
        conn.rollback()
        result.skipped += 1
        result.notes.append((message_id, f"database gate refused it: {exc}"))
        return
    if to_state == "APPROVED":
        result.approved += 1
    else:
        result.rejected += 1


def _parse_marks(text: str) -> dict[int, dict]:
    """message id -> {'decision', 'reason', 'content'} for each `## message` block."""
    headings = list(_MESSAGE_HEADING.finditer(text))
    marks: dict[int, dict] = {}
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[start:end]
        approved = any(m.group(1) in "xX" for m in _APPROVE_MARK.finditer(block))
        reject_hits = [m for m in _REJECT_MARK.finditer(block) if m.group(1) in "xX"]
        if approved and reject_hits:
            decision, reason = "conflict", ""
        elif approved:
            decision, reason = "approve", ""
        elif reject_hits:
            decision, reason = "reject", reject_hits[0].group(2).strip()
        else:
            decision, reason = None, ""
        content = _CONTENT_MARK.search(block)
        marks[int(heading.group(1))] = {
            "decision": decision,
            "reason": reason,
            "content": content.group(1) if content else None,
        }
    return marks


def _approve_all(text: str) -> bool:
    """Only the header (everything before the first message block) may set it."""
    first_block = _MESSAGE_HEADING.search(text)
    header = text[:first_block.start()] if first_block else text
    match = _APPROVE_ALL.search(header)
    return bool(match and match.group(1).lower() in {"yes", "true", "y"})


def _assert_not_stale(conn, review_path: Path, message_ids: list[int]) -> None:
    """The file must postdate every message it covers (PLAN.md §7.3)."""
    if not message_ids:
        return
    placeholders = ",".join("?" * len(message_ids))
    row = conn.execute(
        f"SELECT max(updated_at) AS newest FROM messages WHERE id IN ({placeholders})",
        message_ids,
    ).fetchone()
    newest = row["newest"] if row else None
    if not newest:
        return
    try:
        changed = datetime.strptime(newest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return
    written = datetime.fromtimestamp(review_path.stat().st_mtime, tz=timezone.utc)
    if written < changed:
        raise StaleReviewFile(
            f"{review_path} was written {written.isoformat(timespec='seconds')} but a "
            f"message it covers changed at {newest} UTC. The file no longer matches the "
            "database — regenerate it with `dmctl review` and read it again."
        )


def _verify_line(row) -> str:
    return (f"{row['verify_verdict'] or 'UNVERIFIED'} via {row['address_source']}"
            f"{', checked ' + row['verified_at'] if row['verified_at'] else ''}"
            f"{' — ' + row['verify_reason'] if row['verify_reason'] else ''}")
