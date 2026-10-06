"""The sheet, the CSV, the DASHBOARD emits, and honest presentation of the numbers.

Contract: PLAN.md §12.6, §3.4, §4.4, §8.4, §10 row (h).

This module is where the system either tells the operator the truth or quietly lies to
him, so three rules are structural rather than aspirational:

1. **One denominator, defined once.** `population_counts()` implements PLAN.md §4.4
   verbatim: sends = step-1 messages in SENT/REPLIED/BOUNCED/CLOSED; delivered =
   sends − bounced; a reply counts only when `replies.is_human = 1`. This module
   performs **no division at all** — every rate goes through `stats.rate()`, which is
   the engine's single arithmetic source (§10 row (h)). `tests/adversarial/test_gates.py`
   asserts the absence of a division operator in this file.
2. **No rate without its n.** `_pct` returns None for n = 0 (the key is omitted from
   the metrics emit rather than written as 0), and `render_stats` prints every rate as
   `X.X% (n=N)`.
3. **No winner below the gate.** Below `min_n_gate` delivered per variant, or fewer
   than 5 human replies in the experiment, `render_stats` prints raw counts and the
   §4.4 banner — no rates, no `leading`, and the table is ALWAYS ordered by variant id
   so nothing about the layout implies a ranking.

Nothing here computes a revenue or pipeline-value number: every GHL opportunity has
`monetaryValue: 0` (§8.3), so such a metric would be garbage dressed as insight.
"""

from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, stats

#: The four columns the outbound-strategy report demanded (PLAN.md §1.2 SC-2, §3.4).
SHEET_COLUMNS = ("variant", "sent_to", "replied", "booked")
#: Context columns the view also exposes; `--full` adds these AFTER the contract four.
SHEET_CONTEXT_COLUMNS = ("company_name", "market", "channel", "sent_at", "bounced")

#: PLAN.md §4.4: sends are step-1 attempts that actually left the building.
SENT_STATES = ("SENT", "REPLIED", "BOUNCED", "CLOSED")
BOOKED_OUTCOMES = ("demo_booked", "call_booked")

DEFAULT_MIN_N_GATE = stats.MIN_N_DEFAULT
#: PLAN.md §4.4 — a winner needs BOTH per-variant n and this many human replies.
MIN_HUMAN_REPLIES_GATE = stats.MIN_EXPERIMENT_REPLIES

LOW_N_BANNER = stats.BANNER_LOW_N


class ReportError(RuntimeError):
    """A report could not be produced from the data at hand."""


def assert_sandboxed(cfg, path: Path) -> Path:
    """Under DM_ENGINE_TEST=1, refuse to write outside the repo or a temp dir.

    PLAN.md §10 row (n) requires "no test touches real DASHBOARD" to be mechanical.
    A test that forgets to redirect `dashboard_metrics_dir` would otherwise drop a
    fabricated snapshot into the operator's live dashboard directory, where it is
    indistinguishable from a real pull. This turns that into a loud failure.
    """
    if not getattr(cfg, "test_mode", False):
        return path
    resolved = Path(path).resolve()
    allowed = [Path(config.REPO_ROOT).resolve(), Path(tempfile.gettempdir()).resolve()]
    for var in ("TMPDIR", "PYTEST_DEBUG_TEMPROOT"):
        import os  # noqa: PLC0415 — only needed on this guarded path
        value = os.environ.get(var)
        if value:
            allowed.append(Path(value).resolve())
    if any(resolved.is_relative_to(base) for base in allowed):
        return path
    raise ReportError(
        f"test mode: refusing to write {resolved} — it is outside the repo and the "
        "temp dir. Build the Config from the `engine_cfg` fixture so "
        "dashboard_metrics_dir / dashboard_approvals_dir point into tmp_path.")


def _pct(numerator: int, denominator: int) -> float | None:
    """Percent, delegated to `stats.rate` — this module never divides (§10 row (h))."""
    fraction = stats.rate(numerator, denominator)
    if fraction is None:
        return None
    return round(fraction * 100, 1)


# --------------------------------------------------------------------------- #
# the sheet (SC-2)
# --------------------------------------------------------------------------- #

