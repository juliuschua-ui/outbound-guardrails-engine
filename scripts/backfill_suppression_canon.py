#!/usr/bin/env python3
"""One-time backfill: store every suppression row CANONICAL, as `add()` now does.

WHY
    `sync_from_file()` and `sync()` used to insert the raw line from the suppression
    file. Every SQL-side matcher — trigger G8 and `channels.base._SUPPRESSION_MATCH` —
    canonicalizes the CONTACT and compares it against `suppression.address` verbatim, so
    a stored row carrying a '+tag' or gmail dots never matched the bare contact at the
    stage gate. That gate is the one that catches a message which is already APPROVED.
    The writers are fixed; rows written before that fix are still raw.

SAFETY
    Dry run by default: prints what it WOULD change, writes nothing. Pass --apply to
    commit. Take a backup first — this touches real prospect data:
        cp data/dm.db data/dm.db.bak-$(date +%Y%m%d)

    A row is only ever REWRITTEN to its canonical form. Where that canonical form
    already exists as its own row the raw duplicate is dropped instead, because the two
    name the same destination and the table is UNIQUE(channel, address). Nothing is ever
    un-suppressed: every address suppressed before the run is still suppressed after it.

USAGE
    ./.venv/bin/python scripts/backfill_suppression_canon.py            # dry run
    ./.venv/bin/python scripts/backfill_suppression_canon.py --apply
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dm_engine.config import Config                  # noqa: E402
from dm_engine.suppression import canonical_address  # noqa: E402


def plan(conn):
    """(rewrite, collide, already_canonical) without writing anything."""
    rows = conn.execute(
        "SELECT id, channel, address, scope FROM suppression ORDER BY id"
    ).fetchall()
    existing = {(r["channel"], r["address"]) for r in rows}
    rewrite, collide, clean = [], [], 0
    for r in rows:
        if r["scope"] == "domain":        # domains are stored as written, by design
            clean += 1
            continue
        try:
            canon = canonical_address(r["channel"], r["address"])
        except Exception as e:            # noqa: BLE001 — one bad row never aborts the sweep
            print(f"  SKIP id={r['id']} {r['channel']}/{r['address']!r}: {e}", file=sys.stderr)
            continue
        if not canon or canon == r["address"]:
            clean += 1
            continue
        (collide if (r["channel"], canon) in existing else rewrite).append((r, canon))
    return rewrite, collide, clean


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--db", default=None,
                    help="database path (default: the configured one)")
    args = ap.parse_args()

    db_path = args.db or Config().db_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rewrite, collide, clean = plan(conn)

    total = len(rewrite) + len(collide) + clean
    print(f"database: {db_path}")
    print(f"  {total} suppression rows, {clean} already canonical")
    print(f"  {len(rewrite)} to rewrite, {len(collide)} raw duplicates to drop")
    for r, canon in rewrite[:20]:
        print(f"    rewrite id={r['id']:<5} {r['channel']:<9} {r['address']!r} -> {canon!r}")
    for r, _canon in collide[:20]:
        print(f"    drop    id={r['id']:<5} {r['channel']:<9} {r['address']!r} "
              f"(its canonical row already exists)")
    if len(rewrite) + len(collide) > 40:
        print("    ... (truncated)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0
    if not (rewrite or collide):
        print("\nnothing to do.")
        return 0

    with conn:
        for r, canon in rewrite:
            conn.execute("UPDATE suppression SET address = ? WHERE id = ?", (canon, r["id"]))
        for r, _canon in collide:
            conn.execute("DELETE FROM suppression WHERE id = ?", (r["id"],))
    print(f"\nAPPLIED: {len(rewrite)} rewritten, {len(collide)} duplicates dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
