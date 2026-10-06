"""Email channel: Gmail DRAFT staging + Sent/bounce reconciliation.

Contract: PLAN.md §12.5. Behaviour: §6.1, §9 (Approval/staging, Caps/clock/host).

    ═══ THIS ADAPTER CREATES DRAFTS AND STOPS. ═══

There is no send call in this file, behind a flag or otherwise, and there never
may be (PLAN.md §14 non-goal 1). A staged draft is released by a SECOND,
independent human gate outside the engine: the operator pressing send in Gmail, or the
DASHBOARD EXEC send-queue (per-draft APPROVE + explicit ARM, VPS host guard,
10/window + 20/day + stagger enforced server-side).

Host sensitivity of the operations here:
  stage --dry-run   any host; writes JSON to data/outbox/dry-run/, zero API calls
  stage             any host; drafting is exactly what WSL is allowed to do
  reconcile         any host; read-only Gmail queries
  (sending)         not here — VPS `send-host` only, via EXEC / send_*.py, which
                    carry `_sender_host_guard.require_sender_host()`

Gmail cannot schedule for us either: `scheduledSendTime` does not exist in the
Gmail API — it is silently ignored and the message sends immediately. Nothing in
this design depends on it. What staging DOES record is the prospect-local send
window (the ICP is national across 4+ zones while the operator runs on a Phoenix
clock), so whoever arms the real send does not fire a cold email at 5am local.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import gmail_compat, locks
from ..db import phx_date
from ..state import note, transition
from .base import (
    STAGE_LOCK,
    ChannelConstraints,
    StageResult,
    cap_refusal,
    dry_run_path,
    effective_personalization,
    fetch_message,
    prospect_suppressed,
    recent_person_touch,
    record_stage_event,
    require_suppression_sync,
    send_window_line,
    shared_ledger_warning,
    stagger_plan,
    suppression_hit,
    verification_staleness,
    verify_refusal,
)

ACTOR = "engine"


class EmailGmailAdapter:
    name = "email"

    def constraints(self) -> ChannelConstraints:
        # PLAN.md §5.4 L6: email body <=120 words, subject required.
        return ChannelConstraints(max_words=120, max_sentences=None,
                                  max_chars=None, has_subject=True)

    # ------------------------------------------------------------------ #
    # stage
    # ------------------------------------------------------------------ #

    def stage(self, conn, cfg, message_ids: list[int], dry_run: bool = False,
              service_factory=None, allow_person=None) -> StageResult:
        """Turn APPROVED email messages into Gmail drafts.

        Only APPROVED rows stage — anything else is skipped by name, and trigger
        G3 aborts the transition anyway if this code is ever handed a forged row.

        Partial failure is expected and safe: each message commits on its own, a
        failure leaves that message APPROVED, and a re-run resumes without
        creating a duplicate draft (edge 22/23).
        """
        result = StageResult()
        allow_person = set(allow_person or ())

        # C1: import the shared suppression_list.txt BEFORE anything is staged.
        # This is the wiring §10(e) claimed and the engine did not have — without
        # it every address on the operator's real list was invisible here. It raises
        # rather than warning when the list cannot be trusted.
        sync = require_suppression_sync(conn, cfg, "stage")
        for warning in sync.warnings:
            result.warnings.append((0, warning))
        ledger = shared_ledger_warning(cfg)
        if ledger:
            result.warnings.append((0, ledger))

        if dry_run:
            return self._stage_dry_run(conn, cfg, message_ids, result, allow_person)

        factory = service_factory or gmail_compat.get_service
        service, sender_email = factory(cfg)

        plan = dict(zip(message_ids, stagger_plan(len(message_ids))))

        # ONE lock for the whole batch — locks.hold() is not reentrant, and the
        # daily cap is a batch-spanning resource.
        with locks.hold(cfg, STAGE_LOCK):
            if ledger:
                self._note_run_warning(conn, message_ids, ledger)
            for message_id in message_ids:
                if result.auth_expired:
                    result.skipped.append(
                        (message_id, "aborted: Gmail auth expired earlier in this run"))
                    continue
                self._stage_one(conn, cfg, service, message_id, result, allow_person,
                                plan.get(message_id))
        return result

    def _note_run_warning(self, conn, message_ids, text: str) -> None:
        """Put a run-level warning somewhere it outlives the terminal session."""
        for message_id in message_ids[:1]:
            try:
                note(conn, "message", message_id, ACTOR, f"WARNING: {text}")
            except Exception:                        # noqa: BLE001 — advisory only
                return

    def _stage_one(self, conn, cfg, service, message_id, result, allow_person,
                   planned_release=None) -> None:
        row = fetch_message(conn, message_id)
        if row is None:
            result.skipped.append((message_id, "no such message"))
            return

        refusal = self._pre_stage_refusal(conn, row, allow_person)
        if refusal:
            result.skipped.append((message_id, refusal))
            note(conn, "message", message_id, ACTOR, f"stage refused: {refusal}")
            return

        stale = verification_staleness(row)
        if stale:
            result.warnings.append((message_id, stale))

        address = row["address"].strip().lower()

        # Authoritative dedup against Gmail Sent (TICKET-694): a prospect already
        # emailed must never be touched twice. Outside the transaction — it is a
        # read, and a hit cancels rather than stages.
        if gmail_compat.already_sent_to(service, address):
            transition(conn, "message", message_id, "CANCELLED", ACTOR,
                       reason=f"already emailed {address} (Gmail Sent)",
                       evidence="gmail_sent_dedup")
            result.skipped.append(
                (message_id, f"already emailed {address} — message CANCELLED"))
            return

        day = phx_date()
        personalization = effective_personalization(conn, row["batch_id"],
                                                    row["personalization"])
        if personalization != row["personalization"]:
            result.warnings.append(
                (message_id,
                 f"batch {row['batch_id']} declares personalization="
                 f"{row['personalization']} but its bodies are near-identical — "
                 f"capping it as a template batch (20/day, not 30)"))
        conn.execute("BEGIN IMMEDIATE")     # cap count + reservation must be atomic
        try:
            reason = cap_refusal(conn, day, self.name, personalization)
            if reason:
                conn.rollback()
                result.skipped.append((message_id, reason))
                note(conn, "message", message_id, ACTOR, f"stage refused: {reason}")
                return

            record_stage_event(conn, message_id, self.name, personalization, day)

            draft_id = _existing_draft_id(service, address, row["subject"])
            if draft_id:
                result.warnings.append(
                    (message_id, f"adopted existing Gmail draft {draft_id} "
                                 "(same recipient + subject) instead of creating a duplicate"))
            else:
                draft_id = gmail_compat.create_draft(
                    service, address, row["subject"], row["body"])

            conn.execute("UPDATE messages SET gmail_draft_id = ? WHERE id = ?",
                         (draft_id, message_id))
            transition(
                conn, "message", message_id, "STAGED", ACTOR,
                reason=f"gmail draft for {address}; "
                       f"send window {send_window_line(row['timezone'])}; "
                       f"planned release {planned_release or 'unscheduled'} "
                       f"(3-5 min stagger, 10/window)",
                evidence=f"gmail_draft:{draft_id}",
            )
            conn.execute("COMMIT")
        except Exception as exc:
            _rollback(conn)
            if type(exc).__name__ == "RefreshError":
                # Headless runs cannot re-auth (standing retry-cap rule): abort the
                # rest of the batch, leave a human-gated line in the ledger, never
                # blind-retry. create_drafts.py uses exit 4 for this class.
                result.auth_expired = True
                result.failed.append((message_id, f"Gmail auth expired: {exc}"))
                _append_pending_action(
                    cfg,
                    "dm-engine stage aborted — Gmail token for operator_personal could not "
                    "refresh. Re-auth: `python ~/.config/skills/gmail-helper/scripts/"
                    "gmail_multi_auth.py --account operator_personal`, then re-run "
                    "`dmctl stage`.",
                )
                return
            result.failed.append((message_id, f"{type(exc).__name__}: {exc}"))
            return

        result.staged += 1
        result.artifacts.append(f"gmail_draft:{draft_id}")

    def _pre_stage_refusal(self, conn, row, allow_person) -> str | None:
        """Every readable gate, in the order a human would check them."""
        if row["channel"] != self.name:
            return f"channel is {row['channel']}, not email"
        if row["state"] != "APPROVED":
            return (f"not APPROVED (state={row['state']}) — staging requires a human "
                    "approval; there is no flag that skips it")
        if row["gmail_draft_id"]:
            return f"already staged as Gmail draft {row['gmail_draft_id']}"
        if not row["subject"]:
            return "email message has no subject"
        hit = suppression_hit(conn, row["contact_id"])
        if hit:
            return hit
        company = prospect_suppressed(conn, row["prospect_id"])
        if company:
            return company
        refusal = verify_refusal(row)
        if refusal:
            return refusal
        if row["person_key"] not in allow_person:
            touch = recent_person_touch(conn, row["id"], row["person_key"])
            if touch:
                return touch
        return None

    def _stage_dry_run(self, conn, cfg, message_ids, result, allow_person) -> StageResult:
        """Write what WOULD be drafted. Zero API calls, zero state changes."""
        plan = dict(zip(message_ids, stagger_plan(len(message_ids))))
        for message_id in message_ids:
            row = fetch_message(conn, message_id)
            if row is None:
                result.skipped.append((message_id, "no such message"))
                continue
            refusal = self._pre_stage_refusal(conn, row, allow_person)
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "dry_run": True,
                "message_id": row["id"],
                "channel": self.name,
                "batch_id": row["batch_id"],
                "variant_id": row["variant_id"],
                "sequence_step": row["sequence_step"],
                "prospect_key": row["prospect_key"],
                "company_name": row["company_name"],
                "to": row["address"],
                "verify": f"{row['verify_verdict']} via {row['address_source']}",
                "subject": row["subject"],
                "body": row["body"],
                "send_window": send_window_line(row["timezone"]),
                "planned_release": plan.get(message_id),
                "stagger_rule": "3-5 min between sends, 10 per window, 20/day",
                "would_stage": refusal is None,
                "blocked_reason": refusal,
                "gmail_sent_dedup": "not checked (dry run makes no API calls)",
            }
            path = dry_run_path(cfg, row["batch_id"], row["id"])
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            result.artifacts.append(str(path))
            if refusal:
                result.skipped.append((message_id, refusal))
            else:
                result.staged += 1
        return result

    # ------------------------------------------------------------------ #
    # reconcile — read-only Gmail, safe on any host
    # ------------------------------------------------------------------ #

    def reconcile(self, conn, cfg, service_factory=None) -> dict:
        """Detect real sends and bounces that happened OUTSIDE the engine.

        STAGED -> SENT when a copy shows up in Gmail Sent (evidence:
        `gmail_sent_scan:<msgid>`). SENT -> BOUNCED on a mailer-daemon return,
        which also writes a suppression row so the address can never be staged
        again (G8 then blocks it at message insert too).
        """
        factory = service_factory or gmail_compat.get_service
        service, _ = factory(cfg)
        counts = {"sent": 0, "bounced": 0, "errors": []}

        for row in conn.execute(
            "SELECT m.id, m.subject, m.staged_at, c.address FROM messages m "
            "JOIN contacts c ON c.id = m.contact_id "
            "WHERE m.state = 'STAGED' AND m.channel = 'email'"
        ).fetchall():
            try:
                found = _find_sent_copy(service, row["address"], row["subject"])
            except Exception as exc:
                counts["errors"].append((row["id"], f"{type(exc).__name__}: {exc}"))
                continue
            if not found:
                continue
            evidence = f"gmail_sent_scan:{found['id']}"
            if found.get("subject") and found["subject"] != row["subject"]:
                # The operator editing a draft before sending is allowed and expected;
                # record it so variant purity is footnoted where it matters (edge 25).
                evidence += "|edited_in_gmail"
            # One transaction per message: the id columns and the state change land
            # together, and transition() joins rather than commits mid-way.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE messages SET gmail_message_id = ?, gmail_thread_id = ? "
                    "WHERE id = ?",
                    (found["id"], found.get("threadId"), row["id"]),
                )
                transition(conn, "message", row["id"], "SENT", ACTOR,
                           reason="observed in Gmail Sent", evidence=evidence)
                conn.execute("COMMIT")
            except Exception as exc:
                _rollback(conn)
                counts["errors"].append((row["id"], f"{type(exc).__name__}: {exc}"))
                continue
            counts["sent"] += 1

        for row in conn.execute(
            "SELECT m.id, m.contact_id, c.address, c.channel FROM messages m "
            "JOIN contacts c ON c.id = m.contact_id "
            "WHERE m.state = 'SENT' AND m.channel = 'email'"
        ).fetchall():
            try:
                bounce = _find_bounce(service, row["address"])
            except Exception as exc:
                counts["errors"].append((row["id"], f"{type(exc).__name__}: {exc}"))
                continue
            if not bounce:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                transition(conn, "message", row["id"], "BOUNCED", ACTOR,
                           reason="mailer-daemon return",
                           evidence=f"gmail_bounce:{bounce['id']}")
                _suppress_row(conn, row["channel"], row["address"])
                conn.execute("COMMIT")
            except Exception as exc:
                _rollback(conn)
                counts["errors"].append((row["id"], f"{type(exc).__name__}: {exc}"))
                continue
            _append_suppression_file(cfg, row["address"])
            counts["bounced"] += 1

        return counts


# --------------------------------------------------------------------------- #
# Gmail helpers — every call shape here exists on tests/fakes.FakeGmailService
# --------------------------------------------------------------------------- #

def _existing_draft_id(service, address: str, subject: str) -> str | None:
    """A draft to this recipient with this subject that already exists in Gmail.

    Covers the crash-after-create-before-DB-write window (edge 23): the artifact
    is real, so adopt it rather than creating a second one. Orphans that do NOT
    match are left alone — cleanup is a human decision, never automatic.
    """
    try:
        resp = service.users().drafts().list(
            userId="me", q=f"to:{address}", maxResults=25).execute()
    except Exception:
        return None                     # fail-open: worst case we create the draft
    for draft in resp.get("drafts", []):
        message = draft.get("message") or {}
        message_id = message.get("id")
        if not message_id:
            continue
        try:
            full = service.users().messages().get(
                userId="me", id=message_id, format="metadata").execute()
        except Exception:
            continue
        if _header(full, "Subject") == subject and _header(full, "To") == address:
            return draft["id"]
    return None


def _find_sent_copy(service, address: str, subject: str) -> dict | None:
    resp = service.users().messages().list(
        userId="me", q=f"in:sent to:{address}", maxResults=5).execute()
    for stub in resp.get("messages", []):
        full = service.users().messages().get(userId="me", id=stub["id"]).execute()
        return {"id": full.get("id", stub["id"]),
                "threadId": full.get("threadId"),
                "subject": _header(full, "Subject")}
    return None


def _find_bounce(service, address: str) -> dict | None:
    resp = service.users().messages().list(
        userId="me", q=f"from:mailer-daemon {address}", maxResults=5).execute()
    messages = resp.get("messages", [])
    return {"id": messages[0]["id"]} if messages else None


def _header(message: dict, name: str) -> str | None:
    headers = (message.get("payload") or {}).get("headers") or []
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def _suppress_row(conn, channel: str, address: str) -> None:
    """Suppress a bounced address, in the canonical form trigger G8 matches.

    Raw SQL rather than `suppression.add()` on purpose: this runs inside the
    reconcile transaction, and `add()` opens its own (`with conn:`), which would
    commit the bounce transition half-way through.
    """
    from .. import suppression as suppression_module

    conn.execute(
        "INSERT OR IGNORE INTO suppression (channel, address, scope, reason, source) "
        "VALUES (?, ?, 'address', 'bounce', 'reconcile')",
        (channel, suppression_module.canonical_email(address)),
    )


def _append_suppression_file(cfg, address: str) -> None:
    """Two-way sync back to the skill's suppression_list.txt (PLAN.md §2.3).

    `suppression.py` is Owner B's module and owns that file's format, so this is a
    soft dependency: the DB row written above is the engine-side gate (G8 enforces
    it), and the file append is best effort.
    """
    try:
        from .. import suppression as suppression_module

        suppression_module.append_to_file(cfg.suppression_file, address, "bounce")
    except (ImportError, AttributeError, OSError):
        pass


def _rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def _append_pending_action(cfg, text: str) -> None:
    """Append a human-gated item to the pending-actions ledger (standing rule).

    Sandbox-guarded, exactly like `cli._note_pending_action`. Without this a
    rehearsal that built its Config from an unmodified `load_config()` would write
    a fabricated "re-auth needed" line into the operator's REAL ~/.config/pending-actions.md,
    where it is indistinguishable from a true one. That has already happened once.
    """
    from .. import report

    try:
        report.assert_sandboxed(cfg, cfg.pending_actions)
    except Exception:                       # noqa: BLE001 — refusing is the point
        return
    try:
        path = Path(cfg.pending_actions)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n- **[GATE {stamp}] {text}**\n")
    except OSError:
        pass


__all__ = ["EmailGmailAdapter"]
