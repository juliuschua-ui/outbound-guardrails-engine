"""`dmctl` — the engine's only entrypoint. Contract: PLAN.md §12.6, §11.

THIN DISPATCH ONLY. Every command here parses arguments, opens the database, calls
exactly one owning module, and turns the result into an exit code. No business logic
lives in this file. Every command is wired to its owning module as of Phase 6; the
`Unwired` refusal below is the guard that stays behind, so a module that goes missing
or loses a function refuses with exit 2 and names what to fix rather than crashing.

Exit codes are contractual — a gate that refuses with exit 0 is invisible to any
script wrapping this CLI:

    0  ok
    1  partial (some work done, some not) / unexpected failure
    2  refused: a gate, a guard, or a command that is not wired yet
    3  gate-abort raised by a database trigger (G1–G11)
    4  auth: no usable Gmail credentials — headless runs cannot re-auth

THE ENGINE CANNOT SEND. There is no send subcommand, no --send flag, and no code
path in this file that transmits a message (PLAN.md §14 non-goal 1). `stage` puts a
draft where a human can review it; a second, separate human action releases it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sqlite3
import sys
from pathlib import Path

from . import config, db, report

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_REFUSED = 2
EXIT_GATE_ABORT = 3
EXIT_AUTH = 4

#: The triggers in 002_triggers.sql all announce themselves as 'G<n>: ...'.
_GATE_RE = re.compile(r"\bG\d+:")
#: Refusals raised by the state machine / approval layer — gates, not crashes.
_GATE_EXCEPTIONS = {"IllegalTransition", "GateRefused", "AssignmentDrift",
                    "StaleReviewFile", "NotATTY"}
_AUTH_TYPES = {"RefreshError", "TestModeError", "AuthError", "DefaultCredentialsError"}
_AUTH_MARKERS = ("invalid_grant", "invalid_scope", "credentials", "token has been expired")

DEFAULT_EXPERIMENT = "EXP-2026-08-skeptic"

#: Reply classes that unlock trigger G7 — i.e. that let Message 2 and its demo
#: link exist at all. Recording one of these is a claim about a human act, so it
#: needs a human at a terminal (§10 row (o) applied to the reply gate).
_HUMAN_REPLY_CLASSES = {"hot", "question", "objection", "no", "unsure"}


def _require_tty(what: str, alternative: str) -> None:
    """The one thing a headless agent does not have. No flag substitutes for it."""
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        raise Refused(
            f"{what} refuses on a non-TTY. This records a human act, so it needs a "
            f"human at a terminal — there is no --yes, no --force and no env "
            f"override by design (PLAN.md §7.3, §10 row (o)). {alternative}")


class Unwired(RuntimeError):
    """A module this command dispatches to has not been built yet."""


class Refused(RuntimeError):
    """The engine declined to act. Always exit 2, always with the reason."""


# --------------------------------------------------------------------------- #
# module resolution (import at dispatch time, so one broken module is one broken
# command rather than a CLI that will not start)
# --------------------------------------------------------------------------- #

def _module(name: str, owner: str):
    """Import a sibling module at dispatch time, never at import time."""
    try:
        return importlib.import_module(f".{name}", __package__)
    except ImportError as exc:
        raise Unwired(
            f"{name}.py is not built yet (Owner {owner}). {exc}") from exc


def _fn(name: str, owner: str, attr: str):
    module = _module(name, owner)
    func = getattr(module, attr, None)
    if func is None:
        raise Unwired(f"{name}.{attr}() is not implemented yet (Owner {owner})")
    return func


def _get_adapter(channel: str):
    """Delegates to channels.get_adapter(channel) -> ChannelAdapter [Owner D].

    PLAN.md §12.5 declares `get_adapter` in `channels/base.py` and the package
    re-exports it; both spellings are accepted so a later move of the symbol between
    the two cannot silently break staging.
    """
    for module_name in ("channels", "channels.base"):
        try:
            module = importlib.import_module(f".{module_name}", __package__)
        except ImportError:
            continue
        func = getattr(module, "get_adapter", None)
        if func is not None:
            return func(channel)
    raise Unwired("channels.get_adapter() is not implemented yet (Owner D)")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def _reject_sms(channel: str | None) -> None:
    """PLAN.md §6.3: cold SMS is excluded entirely, and the CLI says why."""
    if channel == "sms":
        raise Refused(
            "cold SMS is excluded: TCPA requires documented express written consent, "
            "and scraped numbers have none. There is no sms adapter and there will "
            "not be one (PLAN.md §6.3, non-goal 3).")


def _connect(cfg, require_schema: bool = True) -> sqlite3.Connection:
    conn = db.connect(cfg)
    if require_schema and not db.applied_versions(conn):
        conn.close()
        raise Refused(f"no schema in {cfg.db_path} — run `dmctl init` first")
    return conn


def _start_run(conn, command: str, args: argparse.Namespace) -> int | None:
    payload = {k: (str(v) if isinstance(v, Path) else v)
               for k, v in vars(args).items() if k not in ("func", "command")}
    try:
        cur = conn.execute("INSERT INTO runs (command, args) VALUES (?, ?)",
                           (command, json.dumps(payload, default=str)))
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error:
        return None


def _finish_run(conn, run_id: int | None, status: str, detail) -> None:
    if run_id is None:
        return
    try:
        conn.execute(
            "UPDATE runs SET finished_at = datetime('now'), status = ?, detail = ? "
            "WHERE id = ?",
            (status, json.dumps(detail, default=str) if detail is not None else None,
             run_id))
        conn.commit()
    except sqlite3.Error:
        pass


def _note_pending_action(cfg, text: str) -> None:
    """Append a human-gated next step to the ledger (standing rule + §9 edge 24).

    Sandbox-guarded: a test run must never append to the operator's real ledger, where a
    fabricated "re-auth needed" line is indistinguishable from a true one.
    """
    try:
        report.assert_sandboxed(cfg, cfg.pending_actions)
    except report.ReportError:
        return
    try:
        path = Path(cfg.pending_actions)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- [ ] {text}\n")
    except OSError:
        pass


def _sync_suppression(conn, cfg, gate: str):
    """Import the shared suppression_list.txt before a gate. Refuses, never guesses.

    PLAN.md §10(e) and §2.3 both claim "file-sync import before every stage run".
    Until this existed, `suppression.sync_from_file` had ZERO callers and every
    address on the shared list was invisible to the engine while the dashboard
    reported a healthy suppression count.
    """
    module = _module("suppression", "B")
    try:
        report_ = module.require_sync(conn, cfg, gate)
    except module.SuppressionSyncError as exc:
        raise Refused(str(exc)) from exc
    for warning in report_.warnings:
        print(f"WARN  suppression: {warning}")
    return report_


def _is_auth_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _AUTH_TYPES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _print_result(label: str, value) -> None:
    if isinstance(value, dict):
        print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in value.items()))
    else:
        print(f"{label}: {value}")


# --------------------------------------------------------------------------- #
# commands — one call-through each
# --------------------------------------------------------------------------- #

def cmd_init(args, cfg) -> int:
    conn = _connect(cfg, require_schema=False)
    try:
        applied = db.migrate(conn, cfg.migrations_dir)
        print(f"migrations applied: {applied or 'none (already current)'}")
        seed = Path(args.seed) if args.seed else Path(
            cfg.variants_dir).joinpath(f"{DEFAULT_EXPERIMENT}.json")
        if not seed.exists():
            print(f"note: no variant seed at {seed} — skipping seed load "
                  "(Owner B ships data/variants/*.json)")
            return EXIT_OK
        # Delegates to variants.load_seed(conn, json_path) [Owner B]
        loaded = _fn("variants", "B", "load_seed")(conn, seed)
        print(f"variants seeded: {loaded}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_import_prospects(args, cfg) -> int:
    conn = _connect(cfg)
    run_id = _start_run(conn, "import-prospects", args)
    try:
        _sync_suppression(conn, cfg, "import-prospects")
        # Delegates to prospects.import_prospects(conn, path, market) -> ImportResult [Owner B]
        result = _fn("prospects", "B", "import_prospects")(conn, Path(args.path), args.market)
        added = getattr(result, "added", 0)
        dupes = getattr(result, "dupes", 0)
        skips = list(getattr(result, "skips", []) or [])
        errors = list(getattr(result, "errors", []) or [])
        print(f"added={added} dupes={dupes} skipped={len(skips)} errors={len(errors)}")
        for key, reason in skips:
            print(f"  skip  {key}: {reason}")
        for key, reason in errors:
            print(f"  ERROR {key}: {reason}")
        status = "partial" if errors else "ok"
        _finish_run(conn, run_id, status,
                    {"added": added, "dupes": dupes, "skips": skips, "errors": errors})
        return EXIT_PARTIAL if errors else EXIT_OK
    finally:
        conn.close()


def cmd_qualify(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to prospects.qualify(conn, prospect_id, actor) [Owner B]
        _fn("prospects", "B", "qualify")(conn, args.prospect_id, args.actor)
        conn.commit()
        print(f"prospect {args.prospect_id} -> QUALIFIED")
        return EXIT_OK
    finally:
        conn.close()


def cmd_exclude(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to prospects.exclude(conn, prospect_id, reason, actor) [Owner B]
        _fn("prospects", "B", "exclude")(conn, args.prospect_id, args.reason, args.actor)
        conn.commit()
        print(f"prospect {args.prospect_id} -> EXCLUDED ({args.reason})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_set_casual(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to prospects.set_casual [Owner B], which validates the value and
        # stamps updated_at.
        _fn("prospects", "B", "set_casual")(conn, args.prospect_id, args.casual)
        conn.commit()
        print(f"prospect {args.prospect_id} company_casual = {args.casual!r}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_verify(args, cfg) -> int:
    """SMTP-verify addresses, or import verdicts produced by an authoritative run.

    `--import` is the documented VPS workflow, and it used to be `json.loads` plus
    unconditional trust: a hand-written file turned a FAIL address into PASS and it
    staged to a real Gmail draft. The import now carries provenance (who produced
    it, on which host, when) and anything unattested or stale is DEGRADED rather
    than trusted — see prospects.load_verify_import.
    """
    conn = _connect(cfg)
    try:
        attestation = None
        if args.import_file:
            # Delegates to prospects.load_verify_import [Owner B]: it owns the
            # envelope shape because it owns what a verdict may do to a contact.
            results, attestation = _fn("prospects", "B", "load_verify_import")(
                Path(args.import_file))
            for line in attestation.describe():
                print(f"  import: {line}")
        else:
            addresses = [r[0] for r in conn.execute(
                "SELECT DISTINCT address FROM contacts WHERE channel = 'email' "
                "AND (verify_verdict IS NULL OR ?)", (1 if args.all else 0,))]
            if not addresses:
                print("nothing to verify")
                return EXIT_OK
            # Delegates to gmail_compat.verify_batch_smtp [Owner D] — the only module
            # allowed to touch the gmail-helper skill.
            results = _fn("gmail_compat", "D", "verify_batch_smtp")(cfg, addresses)
            if args.emit:
                path = _fn("prospects", "B", "write_verify_export")(
                    Path(args.emit), results)
                print(f"attested verdicts: {path}")
        # Delegates to prospects.record_verify_verdicts [Owner B]: `contacts` is a
        # prospects.py table, so the write lives there and this stays dispatch (§12.6).
        outcome = _fn("prospects", "B", "record_verify_verdicts")(
            conn, results, attestation=attestation)
        conn.commit()
        print(f"verdicts recorded: {outcome.recorded}")
        for address in outcome.unknown:
            print(f"  no contact row for {address} — verdict not recorded")
        for address, note in outcome.degraded:
            print(f"  DEGRADED {address}: {note}")
        for address, note in outcome.refused:
            print(f"  REFUSED  {address}: {note}")
        if outcome.blocklisted:
            print(f"WARN  {outcome.blocklisted} result(s) mention a blocklist — WSL's residential "
                  "IP is Spamhaus-PBL-listed. Re-verify from the VPS "
                  "(ssh user@send-host.example) and import with --import.")
        return EXIT_PARTIAL if outcome.refused else EXIT_OK
    finally:
        conn.close()


def cmd_sync_suppression(args, cfg) -> int:
    """Import the shared suppression_list.txt into the engine database."""
    conn = _connect(cfg)
    try:
        report_ = _sync_suppression(conn, cfg, "sync-suppression")
        conn.commit()
        print(f"suppression file: {report_.path}")
        print(f"addresses in file: {report_.file_addresses}  "
              f"new rows: {report_.added}  "
              f"prospects moved to SUPPRESSED: {report_.prospects_suppressed}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_new_batch(args, cfg) -> int:
    _reject_sms(args.channel)
    conn = _connect(cfg)
    try:
        # Delegates to variants.new_batch [Owner B]: batch_seq is the §4.3 round-robin
        # offset, so it is allocated next to the algorithm that consumes it.
        seq = _fn("variants", "B", "new_batch")(
            conn, args.batch_id, args.experiment, args.market, args.channel,
            args.personalization)
        conn.commit()
        print(f"batch {args.batch_id} created ({args.channel}, "
              f"{args.personalization}, seq={seq})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_assign(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to variants.assign_batch(conn, experiment_id, batch_id) [Owner B]
        rows = _fn("variants", "B", "assign_batch")(conn, args.experiment, args.batch)
        conn.commit()
        print(f"assigned: {len(rows)}")
        for row in rows:
            print(f"  {row.get('prospect_key', row.get('prospect_id'))} -> "
                  f"{row.get('variant_id')}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_compose(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to compose.emit_compose_job(conn, cfg, batch_id) -> Path [Owner C]
        path = _fn("compose", "C", "emit_compose_job")(conn, cfg, args.batch)
        conn.commit()
        print(f"compose job: {path}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_ingest_drafts(args, cfg) -> int:
    conn = _connect(cfg)
    run_id = _start_run(conn, "ingest-drafts", args)
    try:
        # Delegates to compose.ingest_drafts(conn, cfg, path) -> IngestResult [Owner C]
        result = _fn("compose", "C", "ingest_drafts")(conn, cfg, Path(args.path))
        conn.commit()
        ready = getattr(result, "ready", 0)
        failed = getattr(result, "failed", 0)
        missing = list(getattr(result, "missing", []) or [])
        unknown = list(getattr(result, "unknown", []) or [])
        print(f"ready={ready} lint_failed={failed} missing={len(missing)} "
              f"unknown={len(unknown)}")
        for key in missing:
            print(f"  missing draft for {key}")
        for key in unknown:
            print(f"  ERROR unknown prospect_key {key}")
        _finish_run(conn, run_id, "partial" if (failed or missing or unknown) else "ok",
                    {"ready": ready, "failed": failed, "missing": missing,
                     "unknown": unknown})
        return EXIT_PARTIAL if (failed or missing or unknown) else EXIT_OK
    finally:
        conn.close()


def cmd_lint_check(args, cfg) -> int:
    """No `_connect`, no `cfg` use, no `_start_run` — this touches no database at
    all, deliberately: a self-check an agent can run freely, any number of times,
    against any batch, without a live `dm.db` or a run-log row to show for it."""
    # Delegates to compose.dry_run_lint(compose_job_path, drafts_path)
    # -> IngestResult [Owner C]
    result = _fn("compose", "C", "dry_run_lint")(
        Path(args.compose_job), Path(args.drafts))
    ready = getattr(result, "ready", 0)
    failed = getattr(result, "failed", 0)
    missing = list(getattr(result, "missing", []) or [])
    unknown = list(getattr(result, "unknown", []) or [])
    print(f"ready={ready} lint_failed={failed} missing={len(missing)} "
          f"unknown={len(unknown)}")
    for detail in getattr(result, "details", []):
        if detail.get("outcome") == "lint_failed":
            print(f"  LINT_FAILED {detail['prospect_key']}: {detail['reason']}")
    for key in missing:
        print(f"  missing draft for {key}")
    for key in unknown:
        print(f"  ERROR unknown prospect_key {key}")
    return EXIT_PARTIAL if (failed or missing or unknown) else EXIT_OK


def cmd_review(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        _sync_suppression(conn, cfg, "review")
        # Delegates to approval.write_review_file(conn, cfg, batch_id) -> Path [Owner D]
        path = _fn("approval", "D", "write_review_file")(conn, cfg, args.batch)
        print(f"review file: {path}")
        print("edit the checkboxes, then AT A TERMINAL: dmctl approve --from-review "
              f"{path}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_review_json(args, cfg) -> int:
    """Read-only. Structured view of a review file's blocks, for a UI to render
    (the DASHBOARD review lane — see MANUAL.md). No TTY needed: nothing here
    decides anything, it only reads the file `dmctl review` already wrote."""
    path = Path(cfg.review_dir) / f"{args.batch}-review.md"
    if not path.exists():
        raise Refused(f"no review file for {args.batch} — run `dmctl review --batch "
                      f"{args.batch}` first")
    # Delegates to approval.parse_review_blocks(path) -> list[dict] [Owner D]
    blocks = _fn("approval", "D", "parse_review_blocks")(path)
    # The review file records a DECISION (the checkbox); the database records what
    # became of it. A UI reading only the file cannot tell an approved message from
    # one already staged into a Gmail draft, so it keeps offering work that is done.
    # Merge the real state in — read-only, and absent for any row the file names
    # that the database doesn't (which `approve` already reports as "unknown id").
    conn = _connect(cfg)
    try:
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, state, prospect_id FROM messages WHERE batch_id = ?",
                (args.batch,))
        }
    finally:
        conn.close()
    for block in blocks:
        row = rows.get(block.get("message_id"))
        block["state"] = row["state"] if row else None
        # replies attach to the PROSPECT, not the message — `log-reply` needs the
        # id, and the review file only ever carried a human-readable domain.
        block["prospect_id"] = row["prospect_id"] if row else None
    print(json.dumps({"batch": args.batch, "review_path": str(path), "blocks": blocks},
                     ensure_ascii=False))
    return EXIT_OK


def cmd_decide(args, cfg) -> int:
    """The DASHBOARD-lane sibling of `approve` — same gate, different proof.

    `approve` proves a human by requiring a TTY. This command proves nothing by
    itself: it flips one checkbox in the review file exactly as a hand-edit
    would, then calls the SAME `approval.approve_from_review()` `approve` uses
    — same content-digest binding, same staleness check, same suppression
    re-check (PLAN.md §7.3). Every protection stays; only the entry point
    changes. That's why `--actor` has no default here unlike every other
    actor-taking command: whatever calls this must say who it is, so the audit
    trail in `state_log` never reads "operator" for a click a script made.
    """
    path = Path(cfg.review_dir) / f"{args.batch}-review.md"
    if not path.exists():
        raise Refused(f"no review file for {args.batch} — run `dmctl review --batch "
                      f"{args.batch}` first")
    conn = _connect(cfg)
    run_id = _start_run(conn, "decide", args)
    try:
        _sync_suppression(conn, cfg, "decide")
        # Delegates to approval.set_review_mark / approve_from_review [Owner D]
        _fn("approval", "D", "set_review_mark")(
            path, args.message_id, args.choice, args.reason)
        result = _fn("approval", "D", "approve_from_review")(conn, cfg, path, args.actor)
        conn.commit()
        approved = getattr(result, "approved", 0)
        rejected = getattr(result, "rejected", 0)
        skipped = getattr(result, "skipped", 0)
        notes = getattr(result, "notes", [])
        print(f"approved={approved} rejected={rejected} skipped={skipped}")
        for message_id, note in notes:
            print(f"  {message_id}: {note}")
        # approve_from_review() processes every marked row in the review file, not
        # just this call's target — on any batch with more than one message, rows
        # nobody has decided yet always show up as "skipped: no box marked", which
        # is not a failure of THIS call. Success/failure is judged on whether the
        # one message `decide` was asked to move is among the skipped notes, not
        # on whether the rest of the file happens to be fully decided yet.
        target_skipped = any(mid == args.message_id for mid, _ in notes)
        _finish_run(conn, run_id, "ok" if not target_skipped else "partial",
                    {"approved": approved, "rejected": rejected, "skipped": skipped})
        return EXIT_OK if not target_skipped else EXIT_PARTIAL
    finally:
        conn.close()


def cmd_approve(args, cfg) -> int:
    """PLAN.md §7.3 / §10 row (o): a headless agent cannot approve.

    BOTH paths require a TTY. `--from-review` used to be the exception, and a review
    file is a file an agent can write — `dmctl review` then flipping `- [ ]` to
    `- [x]` was a complete, human-free path from draft to Gmail draft. There is
    deliberately no --yes, no --force and no env override on this command.
    """
    _require_tty(
        "approve",
        "Approval is the gate; run it yourself from a terminal after reading the "
        "review file.")
    if not args.from_review:
        if not args.batch:
            raise Refused("interactive approve needs --batch")
        # Delegates to approval.approve_interactive(conn, cfg, batch_id) [Owner D]
        conn = _connect(cfg)
        try:
            _sync_suppression(conn, cfg, "approve")
            result = _fn("approval", "D", "approve_interactive")(conn, cfg, args.batch)
            conn.commit()
        finally:
            conn.close()
    else:
        conn = _connect(cfg)
        try:
            _sync_suppression(conn, cfg, "approve")
            # Delegates to approval.approve_from_review(conn, cfg, review_path, actor) [Owner D]
            result = _fn("approval", "D", "approve_from_review")(
                conn, cfg, Path(args.from_review), "operator")
            conn.commit()
        finally:
            conn.close()
    approved = getattr(result, "approved", 0)
    rejected = getattr(result, "rejected", 0)
    skipped = getattr(result, "skipped", 0)
    print(f"approved={approved} rejected={rejected} skipped={skipped}")
    return EXIT_OK


def cmd_stage(args, cfg) -> int:
    _reject_sms(args.channel)
    conn = _connect(cfg)
    run_id = _start_run(conn, "stage", args)
    try:
        rows = conn.execute(
            "SELECT id, channel FROM messages WHERE batch_id = ? AND state = 'APPROVED' "
            "ORDER BY id", (args.batch,)).fetchall()
        if args.limit:
            rows = rows[:args.limit]
        if not rows:
            print("nothing APPROVED in this batch to stage")
            _finish_run(conn, run_id, "ok", {"staged": 0})
            return EXIT_OK
        by_channel: dict[str, list[int]] = {}
        for row in rows:
            by_channel.setdefault(row["channel"], []).append(row["id"])

        staged = failed = 0
        skipped: list = []
        warnings: list = []
        allow_person = set(args.allow_person or ())
        for channel, ids in by_channel.items():
            _reject_sms(channel)
            # Delegates to channels.get_adapter(channel).stage(conn, cfg, ids, dry_run) [Owner D]
            adapter = _get_adapter(channel)
            result = adapter.stage(conn, cfg, ids, args.dry_run,
                                   allow_person=allow_person)
            staged += getattr(result, "staged", 0)
            skipped.extend(getattr(result, "skipped", []) or [])
            warnings.extend(getattr(result, "warnings", []) or [])
            failed += len(getattr(result, "failed", []) or [])
        conn.commit()
        print(f"staged={staged} skipped={len(skipped)} failed={failed}"
              + (" (dry run: no state changed)" if args.dry_run else ""))
        for message_id, reason in warnings:
            print(f"  WARN  message {message_id}: {reason}")
        for message_id, reason in skipped:
            print(f"  skip  message {message_id}: {reason}")
        _finish_run(conn, run_id, "partial" if failed else "ok",
                    {"staged": staged, "skipped": skipped, "failed": failed})
        return EXIT_PARTIAL if failed else EXIT_OK
    finally:
        conn.close()


def cmd_reconcile(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # Delegates to channels.get_adapter(channel).reconcile(conn, cfg) -> dict [Owner D]
        adapter = _get_adapter(args.channel)
        result = adapter.reconcile(conn, cfg)
        conn.commit()
        _print_result("reconcile", result)
        return EXIT_OK
    finally:
        conn.close()


def cmd_mark_sent(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        # One transaction: a refused transition must not leave sent_at/sent_evidence
        # written on a message that never went anywhere.
        with conn:
            conn.execute(
                "UPDATE messages SET sent_at = datetime('now'), sent_evidence = ? "
                "WHERE id = ?", (args.evidence, args.message_id))
            # G4 refuses this unless the row is STAGED and carries evidence.
            replies_mod.transition(conn, "message", args.message_id, "SENT",
                                   args.actor, reason="manual mark-sent",
                                   evidence=args.evidence)
        print(f"message {args.message_id} -> SENT ({args.evidence})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_scan_replies(args, cfg) -> int:
    conn = _connect(cfg)
    run_id = _start_run(conn, "scan-replies", args)
    try:
        from . import replies as replies_mod
        result = replies_mod.scan(conn, cfg, newer_than_days=args.days)
        _print_result("scan-replies", result)
        _finish_run(conn, run_id, "ok", result)
        return EXIT_OK
    finally:
        conn.close()


def cmd_ingest_triage(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        count = replies_mod.ingest_triage(conn, cfg, Path(args.path))
        print(f"replies recorded from triage file: {count}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_record_reply(args, cfg) -> int:
    """Record a reply by hand. A HUMAN class needs a human at the terminal.

    `hot`/`question`/`objection`/`no`/`unsure` are what trigger G7 counts, so this
    one command is what unlocks Message 2 and its demo link. Left ungated, a
    headless agent could manufacture the reply that earns the pitch — the reply
    gate would have been satisfied by a single CLI call with `--actor operator`
    defaulted in. The automated path is `dmctl ingest-triage`, which records what
    the Gmail scan actually saw.
    """
    _reject_sms(args.channel)
    if args.classification in _HUMAN_REPLY_CLASSES:
        _require_tty(
            f"record-reply --class {args.classification}",
            "A human reply class unlocks Message 2 (trigger G7). For the automated "
            "path use `dmctl scan-replies` or `dmctl ingest-triage <file>`, which "
            "record what Gmail actually contained.")
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        replies_mod.record_reply(conn, args.prospect_id, args.channel,
                                 args.classification, args.snippet, args.actor)
        print(f"reply recorded for prospect {args.prospect_id} "
              f"({args.classification}, is_human="
              f"{int(replies_mod.is_human_class(args.classification))})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_log_reply(args, cfg) -> int:
    """The DASHBOARD-lane sibling of `record-reply` — same write, different proof.

    `record-reply` proves a human by requiring a TTY for the classes that unlock
    Message 2 (G7). This one proves nothing by itself; it calls the SAME
    `replies.record_reply()` and is reachable from DASHBOARD's bearer-token API,
    exactly as `decide` is to `approve`.

    The thing being protected here is not send capability — there is none — but
    the EVIDENCE the whole experiment is scored on. A fabricated `hot` both
    unlocks the demo-link email and moves the variant scoreboard. So the honesty
    requirement is stricter than elsewhere, not looser: `--actor` is REQUIRED
    with no default, so `replies.actor` can never read "operator" for a row a
    click created, and `dmctl stats --replies` prints verbatim text a human can
    check against the real inbox.

    The automated paths remain better whenever they apply: `scan-replies` and
    `ingest-triage` record what Gmail actually contained. This exists for the DM
    channels, where a hand-sent message leaves nothing to scan.
    """
    _reject_sms(args.channel)
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        replies_mod.record_reply(conn, args.prospect_id, args.channel,
                                 args.classification, args.snippet, args.actor)
        print(f"reply recorded for prospect {args.prospect_id} "
              f"({args.classification}, is_human="
              f"{int(replies_mod.is_human_class(args.classification))}, "
              f"actor={args.actor})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_attrib(args, cfg) -> int:
    conn = _connect(cfg)
    run_id = _start_run(conn, "attrib", args)
    try:
        from . import attribution
        total = sum(1 for _ in attribution.load_ghl_export(Path(args.ghl_export)))
        candidates = attribution.match(conn, Path(args.ghl_export), cfg)
        auto = [c for c in candidates if c.auto]
        manual = [c for c in candidates if not c.auto]
        print(f"ghl contacts read={total} auto-attributed={len(auto)} "
              f"manual-confirm cards={len(manual)} unmatched(organic)="
              f"{total - len(candidates)}")
        for candidate in auto:
            print(f"  booked prospect {candidate.prospect_id} via {candidate.method} "
                  f"({candidate.confidence}): {candidate.evidence}")
        for candidate in manual:
            print(f"  CARD  prospect {candidate.prospect_id} <- ghl "
                  f"{candidate.ghl_contact_id}: {candidate.evidence}")
        _finish_run(conn, run_id, "ok",
                    {"read": total, "auto": len(auto), "manual": len(manual),
                     "unmatched": total - len(candidates)})
        return EXIT_OK
    finally:
        conn.close()


def cmd_book(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        from . import attribution
        attribution.book(conn, args.prospect_id, args.method, args.evidence,
                         args.actor, outcome=args.outcome)
        print(f"prospect {args.prospect_id} -> {args.outcome} "
              f"({args.method}/{attribution.CONFIDENCE_BY_METHOD[args.method]})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_sheet(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        rows = report.sheet_rows(conn, full=args.full)
        if args.csv:
            report.write_csv(rows, Path(args.csv))
            print(f"{len(rows)} rows -> {args.csv}")
            return EXIT_OK
        if not rows:
            print("no attempts have reached SENT yet — the sheet is empty")
            return EXIT_OK
        columns = list(rows[0].keys())
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join("" if row[c] is None else str(row[c]) for c in columns))
        print(f"({len(rows)} rows)")
        return EXIT_OK
    finally:
        conn.close()


def cmd_stats(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        # All statistics — counts, rates, Wilson intervals, and the min-n gate — are
        # computed in stats.py. This command only renders what it is handed; it never
        # recomputes a rate and never routes around the gate (§10 row (h)).
        summary = _fn("stats", "B", "summary")(conn, args.experiment, args.by_family)
        verbatim = None
        if args.replies:
            verbatim = _fn("stats", "B", "replies_by_variant")(conn, args.experiment)
        print(report.render_stats(summary, replies_by_variant=verbatim))
        return EXIT_OK
    finally:
        conn.close()


def cmd_metrics_emit(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        path = report.emit_dashboard_metrics(conn, cfg)
        print(f"metrics: {path}")
        return EXIT_OK
    finally:
        conn.close()


def cmd_suppress(args, cfg) -> int:
    _reject_sms(args.channel)
    conn = _connect(cfg)
    try:
        # Delegates to suppression.add [Owner B], which owns scope semantics and the
        # two-way sync with the gmail-helper suppression file.
        _fn("suppression", "B", "add")(conn, args.channel, args.address.lower(),
                                       args.scope, args.reason, "dmctl")
        conn.commit()
        print(f"suppressed {args.address} ({args.channel}/{args.scope}/{args.reason})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_status(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        print(report.render_status(report.status_summary(conn, cfg)))
        return EXIT_OK
    finally:
        conn.close()


def cmd_cancel(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        replies_mod.transition(conn, "message", args.message_id, "CANCELLED",
                               args.actor, reason=args.reason)
        conn.commit()
        print(f"message {args.message_id} -> CANCELLED ({args.reason})")
        return EXIT_OK
    finally:
        conn.close()


def cmd_drop_from_batch(args, cfg) -> int:
    """Remove a prospect from a batch by cancelling its non-terminal messages.

    Assumption (reversible, logged): 'dropping' means the prospect stops being part
    of this batch's output; the assignment row stays so the variant it was given can
    never be silently re-rolled (PLAN.md §4.3 stability, §9 edge 14).
    """
    conn = _connect(cfg)
    try:
        from . import replies as replies_mod
        rows = conn.execute(
            "SELECT id FROM messages WHERE batch_id = ? AND prospect_id = ? "
            "AND state IN ('COMPOSED','LINT_FAILED','READY')",
            (args.batch, args.prospect_id)).fetchall()
        for row in rows:
            replies_mod.transition(conn, "message", row["id"], "CANCELLED", args.actor,
                                   reason=f"dropped from batch {args.batch}")
        conn.commit()
        print(f"cancelled {len(rows)} message(s) for prospect {args.prospect_id} "
              f"in {args.batch}")
        if not rows:
            print("nothing to drop (no COMPOSED/LINT_FAILED/READY message here)")
        return EXIT_OK
    finally:
        conn.close()


def cmd_variants(args, cfg) -> int:
    conn = _connect(cfg)
    try:
        if args.action == "retire":
            # Delegates to variants.retire(conn, variant_id, force, reason, actor) [Owner B]
            _fn("variants", "B", "retire")(conn, args.variant_id, args.force,
                                           args.reason, args.actor)
            conn.commit()
            print(f"variant {args.variant_id} retired")
        else:
            # Delegates to variants.declare_winner(conn, variant_id, force, reason, actor)
            # [Owner B] — §4.4 requires the same min-n gate as retire.
            _fn("variants", "B", "declare_winner")(conn, args.variant_id, args.force,
                                                   args.reason, args.actor)
            conn.commit()
            print(f"variant {args.variant_id} declared winner")
        return EXIT_OK
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dmctl",
        description="Outbound DM Engine. Composes, gates and stages outreach. "
                    "It cannot send — a human always releases the message.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text, func):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    p = add("init", "create/upgrade the database and load the variant seed", cmd_init)
    p.add_argument("--seed", help="variant seed JSON (default: data/variants/<exp>.json)")

    p = add("import-prospects", "import a prospects.json (contract §12.7)",
            cmd_import_prospects)
    p.add_argument("path")
    p.add_argument("--market", required=True, help="'City ST', e.g. 'Boise ID'")

    p = add("qualify", "NEW -> QUALIFIED", cmd_qualify)
    p.add_argument("prospect_id", type=int)
    p.add_argument("--actor", default="operator")

    p = add("exclude", "mark a prospect EXCLUDED (terminal)", cmd_exclude)
    p.add_argument("prospect_id", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="operator")

    p = add("set-casual", "fix a badly casualized company name", cmd_set_casual)
    p.add_argument("prospect_id", type=int)
    p.add_argument("casual")

    p = add("verify", "SMTP-verify contact addresses (authoritative on the VPS)",
            cmd_verify)
    p.add_argument("--all", action="store_true", help="re-verify already-verified rows")
    p.add_argument("--import", dest="import_file",
                   help="import verdicts produced by a VPS run (attested envelope; "
                        "a bare list is accepted but every verdict is degraded)")
    p.add_argument("--emit", help="write this run's verdicts as an attested envelope "
                                  "for `--import` on another host")

    p = add("sync-suppression", "import the shared suppression_list.txt (§10 row (e))",
            cmd_sync_suppression)

    p = add("new-batch", "open a batch", cmd_new_batch)
    p.add_argument("batch_id")
    p.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    p.add_argument("--market", required=True)
    p.add_argument("--channel",
                   choices=("email", "linkedin", "whatsapp", "messenger", "sms"),
                   default="email")
    # The operator's rule makes 30/day the EXCEPTION for uniquely-written bodies, so the
    # default is the 20/day class. Declaring `unique` is a claim about the copy,
    # and channels.base.effective_personalization checks it against the bodies.
    p.add_argument("--personalization", choices=("template", "unique"),
                   default="template",
                   help="body class; 'unique' (cap 30/day) must be earned — the "
                        "stage gate re-measures it against the actual bodies")

    p = add("assign", "deterministically assign variants to a batch (§4.3)", cmd_assign)
    p.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    p.add_argument("--batch", required=True)

    p = add("compose", "emit the compose job for the outreach-specialist", cmd_compose)
    p.add_argument("--batch", required=True)

    p = add("ingest-drafts", "ingest agent drafts and lint them (contract §12.8)",
            cmd_ingest_drafts)
    p.add_argument("path")

    p = add("lint-check", "dry-run the lint rules against a candidate drafts file "
                          "(no DB, no writes — for self-check before ingest-drafts)",
            cmd_lint_check)
    p.add_argument("compose_job", help="the *-composejob.json this drafts file answers")
    p.add_argument("drafts", help="candidate drafts JSON, same shape ingest-drafts expects")

    p = add("review", "write the human review file for a batch", cmd_review)
    p.add_argument("--batch", required=True)

    p = add("review-json", "read-only JSON view of a review file's blocks "
                           "(no TTY — for a UI; see MANUAL.md 'DASHBOARD review lane')",
            cmd_review_json)
    p.add_argument("--batch", required=True)

    # Bearer-token-gated, not TTY-gated — the DASHBOARD-lane sibling of `approve`.
    # See the cmd_decide / approval.py docstrings before touching this command.
    p = add("decide", "approve/reject ONE message without a TTY (DASHBOARD review "
                      "lane — same gate as `approve`, different proof of human)",
            cmd_decide)
    p.add_argument("--batch", required=True)
    p.add_argument("message_id", type=int)
    p.add_argument("--choice", required=True, choices=("approve", "reject"))
    p.add_argument("--reason", default="")
    p.add_argument("--actor", required=True,
                   help="who/what actually triggered this — never defaults to "
                        "'operator', so the audit trail stays honest")

    # No --yes, no --force, no env override: approval is a human act (§7.3).
    p = add("approve", "approve reviewed messages (TTY or --from-review only)",
            cmd_approve)
    p.add_argument("--batch")
    p.add_argument("--from-review", help="path to the edited review file")

    p = add("stage", "stage APPROVED messages as drafts/cards (never sends)", cmd_stage)
    p.add_argument("--batch", required=True)
    p.add_argument("--channel",
                   choices=("email", "linkedin", "whatsapp", "messenger", "sms"))
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    # channels/base.recent_person_touch tells the operator to run this; before it
    # existed there was no release path out of the 90-day cooldown short of
    # editing source. Named person keys only — never a blanket override.
    p.add_argument("--allow-person", action="append", metavar="PERSON_KEY",
                   help="release ONE person_key from the 90-day cooldown "
                        "(repeatable); everything else stays held")

    p = add("reconcile", "detect real sends and bounces from Gmail (read-only)",
            cmd_reconcile)
    p.add_argument("--channel",
                   choices=("email", "linkedin", "whatsapp", "messenger"),
                   default="email")

    p = add("mark-sent", "record a manual send (STAGED -> SENT, evidence required)",
            cmd_mark_sent)
    p.add_argument("message_id", type=int)
    p.add_argument("--evidence", required=True)
    p.add_argument("--actor", default="operator")

    p = add("scan-replies", "scan Gmail for replies and classify them (read-only)",
            cmd_scan_replies)
    p.add_argument("--days", type=int, default=30)

    p = add("ingest-triage", "ingest a dashboard-reply-triage output file",
            cmd_ingest_triage)
    p.add_argument("path")

    p = add("record-reply", "record a reply by hand (LinkedIn, or a correction)",
            cmd_record_reply)
    p.add_argument("prospect_id", type=int)
    p.add_argument("--channel",
                   choices=("email", "linkedin", "instagram", "whatsapp", "messenger",
                            "sms"),
                   required=True)
    p.add_argument("--class", dest="classification", required=True,
                   choices=("hot", "question", "objection", "no", "unsure",
                            "bounce", "auto_reply", "vacation"))
    p.add_argument("--snippet", default="")
    p.add_argument("--actor", default="operator")

    p = add("log-reply", "record a reply without a TTY (DASHBOARD review lane — same "
                         "write as `record-reply`, different proof of human)",
            cmd_log_reply)
    p.add_argument("prospect_id", type=int)
    p.add_argument("--channel",
                   choices=("email", "linkedin", "instagram", "whatsapp", "messenger",
                            "sms"),
                   required=True)
    p.add_argument("--class", dest="classification", required=True,
                   choices=("hot", "question", "objection", "no", "unsure",
                            "bounce", "auto_reply", "vacation"))
    p.add_argument("--snippet", default="")
    p.add_argument("--actor", required=True,
                   help="who/what actually recorded this — never defaults to "
                        "'operator', so the reply evidence stays auditable")

    p = add("attrib", "match a GHL disk export against messaged prospects (§8.2)",
            cmd_attrib)
    p.add_argument("--ghl-export", required=True)

    p = add("book", "record a booking with its attribution method (SC-7)", cmd_book)
    p.add_argument("prospect_id", type=int)
    p.add_argument("--method", required=True,
                   choices=("utm", "domain_match", "name_phone_match", "manual_confirm"))
    p.add_argument("--evidence", required=True)
    p.add_argument("--outcome", choices=("demo_booked", "call_booked"),
                   default="demo_booked")
    p.add_argument("--actor", default="operator")

    p = add("sheet", "the measurement sheet: variant | sent_to | replied | booked",
            cmd_sheet)
    p.add_argument("--full", action="store_true", help="add the context columns")
    p.add_argument("--csv", help="write to this path instead of stdout")

    p = add("stats", "per-variant counts, gated honestly (§4.4)", cmd_stats)
    p.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    p.add_argument("--replies", action="store_true",
                   help="print verbatim reply snippets — the real signal at low n")
    p.add_argument("--by-family", action="store_true")

    add("metrics-emit", "write the DASHBOARD metrics snapshot (§8.4)", cmd_metrics_emit)

    p = add("suppress", "add an address to the suppression list", cmd_suppress)
    p.add_argument("address")
    p.add_argument("--channel",
                   choices=("email", "linkedin", "instagram", "whatsapp", "messenger",
                            "sms", "all"),
                   default="email")
    p.add_argument("--scope", choices=("address", "domain", "person"), default="address")
    p.add_argument("--reason", required=True,
                   choices=("hard_no", "unsubscribe", "bounce", "dead_mailbox",
                            "legal", "manual"))

    add("status", "pipeline snapshot + honesty warnings", cmd_status)

    p = add("cancel", "cancel a message", cmd_cancel)
    p.add_argument("message_id", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="operator")

    p = add("drop-from-batch", "drop a prospect from a batch (no silent truncation)",
            cmd_drop_from_batch)
    p.add_argument("--batch", required=True)
    p.add_argument("--prospect", dest="prospect_id", type=int, required=True)
    p.add_argument("--actor", default="operator")

    p = add("variants", "retire a variant / declare a winner (min-n gated, §4.4)",
            cmd_variants)
    p.add_argument("action", choices=("retire", "declare-winner"))
    p.add_argument("variant_id")
    p.add_argument("--force", action="store_true",
                   help="override the min-n gate; writes a permanent state_log row")
    p.add_argument("--reason", help="required with --force")
    p.add_argument("--actor", default="operator")

    return parser


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "variants" and args.force and not args.reason:
        print("dmctl: --force requires --reason (it writes a permanent audit row)",
              file=sys.stderr)
        return EXIT_REFUSED

    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        print(f"dmctl: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        return args.func(args, cfg)
    except (Refused, report.ReportError) as exc:
        print(f"dmctl: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Unwired as exc:
        print(f"dmctl: NOT WIRED YET — {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        if _GATE_RE.search(str(exc)):
            print(f"dmctl: gate abort: {exc}", file=sys.stderr)
            return EXIT_GATE_ABORT
        print(f"dmctl: database error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL
    except TimeoutError as exc:
        print(f"dmctl: refused: another run holds the lock ({exc})", file=sys.stderr)
        return EXIT_REFUSED
    except FileNotFoundError as exc:
        print(f"dmctl: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — exit codes are the contract, not tracebacks
        # A refusal from the state machine is the same gate the triggers enforce,
        # just caught one layer earlier. Matched by name so this module never
        # imports state.py at load time.
        if type(exc).__name__ in _GATE_EXCEPTIONS:
            print(f"dmctl: gate abort: {exc}", file=sys.stderr)
            return EXIT_GATE_ABORT
        if _is_auth_error(exc):
            print(f"dmctl: auth failure: {exc}", file=sys.stderr)
            _note_pending_action(
                cfg, "dm-engine: re-authorize personal Gmail — "
                     "python ~/.config/skills/gmail-helper/scripts/gmail_multi_auth.py "
                     "--account operator_personal (headless runs cannot re-auth)")
            return EXIT_AUTH
        print(f"dmctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
