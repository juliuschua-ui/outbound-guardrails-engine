"""LinkedIn channel: research-automated, HUMAN-SENT. Contract: PLAN.md §12.5, §6.2.

    ═══ THERE IS NO DM-SENDING CODE PATH IN THIS FILE. NOT EVEN BEHIND A FLAG. ═══

The tradeoff was decided in PLAN.md §6.2 and builders do not revisit it:
automated LinkedIn DMs violate the LinkedIn User Agreement (§8.2 — no bots or
automated access) and LinkedIn bans accounts for it. The operator has ONE personal
LinkedIn account. Losing it kills a channel permanently to save ~60 seconds per
message. So staging produces a card a human sends by hand:

  1. `data/outbox/linkedin/<message_id>-<slug>.md` — profile URL, the exact text
     in a copy-ready fence, the prospect-local send window, and the
     `dmctl mark-sent` line to run afterwards.
  2. a DASHBOARD approvals item (`{"items":[{"id","title","summary","link"}]}`) so
     the card surfaces where the operator already looks.

The operator pastes and sends; `dmctl mark-sent <id> --evidence "sent manually …"`
moves STAGED -> SENT. For LinkedIn the human IS the transport, so the second
release gate is inherent.

Allowed automation on this channel is READ-ONLY research via the browser-helper
skill (profile/company enrichment, finding where the ICP posts) under its own
caps — <=25 profile views/day, <=3 sessions/day, hard stop on any
checkpoint/captcha. That runs outside this engine; the LinkedIn cookie lives in
browser-helper's own .env and never in dm-engine.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from .. import locks
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
    record_stage_event,
    require_suppression_sync,
    send_window_line,
    shared_ledger_warning,
    stagger_plan,
    suppression_hit,
)

ACTOR = "engine"

_CARD_TEMPLATE = """# LinkedIn DM — {company_name}

- message id: {message_id}
- prospect: {prospect_key} · {market}
- variant: {variant_id} (step {sequence_step})
- profile: {profile}
- send window: {send_window}
- staged: {staged_at}

This engine does not send LinkedIn DMs. Automated DMs violate the LinkedIn User
Agreement and risk a permanent ban on the only account the operator has. Open the
profile, paste the text below, send it yourself.

## message — copy everything between the fences

~~~
{body}
~~~

## after you send

    dmctl mark-sent {message_id} --evidence "sent manually {today}"

If you decide not to send it:

    dmctl cancel {message_id} --reason "<why>"