def sheet_rows(conn, full: bool = False) -> list[dict]:
    """The measurement sheet, one query against the §3.4 view. Contract: §12.6.

    Ordered chronologically by send time — deliberately NOT by any performance
    column, so reading the sheet never implies a ranking.
    """
    columns = list(SHEET_COLUMNS) + (list(SHEET_CONTEXT_COLUMNS) if full else [])
    sql = (f"SELECT {', '.join(columns)} FROM sheet "
           "ORDER BY sent_at IS NULL, sent_at, variant, sent_to")
    return [{column: row[column] for column in columns} for row in conn.execute(sql)]


def write_csv(rows, path: Path) -> None:
    """Write sheet rows to CSV. Writes every row it was given or raises — never truncates."""
    rows = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else list(SHEET_COLUMNS)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            extra = set(row) - set(fieldnames)
            if extra:
                raise ReportError(
                    f"refusing to drop columns {sorted(extra)} from the CSV export")
            writer.writerow(row)


# --------------------------------------------------------------------------- #
# population counts — the honest denominator (PLAN.md §4.4, §10 row (h))
# --------------------------------------------------------------------------- #

def population_counts(conn, days: int | None = None) -> dict:
    """Engine-wide counts using the §4.4 definitions.

    `sends`      step-1 messages in SENT/REPLIED/BOUNCED/CLOSED
    `bounced`    of those, state BOUNCED
    `delivered`  sends − bounced          <- the reply-rate denominator
    `replies`    DISTINCT prospects in the delivered population with is_human = 1
    `booked`     DISTINCT prospects in the delivered population with a booked outcome

    Restricting the numerators to the delivered population is what makes it
    arithmetically impossible for a rate to exceed 100%: an auto-responder, a bounce,
    an organic demo, or a reply from someone we never messaged cannot enter.
    """
    where = ["m.sequence_step = 1", f"m.state IN ({','.join('?' * len(SENT_STATES))})"]
    params: list = list(SENT_STATES)
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds")
        where.append("m.sent_at IS NOT NULL AND m.sent_at >= ?")
        params.append(cutoff)
    clause = " AND ".join(where)

    row = conn.execute(
        f"SELECT count(*) AS sends, "
        f"       sum(CASE WHEN m.state = 'BOUNCED' THEN 1 ELSE 0 END) AS bounced "
        f"FROM messages m WHERE {clause}", params).fetchone()
    sends = row["sends"] or 0
    bounced = row["bounced"] or 0

    delivered_clause = f"{clause} AND m.state != 'BOUNCED'"
    replies = conn.execute(
        f"SELECT count(DISTINCT m.prospect_id) FROM messages m "
        f"WHERE {delivered_clause} AND EXISTS ("
        f"  SELECT 1 FROM replies r WHERE r.prospect_id = m.prospect_id AND r.is_human = 1)",
        params).fetchone()[0] or 0
    booked = conn.execute(
        f"SELECT count(DISTINCT m.prospect_id) FROM messages m "
        f"JOIN outcomes o ON o.prospect_id = m.prospect_id "
        f"WHERE {delivered_clause} AND o.outcome IN "
        f"({','.join('?' * len(BOOKED_OUTCOMES))})",
        params + list(BOOKED_OUTCOMES)).fetchone()[0] or 0

    return {
        "sends": sends,
        "bounced": bounced,
        "delivered": sends - bounced,
        "replies": replies,
        "booked": booked,
    }


def reply_rate_pct(counts: dict) -> float | None:
    """Human replies over DELIVERED (not staged, not imported). None when delivered=0."""
    return _pct(counts["replies"], counts["delivered"])


def booking_rate_pct(counts: dict) -> float | None:
    """Bookings over DELIVERED — the same denominator, stated the same way."""
    return _pct(counts["booked"], counts["delivered"])


# --------------------------------------------------------------------------- #
# DASHBOARD emits
# --------------------------------------------------------------------------- #

