"""Message/prospect/batch state machine + the append-only transition log.

Contract: PLAN.md §12.5. State diagram: §7.2.

Two layers guard every state change and BOTH must hold:

  1. this module — the readable first line of defense. Every legal edge is
     enumerated below by hand; anything not in the table raises
     `IllegalTransition` with a message a human can act on.
  2. the G-triggers in `data/migrations/002_triggers.sql` — the SQL backstop that
     also stops a raw `sqlite3` write.

The Python table is NOT redundant: an opaque `sqlite3.IntegrityError: G3: ...`
is a worse operator experience than "READY -> STAGED is not a legal transition
(approval was skipped)". Never delete a check here because a trigger covers it.

Transactions: `transition()` joins the caller's transaction when one is open
(`conn.in_transaction`) and commits only the transaction it started itself. That
is what lets `channels/*.stage()` run the cap count, the `stage_events` insert
and the state change inside ONE `BEGIN IMMEDIATE` (PLAN.md §10 row (c)).
"""

import sqlite3

__all__ = [
    "IllegalTransition",
    "MESSAGE_TRANSITIONS",
    "PROSPECT_TRANSITIONS",
    "BATCH_TRANSITIONS",
    "TRANSITIONS",
    "current_state",
    "legal_targets",
    "transition",
    "note",
]


class IllegalTransition(Exception):
    """A state change that the machine refuses. Not a bug — a gate doing its job."""


# --------------------------------------------------------------------------- #
# the legal edge set — enumerated, never inferred
# --------------------------------------------------------------------------- #
#
# COMPOSED --lint--> READY --approve--> APPROVED --stage--> STAGED --send--> SENT
#
# Deliberately absent (each one is a real bypass, tested in tests/test_state.py):
#   READY    -> STAGED     skips the human approval gate
#   COMPOSED -> APPROVED   approves un-linted copy
#   APPROVED -> SENT       claims a send the engine never made
#   STAGED   -> APPROVED   un-stages a draft that already exists in Gmail
#   SENT     -> anything upstream (rewriting history)
#   REJECTED -> anything   a rejection is final; recompose into a new message
#
MESSAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "COMPOSED":    frozenset({"READY", "LINT_FAILED", "CANCELLED"}),
    "LINT_FAILED": frozenset({"COMPOSED", "CANCELLED"}),   # re-ingest with a new body
    "READY":       frozenset({"APPROVED", "REJECTED", "CANCELLED"}),
    "APPROVED":    frozenset({"STAGED", "CANCELLED"}),     # cancel = un-approve pre-staging
    "STAGED":      frozenset({"SENT", "CANCELLED"}),       # cancel = draft discarded (edge 26)
    "SENT":        frozenset({"REPLIED", "BOUNCED", "CLOSED"}),
    "REPLIED":     frozenset({"CLOSED"}),
    "BOUNCED":     frozenset({"CLOSED"}),
    "REJECTED":    frozenset(),
    "CANCELLED":   frozenset(),
    "CLOSED":      frozenset(),
}

PROSPECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW":        frozenset({"QUALIFIED", "EXCLUDED", "SUPPRESSED"}),
    "QUALIFIED":  frozenset({"EXCLUDED", "SUPPRESSED"}),
    # Suppression is permanent by design (PLAN.md §9.34) — no path back to QUALIFIED.
    "SUPPRESSED": frozenset({"EXCLUDED"}),
    "EXCLUDED":   frozenset(),
}

BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN":     frozenset({"COMPOSED", "ABORTED"}),
    "COMPOSED": frozenset({"REVIEWED", "ABORTED"}),
    "REVIEWED": frozenset({"STAGED", "COMPOSED", "ABORTED"}),   # back to COMPOSED to recompose
    "STAGED":   frozenset({"CLOSED", "ABORTED"}),
    "CLOSED":   frozenset(),
    "ABORTED":  frozenset(),
}

TRANSITIONS = {
    "message": MESSAGE_TRANSITIONS,
    "prospect": PROSPECT_TRANSITIONS,
    "batch": BATCH_TRANSITIONS,
}

#: entity -> (table, has an updated_at column)
_TABLES = {
    "message": ("messages", True),
    "prospect": ("prospects", True),
    "batch": ("batches", False),        # batches carry created_at only
}