"""


class LinkedInManualAdapter:
    name = "linkedin"

    def constraints(self) -> ChannelConstraints:
        # PLAN.md §5.4 L7: <=3 sentences, <=300 chars, no subject line.
        return ChannelConstraints(max_words=None, max_sentences=3,
                                  max_chars=300, has_subject=False)

    def stage(self, conn, cfg, message_ids: list[int], dry_run: bool = False,
              service_factory=None, allow_person=None) -> StageResult:
        """Write human-send cards for APPROVED LinkedIn messages.

        `service_factory` is accepted for interface symmetry and never used —
        this adapter touches no API at all.
        """
        result = StageResult()
        cards: list[dict] = []

        # C1: the shared suppression list is imported before anything is staged,
        # on every channel. It refuses the run when the list cannot be trusted.
        sync = require_suppression_sync(conn, cfg, "stage")
        for warning in sync.warnings:
            result.warnings.append((0, warning))
        ledger = shared_ledger_warning(cfg)
        if ledger:
            result.warnings.append((0, ledger))
        plan = dict(zip(message_ids, stagger_plan(len(message_ids))))

        if dry_run:
            for message_id in message_ids:
                row = fetch_message(conn, message_id)
                if row is None:
                    result.skipped.append((message_id, "no such message"))
                    continue
                refusal = self._pre_stage_refusal(conn, row)
                path = dry_run_path(cfg, row["batch_id"], row["id"])
                path.write_text(json.dumps({
                    "generated_at": _now_iso(),
                    "dry_run": True,
                    "message_id": row["id"],
                    "channel": self.name,
                    "profile": row["address"],
                    "body": row["body"],
                    "send_window": send_window_line(row["timezone"]),
                    "planned_release": plan.get(message_id),
                    "would_stage": refusal is None,
                    "blocked_reason": refusal,
                }, indent=2) + "\n", encoding="utf-8")
                result.artifacts.append(str(path))
                if refusal:
                    result.skipped.append((message_id, refusal))
                else:
                    result.staged += 1
            return result

        with locks.hold(cfg, STAGE_LOCK):
            for message_id in message_ids:
                card = self._stage_one(conn, cfg, message_id, result)
                if card:
                    cards.append(card)

        if cards:
            result.artifacts.append(str(_write_approvals_card(cfg, cards)))
        return result

    def _stage_one(self, conn, cfg, message_id, result) -> dict | None:
        row = fetch_message(conn, message_id)
        if row is None:
            result.skipped.append((message_id, "no such message"))
            return None

        refusal = self._pre_stage_refusal(conn, row)
        if refusal:
            result.skipped.append((message_id, refusal))
            note(conn, "message", message_id, ACTOR, f"stage refused: {refusal}")
            return None

        day = phx_date()
        personalization = effective_personalization(conn, row["batch_id"],
                                                    row["personalization"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            reason = cap_refusal(conn, day, self.name, personalization)
            if reason:
                conn.rollback()
                result.skipped.append((message_id, reason))
                note(conn, "message", message_id, ACTOR, f"stage refused: {reason}")
                return None

            record_stage_event(conn, message_id, self.name, personalization, day)
            path = _write_card(cfg, row)
            transition(conn, "message", message_id, "STAGED", ACTOR,
                       reason=f"linkedin human-send card; "
                              f"send window {send_window_line(row['timezone'])}",
                       evidence=f"outbox_card:{path.name}")
            conn.execute("COMMIT")
        except Exception as exc:
            _rollback(conn)
            result.failed.append((message_id, f"{type(exc).__name__}: {exc}"))
            return None

        result.staged += 1
        result.artifacts.append(str(path))
        return {
            "id": f"dmengine-msg-{row['id']}",
            "title": f"LinkedIn DM ready: {row['company_name']}",
            "summary": (f"{row['variant_id']} · {row['market']} · paste + send by hand, "
                        f"then `dmctl mark-sent {row['id']}`. "
                        f"Send window {send_window_line(row['timezone'])}."),
            "link": str(path),
        }

    def _pre_stage_refusal(self, conn, row) -> str | None:
        if row["channel"] != self.name:
            return f"channel is {row['channel']}, not linkedin"
        if row["state"] != "APPROVED":
            return (f"not APPROVED (state={row['state']}) — staging requires a human "
                    "approval; there is no flag that skips it")
        hit = suppression_hit(conn, row["contact_id"])
        if hit:
            return hit
        company = prospect_suppressed(conn, row["prospect_id"])
        if company:
            return company
        if not row["address"]:
            return "contact has no LinkedIn profile URL"
        return None

    def reconcile(self, conn, cfg, service_factory=None) -> dict:
        """No-op by design: a LinkedIn send leaves no trace the engine can read.

        STAGED -> SENT happens through `dmctl mark-sent <id> --evidence "…"`,
        which is the operator reporting what he did with his own hands. Replies are
        likewise recorded manually with `dmctl record-reply`.
        """
        return {"sent": 0, "bounced": 0,
                "note": "linkedin sends and replies are recorded manually "
                        "(dmctl mark-sent / dmctl record-reply)"}


# --------------------------------------------------------------------------- #
# card writers
# --------------------------------------------------------------------------- #

def _write_card(cfg, row):
    directory = cfg.outbox_dir / "linkedin"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{row['id']}-{_slug(row['company_name'])}.md"
    path.write_text(_CARD_TEMPLATE.format(
        company_name=row["company_name"],
        message_id=row["id"],
        prospect_key=row["prospect_key"],
        market=row["market"],
        variant_id=row["variant_id"],
        sequence_step=row["sequence_step"],
        profile=row["address"],
        send_window=send_window_line(row["timezone"]),
        staged_at=_now_iso(),
        body=row["body"],
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    ), encoding="utf-8")
    return path


def _write_approvals_card(cfg, items: list[dict]):
    """DASHBOARD approvals lane, exec-card shape (PLAN.md §2.3)."""
    directory = cfg.dashboard_approvals_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = directory / f"dmengine-{stamp}.json"
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")).get("items", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    seen = {item.get("id") for item in existing}
    merged = existing + [item for item in items if item["id"] not in seen]
    path.write_text(json.dumps({"items": merged}, indent=2) + "\n", encoding="utf-8")
    return path


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "prospect").lower()).strip("-")[:48] or "prospect"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


__all__ = ["LinkedInManualAdapter"]
