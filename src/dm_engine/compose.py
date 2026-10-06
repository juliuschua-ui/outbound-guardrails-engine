"""Compose-job emit and draft ingest — the boundary with the copywriting agent.

Contract: PLAN.md §5.1, §5.3, §12.4, §12.8. Two functions, both file-shaped:

* `emit_compose_job()` writes everything the `outreach-specialist` agent needs to
  personalize a batch — hook fact, casual company name, owner first name, the
  assigned variant's angle and spec, the channel's constraints, the rules it must
  satisfy — and *nothing it must not have*: no email addresses, no profile URLs,
  no tokens, no paths outside the repo. A prospect missing `company_casual` or
  `hook_fact` never reaches the agent; it is listed under `blocked` (PLAN.md §5.3).
* `ingest_drafts()` reads the agent's answer, lints every draft, and moves each
  message COMPOSED→READY (clean) or COMPOSED→LINT_FAILED (dirty).

The engine trusts nothing the agent returns (PLAN.md §5.1: "the agent pipeline is
quality; the lint is law"). Ingest is therefore written to survive a copywriting
agent returning an unknown prospect_key, the same key twice, a draft with no body,
extra keys, or half a batch — **every input row lands in exactly one of the
IngestResult buckets, and none is silently dropped** (PLAN.md §10 row (i)).

Storage note: bodies and subjects are stored NORMALIZED (`lint.normalize`), so the
SQL belt (trigger G6, which matches ASCII literals) sees the same text the lint did.

Owner-D note: this module writes `state_log` rows directly rather than calling
`state.transition()` — `state.py` is being built in parallel and must not be
imported from here. The rows use the same columns and `actor='engine'`; wiring
ingest through `state.transition()` later is a one-line change.

Owner-B note: `hook_fact` is read from `prospects.icp_fit_note` — either the raw
note text, or, if the note parses as a JSON object, its `hook_fact` key. There is
no `hook_fact` column in the schema (PLAN.md §3.2) and §5.3 designates
`icp_fit_note` as its home.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from . import lint as lint_mod
from .lint import Constraints, DEMO_LINK, MessageDraft, normalize, resolve_constraints

#: Schema tag written into every compose job, so a future reader can version-check.
COMPOSE_JOB_SCHEMA = "dm-engine/compose-job/v1"

#: Lookback for the L9 subject-uniqueness check (PLAN.md §5.4 L9).
RECENT_SUBJECT_DAYS = 30

_VALID_CHANNELS = ("email", "linkedin", "whatsapp", "messenger")
_OVERWRITABLE = ("COMPOSED", "LINT_FAILED")
_ABORT_RULE = re.compile(r"^(G\d+)\b")


class ComposeError(RuntimeError):
    """The job or draft file cannot be processed at all (bad batch, bad JSON shape)."""


@dataclass
class IngestResult:
    """PLAN.md §12.4. `ready + failed + len(unknown) == number of draft rows in the
    file`; `missing` lists batch prospects the file did not answer for. `details`
    is an additive, machine-readable per-row log for `runs.detail`."""

    ready: int = 0
    failed: int = 0
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    @property
    def handled(self) -> int:
        return self.ready + self.failed + len(self.unknown)

    def to_json(self) -> str:
        return json.dumps({"ready": self.ready, "failed": self.failed,
                           "missing": self.missing, "unknown": self.unknown,
                           "details": self.details}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def market_casual(market: str | None) -> str:
    """'Boise ID' -> 'Boise' (PLAN.md §5.3: city only, casualized)."""
    if not market:
        return ""
    return re.sub(r"\s+[A-Z]{2}$", "", market.strip())


def hook_fact(icp_fit_note: str | None) -> str | None:
    """The one concrete, verifiable fact the opener hangs on (PLAN.md §5.3)."""
    if not icp_fit_note or not icp_fit_note.strip():
        return None
    text = icp_fit_note.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(data, dict):
            value = data.get("hook_fact")
            return value.strip() if isinstance(value, str) and value.strip() else None
        return text
    return text


def _load_spec(spec: str | None) -> dict:
    if not spec:
        return {}
    try:
        data = json.loads(spec)
    except (ValueError, TypeError):
        return {"raw": spec}
    return data if isinstance(data, dict) else {"raw": spec}


def _constraints_dict(limits) -> dict:
    return {
        "max_words": limits.max_words,
        "max_sentences": limits.max_sentences,
        "max_chars": limits.max_chars,
        "has_subject": limits.has_subject,
        "max_subject_chars": limits.max_subject_chars,
        "requires_signature": limits.requires_signature,
    }


def recent_subjects(conn: sqlite3.Connection, days: int = RECENT_SUBJECT_DAYS) -> list[str]:
    """Subjects staged inside the lookback window (L9's history half)."""
    rows = conn.execute(
        "SELECT DISTINCT subject FROM messages "
        "WHERE subject IS NOT NULL AND trim(subject) != '' "
        "  AND staged_at IS NOT NULL AND staged_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchall()
    return [row[0] for row in rows]


def _batch_subjects(conn: sqlite3.Connection, batch_id: str) -> dict[int, str]:
    """Live subjects already claimed inside this batch, keyed by prospect id."""
    rows = conn.execute(
        "SELECT prospect_id, subject FROM messages "
        "WHERE batch_id = ? AND subject IS NOT NULL AND trim(subject) != '' "
        "  AND state NOT IN ('LINT_FAILED','REJECTED','CANCELLED')",
        (batch_id,),
    ).fetchall()
    return {row["prospect_id"]: row["subject"] for row in rows}


def _batch_row(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ComposeError(f"unknown batch {batch_id!r} — create it before composing")
    return row


def _assigned(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id AS prospect_id, p.prospect_key, p.company_name, p.company_casual, "
        "       p.owner_first_name, p.icp_fit_note, p.market, p.state AS prospect_state, "
        "       a.id AS assignment_id, v.id AS variant_id, v.family, v.name AS variant_name, "
        "       v.angle, v.spec "
        "FROM assignments a "
        "JOIN prospects p ON p.id = a.prospect_id "
        "JOIN variants  v ON v.id = a.variant_id "
        "WHERE a.batch_id = ? ORDER BY p.prospect_key",
        (batch_id,),
    ).fetchall()


def _contact_for(conn: sqlite3.Connection, prospect_id: int, channel: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, address, verify_verdict, address_source FROM contacts "
        "WHERE prospect_id = ? AND channel = ? "
        "ORDER BY (verify_verdict = 'PASS') DESC, id LIMIT 1",
        (prospect_id, channel),
    ).fetchone()


def _log(conn: sqlite3.Connection, message_id: int, from_state: str | None,
         to_state: str, reason: str, evidence: str | None = None) -> None:
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason, evidence) "
        "VALUES ('message', ?, ?, ?, 'engine', ?, ?)",
        (str(message_id), from_state, to_state, reason, evidence),
    )


# --------------------------------------------------------------------------- #
# emit_compose_job
# --------------------------------------------------------------------------- #


def emit_compose_job(conn: sqlite3.Connection, cfg, batch_id: str, *,
                     constraints=None, sequence_step: int = 1) -> Path:
    """Write `<review_dir>/<batch>-composejob.json` for the copywriting agent.

    Carries hook fact, casual company name, owner first name, variant angle and
    spec, and the channel constraints. Carries NO addresses, NO credentials, NO
    absolute paths — the agent needs the identity key (`prospect_key`) to round-trip
    its answer and nothing more (PLAN.md §12.8).
    """
    batch = _batch_row(conn, batch_id)
    channel = batch["channel"]
    limits = resolve_constraints(channel, constraints)

    prospects: list[dict] = []
    blocked: list[dict] = []
    for row in _assigned(conn, batch_id):
        missing: list[str] = []
        if not (row["company_casual"] or "").strip():
            missing.append("company_casual")
        fact = hook_fact(row["icp_fit_note"])
        if not fact:
            missing.append("hook_fact")
        if row["prospect_state"] != "QUALIFIED":
            missing.append(f"prospect_state={row['prospect_state']}")
        if _contact_for(conn, row["prospect_id"], channel) is None:
            missing.append(f"{channel}_contact")
        if missing:
            blocked.append({"prospect_key": row["prospect_key"],
                            "company_name": row["company_name"],
                            "missing": missing})
            continue
        spec = _load_spec(row["spec"])
        prospects.append({
            "prospect_key": row["prospect_key"],
            "company_casual": (row["company_casual"] or "").strip(),
            "first_name": (row["owner_first_name"] or "").strip() or None,
            "hook_fact": fact,
            "market_casual": market_casual(row["market"]),
            "variant": {
                "id": row["variant_id"],
                "family": row["family"],
                "name": row["variant_name"],
                "angle": row["angle"],
                "spec": spec,
            },
        })

    job = {
        "schema": COMPOSE_JOB_SCHEMA,
        "generated_at": _now(),
        "batch": {
            "id": batch["id"],
            "experiment_id": batch["experiment_id"],
            "market": batch["market"],
            "market_casual": market_casual(batch["market"]),
            "channel": channel,
            "personalization": batch["personalization"],
            "sequence_step": sequence_step,
        },
        "channel_constraints": _constraints_dict(limits),
        "rules": _rules_block(sequence_step),
        "agent_sequence": [
            "draft one message per prospect inside its assigned variant's angle",
            "/voice-extractor — check it sounds like the operator",
            "/de-ai-ify — strip AI tells",
            "return JSON matching output_contract; the engine lints every draft",
        ],
        "output_contract": {
            "batch_id": batch["id"],
            "drafts": [{"prospect_key": "<from prospects[].prospect_key>",
                        "channel": channel,
                        "sequence_step": sequence_step,
                        "subject": "<lowercase, unique per prospect>"
                                   if limits.has_subject else None,
                        "body": "<the message>"}],
        },
        "avoid_subjects": sorted(set(recent_subjects(conn))),
        "prospects": prospects,
        "blocked": blocked,
    }

    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    path = Path(cfg.review_dir) / f"{batch_id}-composejob.json"
    path.write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _rules_block(sequence_step: int) -> dict:
    """The lint contract, restated for the writer in plain language."""
    banned = [label for label, _pattern, scope in lint_mod._BANNED
              if scope != "step1" or sequence_step == 1]
    block = {
        "lint_rules": dict(lint_mod.RULES),
        "banned_phrases": banned,
        "fake_urgency": [label for label, _ in lint_mod._URGENCY],
        "no_emoji": True,
        "one_question_mark_in_the_closing_sentence": sequence_step == 1,
        "signature": lint_mod.SIGNATURE,
        "min_hook_chars": lint_mod.MIN_HOOK_CHARS,
        "forbidden_placeholders": "no {token}, [Token], <token>, %TOKEN%, $token — "
                                  "write the real words; a literal token is worse "
                                  "than no message",
    }
    if sequence_step == 1:
        block["no_links"] = ("Message 1 carries no URL in any form — not bare domains, "
                             "not 'their site x.com', not markdown links. Refer to "
                             "'your website'.")
        block["no_product_name"] = ("Message 1 names no product: no BDAI, no Bright Desk AI, "
                                    "no BrightFlow Cure, no brightdesk.")
    else:
        block["demo_link"] = f"Message 2 contains {DEMO_LINK} exactly once."
    return block


# --------------------------------------------------------------------------- #
# ingest_drafts
# --------------------------------------------------------------------------- #


def ingest_drafts(conn: sqlite3.Connection, cfg, path: Path) -> IngestResult:
    """Lint the agent's drafts and move each message COMPOSED→READY|LINT_FAILED.

    Every row of the file is accounted for. A row that cannot become a message at
    all (unknown key, duplicate key, no body, wrong channel, no contact, already
    past COMPOSED, suppressed contact) lands in `unknown` with the reason attached.
    """
    result = IngestResult()
    payload = _read_payload(path)
    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ComposeError(f"{path}: 'batch_id' is required (PLAN.md §12.8)")
    batch = _batch_row(conn, batch_id)
    drafts = payload.get("drafts")
    if not isinstance(drafts, list):
        raise ComposeError(f"{path}: 'drafts' must be a list (PLAN.md §12.8)")

    assigned = {row["prospect_key"]: row for row in _assigned(conn, batch_id)}
    history = recent_subjects(conn)
    claimed = _batch_subjects(conn, batch_id)
    seen: set[str] = set()

    for index, row in enumerate(drafts):
        label, problem = _row_identity(row, index)
        if problem:
            _reject(result, label, problem)
            continue
        key = row["prospect_key"].strip()
        if key in seen:
            _reject(result, key, "duplicate draft for this prospect_key in the file")
            continue
        seen.add(key)
        assignment = assigned.get(key)
        if assignment is None:
            _reject(result, key, f"prospect is not assigned to batch {batch_id}")
            continue

        problem = _validate_row(row, batch)
        if problem:
            _reject(result, key, problem)
            continue

        contact = _contact_for(conn, assignment["prospect_id"], row["channel"])
        if contact is None:
            _reject(result, key, f"no {row['channel']} contact on this prospect")
            continue

        existing = conn.execute(
            "SELECT id, state FROM messages WHERE batch_id = ? AND prospect_id = ? "
            "AND sequence_step = ?",
            (batch_id, assignment["prospect_id"], row["sequence_step"]),
        ).fetchone()
        if existing is not None and existing["state"] not in _OVERWRITABLE:
            _reject(result, key,
                    f"message {existing['id']} is already {existing['state']} — "
                    "refusing to overwrite reviewed content")
            continue

        _ingest_one(conn, result, batch, assignment, contact, existing, row,
                    claimed, history, key)

    # `missing` is prospect-level, not row-level: any batch prospect that still has
    # no live (READY-or-better) message needs another pass from the writer. A draft
    # that arrived and failed lint counts in `failed` AND lands here, because the
    # batch is not complete until it is re-composed (PLAN.md §9 edge 14).
    for key, assignment in assigned.items():
        live = conn.execute(
            "SELECT 1 FROM messages WHERE batch_id = ? AND prospect_id = ? "
            "AND state NOT IN ('LINT_FAILED','REJECTED','CANCELLED')",
            (batch_id, assignment["prospect_id"]),
        ).fetchone()
        if live is None:
            result.missing.append(key)
    result.missing.sort()
    return result


def dry_run_lint(compose_job_path: Path, drafts_path: Path) -> IngestResult:
    """Lint a candidate drafts file against its own compose job. Writes nothing.

    Same rules, same two file shapes `ingest_drafts()` consumes (PLAN.md §12.4
    compose job, §12.8 drafts) — but no database, no batch row, no state
    transition. For a copywriting agent (or a human) to self-check a batch before
    `ingest-drafts` actually consumes it, closing the loop where an agent was
    hand-deriving the same L1-L12 checks from the compose job's `rules` block
    instead of running them.

    The compose job already carries everything the rules need: each `prospects[]`
    entry has `company_casual`/`first_name`, `channel_constraints` maps onto
    `Constraints` field-for-field, and `avoid_subjects` is the 30-day history half
    of L9 (PLAN.md §5.3). The batch-local half of L9 is computed here, from the
    OTHER rows in the same candidate file, since none of it has been staged yet.

    Reuses `IngestResult`: `ready`/`failed` are lint outcomes against the compose
    job's own `prospects[]`, `missing` lists prospects the file never answered
    for, `unknown` covers rows that don't match a `prospects[]` entry or are
    malformed the same way a live ingest would reject them.
    """
    job = _read_payload(compose_job_path)
    prospects = job.get("prospects")
    if not isinstance(prospects, list):
        raise ComposeError(f"{compose_job_path}: 'prospects' must be a list")
    by_key = {}
    for prospect in prospects:
        key = prospect.get("prospect_key") if isinstance(prospect, dict) else None
        if isinstance(key, str) and key.strip():
            by_key[key.strip()] = prospect

    known_fields = {f.name for f in fields(Constraints)}
    raw_constraints = job.get("channel_constraints")
    constraints = Constraints(**{k: v for k, v in (raw_constraints or {}).items()
                                 if k in known_fields})
    history = [s for s in (job.get("avoid_subjects") or []) if isinstance(s, str)]
    batch_stub = {"channel": (job.get("batch") or {}).get("channel")}

    payload = _read_payload(drafts_path)
    drafts = payload.get("drafts")
    if not isinstance(drafts, list):
        raise ComposeError(f"{drafts_path}: 'drafts' must be a list (PLAN.md §12.8)")

    result = IngestResult()
    seen: set[str] = set()
    claimed: dict[str, str] = {}  # prospect_key -> subject, this file's live rows only

    for index, row in enumerate(drafts):
        label, problem = _row_identity(row, index)
        if problem:
            _reject(result, label, problem)
            continue
        key = row["prospect_key"].strip()
        if key in seen:
            _reject(result, key, "duplicate draft for this prospect_key in the file")
            continue
        seen.add(key)
        prospect = by_key.get(key)
        if prospect is None:
            _reject(result, key, "prospect_key is not in this compose job's prospects[]")
            continue

        problem = _validate_row(row, batch_stub)
        if problem:
            _reject(result, key, problem)
            continue

        draft = MessageDraft(
            prospect_key=key,
            channel=row["channel"],
            sequence_step=row["sequence_step"],
            subject=row.get("subject"),
            body=row["body"],
        )
        draft = lint_mod.normalize_draft(draft)
        peers = [subject for pkey, subject in claimed.items() if pkey != key]
        report = lint_mod.lint_message(
            draft, peers, history, prospect.get("company_casual"),
            constraints=constraints, first_name=prospect.get("first_name"),
        )

        if report.ok:
            result.ready += 1
            if draft.subject:
                claimed[key] = draft.subject
        else:
            result.failed += 1
        result.details.append({
            "prospect_key": key,
            "outcome": "ready" if report.ok else "lint_failed",
            "rules": report.rules(),
            "reason": report.summary()[:500],
        })

    for key in by_key:
        if key not in seen:
            result.missing.append(key)
    result.missing.sort()
    return result


def _read_payload(path: Path) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ComposeError(f"cannot read draft file {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ComposeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ComposeError(f"{path}: top level must be a JSON object (PLAN.md §12.8)")
    return payload


def _row_identity(row, index: int) -> tuple[str, str | None]:
    if not isinstance(row, dict):
        return f"<row {index}>", "draft is not a JSON object"
    key = row.get("prospect_key")
    if not isinstance(key, str) or not key.strip():
        return f"<row {index}>", "draft has no prospect_key"
    return key.strip(), None


def _validate_row(row: dict, batch: sqlite3.Row) -> str | None:
    """Normalize the row in place; return a rejection reason or None."""
    channel = row.get("channel") or batch["channel"]
    if channel not in _VALID_CHANNELS:
        return f"unsupported channel {channel!r}"
    if channel != batch["channel"]:
        return (f"channel {channel!r} does not match batch channel "
                f"{batch['channel']!r} — cross-batch contamination")
    row["channel"] = channel

    step = row.get("sequence_step", 1)
    if isinstance(step, bool) or not isinstance(step, int) or step not in (1, 2):
        return f"sequence_step must be 1 or 2, got {step!r}"
    row["sequence_step"] = step

    body = row.get("body")
    if not isinstance(body, str) or not body.strip():
        return "draft has no body"

    subject = row.get("subject")
    if subject is not None and not isinstance(subject, str):
        return f"subject must be a string or null, got {type(subject).__name__}"
    return None


def _reject(result: IngestResult, label: str, reason: str) -> None:
    result.unknown.append(f"{label}: {reason}")
    result.details.append({"prospect_key": label, "outcome": "unknown", "reason": reason})


def _ingest_one(conn, result: IngestResult, batch, assignment, contact, existing,
                row: dict, claimed: dict[int, str], history: list[str], key: str) -> None:
    prospect_id = assignment["prospect_id"]
    draft = MessageDraft(
        prospect_key=key,
        channel=row["channel"],
        sequence_step=row["sequence_step"],
        subject=row.get("subject"),
        body=row["body"],
    )
    draft = lint_mod.normalize_draft(draft)
    peers = [subject for pid, subject in claimed.items() if pid != prospect_id]
    report = lint_mod.lint_message(
        draft,
        peers,
        history,
        assignment["company_casual"],
        first_name=assignment["owner_first_name"],
    )

    try:
        with conn:
            message_id = _upsert_composed(conn, batch, assignment, contact, draft, existing)
    except sqlite3.Error as exc:
        _reject(result, key, _abort_reason(exc))
        return

    target = "READY" if report.ok else "LINT_FAILED"
    try:
        with conn:
            conn.execute(
                "UPDATE messages SET state = ?, lint_report = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (target, report.to_json(), message_id),
            )
            _log(conn, message_id, "COMPOSED", target, "ingest-drafts",
                 report.summary()[:500])
    except sqlite3.Error as exc:
        # The SQL belt (G6) refused content the lint let through. Never lose the
        # message: park it at LINT_FAILED with the database's own reason attached.
        reason = _abort_reason(exc)
        rule = _ABORT_RULE.match(reason)
        report = lint_mod.LintReport.build(
            report.violations + [{"rule": rule.group(1) if rule else "DB",
                                  "where": "body", "detail": reason}])
        target = "LINT_FAILED"
        with conn:
            conn.execute(
                "UPDATE messages SET state = 'LINT_FAILED', lint_report = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (report.to_json(), message_id),
            )
            _log(conn, message_id, "COMPOSED", "LINT_FAILED", "ingest-drafts", reason[:500])

    if target == "READY":
        result.ready += 1
        if draft.subject:
            claimed[prospect_id] = draft.subject
    else:
        result.failed += 1
    result.details.append({
        "prospect_key": key,
        "message_id": message_id,
        "outcome": "ready" if target == "READY" else "lint_failed",
        "rules": report.rules(),
        "reason": report.summary()[:500],
    })


def _upsert_composed(conn, batch, assignment, contact, draft: MessageDraft,
                     existing) -> int:
    """Ensure a COMPOSED message row holds the normalized content. Returns its id."""
    if existing is not None:
        conn.execute(
            "UPDATE messages SET contact_id = ?, channel = ?, subject = ?, body = ?, "
            "state = 'COMPOSED', lint_report = NULL, updated_at = datetime('now') "
            "WHERE id = ?",
            (contact["id"], draft.channel, draft.subject, draft.body, existing["id"]),
        )
        if existing["state"] != "COMPOSED":
            _log(conn, existing["id"], existing["state"], "COMPOSED", "re-ingest")
        return int(existing["id"])
    cursor = conn.execute(
        "INSERT INTO messages (batch_id, prospect_id, contact_id, assignment_id, variant_id, "
        "sequence_step, channel, subject, body, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPOSED')",
        (batch["id"], assignment["prospect_id"], contact["id"], assignment["assignment_id"],
         assignment["variant_id"], draft.sequence_step, draft.channel, draft.subject,
         draft.body),
    )
    message_id = int(cursor.lastrowid)
    _log(conn, message_id, None, "COMPOSED", "ingest-drafts")
    return message_id


def _abort_reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


__all__ = [
    "COMPOSE_JOB_SCHEMA", "ComposeError", "IngestResult", "emit_compose_job",
    "hook_fact", "ingest_drafts", "market_casual", "recent_subjects",
    "MessageDraft", "normalize",
]