#: message states that stamp a timestamp column as a side effect of arriving.
_MESSAGE_STAMPS = {
    "APPROVED": "approved_at",
    "STAGED": "staged_at",
    "SENT": "sent_at",
}


def _table(entity: str) -> tuple[str, bool]:
    try:
        return _TABLES[entity]
    except KeyError:
        raise ValueError(
            f"unknown entity {entity!r}; expected one of {sorted(_TABLES)}"
        ) from None


def current_state(conn: sqlite3.Connection, entity: str, entity_id) -> str:
    """Current state of an entity. Raises LookupError when the row is missing."""
    table, _ = _table(entity)
    row = conn.execute(f"SELECT state FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise LookupError(f"{entity} {entity_id!r} does not exist")
    return row[0]


def legal_targets(entity: str, from_state: str) -> frozenset[str]:
    """Every state reachable in one legal step from `from_state`."""
    _table(entity)                      # validates the entity name
    table = TRANSITIONS[entity]
    if from_state not in table:
        raise IllegalTransition(f"{from_state!r} is not a known {entity} state")
    return table[from_state]


def transition(
    conn: sqlite3.Connection,
    entity: str,
    entity_id,
    to_state: str,
    actor: str,
    reason: str | None = None,
    evidence: str | None = None,
) -> None:
    """Move an entity to `to_state`, or refuse loudly.

    Writes one `state_log` row per accepted transition (append-only, G10).
    `actor` is 'operator' | 'engine' | 'agent:<name>' and is required — an
    unattributed state change is not auditable.

    SENT additionally requires `evidence`; it becomes `messages.sent_evidence`,
    which trigger G4 also demands. The engine does not send, so the evidence is
    always an observation of someone else's send (a Gmail Sent scan, or the operator
    confirming a manual LinkedIn DM).
    """
    table_name, has_updated_at = _table(entity)
    table = TRANSITIONS[entity]

    if not actor:
        raise ValueError("actor is required on every transition")
    if to_state not in table:
        raise IllegalTransition(
            f"{to_state!r} is not a known {entity} state; "
            f"legal states: {sorted(table)}"
        )

    from_state = current_state(conn, entity, entity_id)
    allowed = table[from_state]
    if to_state not in allowed:
        raise IllegalTransition(
            f"{entity} {entity_id}: {from_state} -> {to_state} is not a legal "
            f"transition. From {from_state} you may go to "
            f"{sorted(allowed) or 'nowhere (terminal state)'}."
        )
    if entity == "message" and to_state == "SENT" and not evidence:
        raise IllegalTransition(
            f"message {entity_id}: SENT requires evidence "
            "(e.g. 'gmail_sent_scan:<msgid>' or 'manual_confirm:<note>')"
        )

    sets = ["state = ?"]
    params: list = [to_state]
    if has_updated_at:
        sets.append("updated_at = datetime('now')")
    if entity == "message":
        stamp = _MESSAGE_STAMPS.get(to_state)
        if stamp:
            sets.append(f"{stamp} = datetime('now')")
        if to_state == "SENT":
            sets.append("sent_evidence = ?")
            params.append(evidence)
    params.append(entity_id)

    started_here = not conn.in_transaction
    try:
        conn.execute(f"UPDATE {table_name} SET {', '.join(sets)} WHERE id = ?", params)
        _log(conn, entity, entity_id, from_state, to_state, actor, reason, evidence)
    except Exception:
        if started_here:
            conn.rollback()
        raise
    if started_here:
        conn.commit()


def note(
    conn: sqlite3.Connection,
    entity: str,
    entity_id,
    actor: str,
    reason: str,
    evidence: str | None = None,
) -> None:
    """Append an audit row without changing state.

    Used for refusals worth remembering — a cap skip, a suppression hit, a
    'VIOLATION: ...' note (SC-6 counts those). `from_state == to_state` marks the
    row as a note rather than a move.
    """
    state = current_state(conn, entity, entity_id)
    started_here = not conn.in_transaction
    try:
        _log(conn, entity, entity_id, state, state, actor, reason, evidence)
    except Exception:
        if started_here:
            conn.rollback()
        raise
    if started_here:
        conn.commit()


def _log(conn, entity, entity_id, from_state, to_state, actor, reason, evidence) -> None:
    conn.execute(
        "INSERT INTO state_log (entity, entity_id, from_state, to_state, actor, reason, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity, str(entity_id), from_state, to_state, actor, reason, evidence),
    )