def _stamped_path(directory: Path, prefix: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M")
    path = directory.joinpath(f"{prefix}-{stamp}.json")
    # "Never delete old files" (dashboard-metrics-pull contract) — and never silently
    # overwrite another producer's same-minute file either.
    # (`.joinpath` not `/`: the adversarial suite asserts the ONLY division operator
    #  in this module is the one inside `_pct`.)
    suffix = 2
    while path.exists():
        path = directory.joinpath(f"{prefix}-{stamp}-{suffix}.json")
        suffix += 1
    return path


def collect_metrics(conn) -> dict:
    """The `dm.*` metric block (PLAN.md §8.4). Plain numbers only; keys omitted, not zeroed."""
    total = population_counts(conn)
    recent = population_counts(conn, days=7)
    states = {row["state"]: row["n"] for row in conn.execute(
        "SELECT state, count(*) AS n FROM messages GROUP BY state")}
    suppressed = conn.execute("SELECT count(*) FROM suppression").fetchone()[0]

    metrics: dict[str, float] = {
        "dm.sends_total": total["sends"],
        "dm.sends_7d": recent["sends"],
        # delivered/bounced are emitted alongside the rate on purpose: a percentage
        # without its denominator is exactly the failure mode §4.4 forbids.
        "dm.delivered_total": total["delivered"],
        "dm.bounced_total": total["bounced"],
        "dm.replies_human_total": total["replies"],
        "dm.demos_booked_total": total["booked"],
        "dm.attempts_gap_to_20": max(0, 20 - total["sends"]),
        "dm.pending_approval": states.get("READY", 0),
        "dm.staged_unsent": states.get("STAGED", 0),
        "dm.suppressed_total": suppressed,
    }
    rate = reply_rate_pct(total)
    if rate is not None:
        metrics["dm.reply_rate_delivered_pct"] = rate
    return metrics


def emit_dashboard_metrics(conn, cfg) -> Path:
    """Write `pull-<YYYY-MM-DD-HHmm>.json` into `cfg.dashboard_metrics_dir`.

    Contract (dashboard-metrics-pull SKILL.md): `{"generatedAt": ISO, "metrics": {...}}`,
    plain numbers only, omit unavailable keys, never delete old files.
    """
    now = datetime.now(timezone.utc)
    assert_sandboxed(cfg, cfg.dashboard_metrics_dir)
    path = _stamped_path(cfg.dashboard_metrics_dir, "pull", now)
    payload = {
        "generatedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "metrics": collect_metrics(conn),
    }
    for key, value in payload["metrics"].items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ReportError(f"metric {key} is not a plain number: {value!r}")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_approvals_card(cfg, card_id: str, title: str, summary: str,
                         link: str) -> Path:
    """Append a DASHBOARD approvals item in the exec-card shape.

    Shape matches `~/work/dashboard/data/approvals/exec-ticket849.json`:
    `{"items": [{"id", "title", "summary", "link"}]}`. Cards produced in the same
    minute share one file (that is what the array is for); re-writing the same
    `card_id` replaces the item rather than duplicating it.
    """
    directory = Path(cfg.dashboard_approvals_dir)
    assert_sandboxed(cfg, directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = directory.joinpath(f"dmengine-{stamp}.json")

    items: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            items = [i for i in (existing.get("items") or []) if i.get("id") != card_id]
        except (json.JSONDecodeError, AttributeError):
            items = []
    items.append({"id": card_id, "title": title, "summary": summary, "link": link})
    path.write_text(json.dumps({"items": items}, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# honest presentation of Owner B's statistics (PLAN.md §4.4)
# --------------------------------------------------------------------------- #

def _field(row, name, default=None):
    """Read a field off a VariantStats dataclass, a dict, or a sqlite3.Row alike."""
    if isinstance(row, dict):
        return row.get(name, default)
    value = getattr(row, name, None)
    if value is None:
        try:
            return row[name]
        except (TypeError, KeyError, IndexError):
            return default
    return value


def summary_from_rows(rows, total_human_replies: int,
                      min_n_gate: int = DEFAULT_MIN_N_GATE,
                      experiment_id: str | None = None) -> dict:
    """Build a `stats.summary()`-shaped dict from bare §12.3 `variant_table` rows.

    Used when only the contract-level data is at hand. The gate applied here is
    deliberately STRICTER than `stats.report_tier` (every variant must clear the
    gate, not just the smallest arm with data) — erring toward "say less".
    """
    rows = list(rows)
    delivered = [int(_field(r, "delivered", 0) or 0) for r in rows
                 if int(_field(r, "sends", 0) or 0) > 0]
    enough_n = len(delivered) >= 2 and all(n >= min_n_gate for n in delivered)
    enough_replies = total_human_replies >= MIN_HUMAN_REPLIES_GATE
    gate_ok = enough_n and enough_replies
    leader = None
    if gate_ok:
        intervals = {}
        for row in rows:
            ci = _field(row, "ci")
            if ci and ci[0] is not None and int(_field(row, "delivered", 0) or 0) > 0:
                intervals[_field(row, "variant_id")] = (float(ci[0]), float(ci[1]))
        for variant, (lo, _hi) in intervals.items():
            others = [hi for other, (_lo, hi) in intervals.items() if other != variant]
            if others and all(lo > hi for hi in others):
                leader = variant
                break
    return {
        "experiment_id": experiment_id,
        "table": rows,
        "tier": {
            "tier": "wilson" if gate_ok else "raw",
            "show_rates": gate_ok,
            "winner_declarable": gate_ok,
            "min_delivered": min(delivered) if delivered else 0,
            "human_replies": total_human_replies,
            "required_delivered": min_n_gate,
            "required_replies": MIN_HUMAN_REPLIES_GATE,
            "banner": None if gate_ok else LOW_N_BANNER,
        },
        "leader": leader,
        "separation_note": None if leader else stats.NO_SEPARABLE_SIGNAL,
    }


def render_stats(summary: dict, replies_by_variant: dict | None = None) -> str:
    """Render `stats.summary()` honestly. Presentation only — no arithmetic, no gating.

    Rules enforced here and tested in tests/test_report.py:
      * rows are ALWAYS ordered by variant id — never by any performance column
      * below the gate: raw counts only, plus the §4.4 banner; no percentages at all
      * above the gate: every rate is printed with its n, and `leading` appears only
        for the variant `stats.leading_variant` cleared
    """
    tier = summary.get("tier") or {}
    rows = sorted(list(summary.get("table") or []),
                  key=lambda r: str(_field(r, "variant_id", "")))
    leading = {summary.get("leader")} - {None}
    show_rates = bool(tier.get("show_rates"))
    experiment_id = summary.get("experiment_id")
    min_n_gate = tier.get("required_delivered", DEFAULT_MIN_N_GATE)
    total_human_replies = tier.get("human_replies", 0)

    lines: list[str] = []
    if experiment_id:
        lines.append(f"experiment: {experiment_id}")
    header = f"{'variant':<10}{'sends':>7}{'delivered':>11}{'bounced':>9}" \
             f"{'replies':>9}{'booked':>8}"
    if show_rates:
        header += f"  {'reply rate':<22}{'booking rate':<22}{'flag':<10}"
    lines.append(header)
    lines.append("-" * len(header))

    for row in rows:
        variant = str(_field(row, "variant_id", "?"))
        sends = int(_field(row, "sends", 0) or 0)
        delivered = int(_field(row, "delivered", 0) or 0)
        bounced = int(_field(row, "bounced", 0) or 0)
        replies = int(_field(row, "replies", 0) or 0)
        booked = int(_field(row, "booked", 0) or 0)
        line = f"{variant:<10}{sends:>7}{delivered:>11}{bounced:>9}{replies:>9}{booked:>8}"
        if show_rates:
            reply_rate = _pct(replies, delivered)
            booking_rate = _pct(booked, delivered)
            ci = _field(row, "ci")
            reply_cell = "n/a" if reply_rate is None else f"{reply_rate}% (n={delivered})"
            if ci and ci[0] is not None:
                reply_cell += f" [{round(float(ci[0]) * 100, 1)}-{round(float(ci[1]) * 100, 1)}]"
            booking_cell = ("n/a" if booking_rate is None
                            else f"{booking_rate}% (n={delivered})")
            flag = "leading" if variant in leading else ""
            line += f"  {reply_cell:<22}{booking_cell:<22}{flag:<10}"
        lines.append(line)

    lines.append("")
    if not show_rates:
        lines.append(tier.get("banner") or LOW_N_BANNER)
        lines.append(
            f"gate unmet: smallest arm has {tier.get('min_delivered', 0)} delivered "
            f"(needs {min_n_gate}); {total_human_replies} human replies so far "
            f"(needs {tier.get('required_replies', MIN_HUMAN_REPLIES_GATE)})")
    elif not leading:
        lines.append(f"{stats.NO_SEPARABLE_SIGNAL} — the Wilson intervals overlap.")

    if replies_by_variant:
        lines.append("")
        lines.append("verbatim replies (the real signal at low n):")
        for variant in sorted(replies_by_variant):
            entries = replies_by_variant[variant] or []
            lines.append(f"  {variant} ({len(entries)}):")
            for entry in entries:
                snippet = _field(entry, "snippet", "") or ""
                classification = _field(entry, "classification", "") or ""
                lines.append(f"    [{classification}] {snippet}".rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def status_summary(conn, cfg=None) -> dict:
    """Pipeline snapshot plus the honesty warnings `dmctl status` prints."""
    counts = population_counts(conn)
    states = {row["state"]: row["n"] for row in conn.execute(
        "SELECT state, count(*) AS n FROM messages GROUP BY state")}
    prospect_states = {row["state"]: row["n"] for row in conn.execute(
        "SELECT state, count(*) AS n FROM prospects GROUP BY state")}
    reply_classes = {row["classification"]: row["n"] for row in conn.execute(
        "SELECT classification, count(*) AS n FROM replies GROUP BY classification")}

    warnings: list[str] = []
    unattributed = conn.execute(
        "SELECT count(*) FROM outcomes WHERE outcome IN (?, ?) AND attribution_method IS NULL",
        BOOKED_OUTCOMES).fetchone()[0]
    if unattributed:
        warnings.append(
            f"{unattributed} booking(s) have no attribution_method — SC-7 requires one "
            "per booking; run dmctl book <prospect> --method manual_confirm --evidence <id>")
    last_scan = conn.execute(
        "SELECT max(created_at) FROM replies").fetchone()[0]
    if counts["sends"] and not last_scan:
        warnings.append("no reply scan has ever run — dmctl scan-replies")
    if counts["sends"] < 20:
        warnings.append(
            f"SC-1 needs 20 step-1 attempts in SENT/REPLIED/BOUNCED/CLOSED; "
            f"{counts['sends']} recorded ({20 - counts['sends']} to go)")
    return {
        "counts": counts,
        "reply_rate_delivered_pct": reply_rate_pct(counts),
        "message_states": states,
        "prospect_states": prospect_states,
        "reply_classes": reply_classes,
        "last_reply_recorded_at": last_scan,
        "warnings": warnings,
    }


def render_status(summary: dict) -> str:
    counts = summary["counts"]
    rate = summary["reply_rate_delivered_pct"]
    lines = [
        f"sends (step 1)      {counts['sends']}",
        f"  bounced           {counts['bounced']}",
        f"  delivered         {counts['delivered']}   <- reply-rate denominator",
        f"human replies       {counts['replies']}",
        f"bookings            {counts['booked']}",
        ("reply rate          n/a (nothing delivered yet)" if rate is None
         else f"reply rate          {rate}% (n={counts['delivered']} delivered)"
              + ("" if counts["delivered"] >= DEFAULT_MIN_N_GATE
                 else "  — n too small to mean anything yet")),
        "",
        "message states      " + (", ".join(
            f"{k}={v}" for k, v in sorted(summary["message_states"].items())) or "none"),
        "reply classes       " + (", ".join(
            f"{k}={v}" for k, v in sorted(summary["reply_classes"].items())) or "none"),
    ]
    for warning in summary["warnings"]:
        lines.append(f"WARN  {warning}")
    return "\n".join(lines)


__all__ = [
    "BOOKED_OUTCOMES", "DEFAULT_MIN_N_GATE", "LOW_N_BANNER",
    "MIN_HUMAN_REPLIES_GATE", "ReportError", "SENT_STATES", "SHEET_COLUMNS",
    "SHEET_CONTEXT_COLUMNS", "booking_rate_pct", "collect_metrics",
    "emit_dashboard_metrics", "population_counts", "render_stats",
    "render_status", "reply_rate_pct", "sheet_rows", "status_summary",
    "summary_from_rows", "write_approvals_card", "write_csv",
]
