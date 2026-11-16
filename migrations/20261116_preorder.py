#!/usr/bin/env python3
"""Migration: pre-order capture (is_preorder / preorder_release_ts / acquisition_date).

Applies, idempotently, the schema half of memos/pre-order-ingestion:

  owned_games  + preorder_flag, preorder_release_ts, is_preorder
  games        + is_preorder, preorder_release_ts, acquisition_date
  (new)          preorder_release_dates  (product_id -> store release date)
  catalog_views, catalog_games  recreated so both expose
                 is_preorder, preorder_release_ts (+ acquisition_date)

The column adds are guarded with PRAGMA table_info, so re-running is a no-op.
The views are DROP + CREATE (same text the app's init_db executes), which is
also idempotent — but NOTE: a *pre-migration* app process recreates the views
WITHOUT the new columns on its next init_db, so this DDL is only durable once
the new image is deployed. That is not theoretical: the pre-change
`manual_api._conn()` calls `init_db()` on EVERY request, so while the old
image is running a single `GET /manual/psn-credential` (or `/manual/needs-match`,
which the pshelf UI polls) silently drops the new columns again. Re-run this
script any time to restore them; the base-table columns and
preorder_release_dates survive (init_db only ever ADDs columns).

The materialized `is_preorder` values are NOT written here: run with
--refresh to call refresh_preorder_flags() afterwards (or let the next
psn_sync / catalog_games rebuild do it).

Usage (stdlib only; repo must be importable, e.g. PYTHONPATH=<repo>/src):

    python3 migrations/20261116_preorder.py --db /srv/docker/game-catalog/data/mailroom.db
    python3 migrations/20261116_preorder.py --db ... --refresh

Exit code 0 = applied (or already applied), 1 = verification failed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from mailroom.db import SCHEMA, _add_column_if_missing

COLUMNS = [
    ("owned_games", "preorder_flag", "preorder_flag INTEGER DEFAULT 0"),
    ("owned_games", "preorder_release_ts", "preorder_release_ts INTEGER"),
    ("owned_games", "is_preorder", "is_preorder INTEGER DEFAULT 0"),
    ("games", "is_preorder", "is_preorder INTEGER DEFAULT 0"),
    ("games", "preorder_release_ts", "preorder_release_ts INTEGER"),
    ("games", "acquisition_date", "acquisition_date TEXT"),
]

EXPECTED_VIEW_COLUMNS = ("is_preorder", "preorder_release_ts")


def _cols(conn: sqlite3.Connection, obj: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({obj})")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection, refresh: bool = False) -> int:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    before = {
        "catalog_games": conn.execute("SELECT count(*) FROM catalog_games").fetchone()[0],
        "catalog_views": conn.execute("SELECT count(*) FROM catalog_views").fetchone()[0],
        "games": conn.execute("SELECT count(*) FROM games").fetchone()[0],
    }
    print(f"before: {before}")

    for table, col, ddl in COLUMNS:
        if not _has_table(conn, table):
            print(f"skip {table}.{col}: table absent (fresh DB — SCHEMA will create it)")
            continue
        had = col in _cols(conn, table)
        _add_column_if_missing(conn, table, col, ddl)
        print(f"{'noop' if had else 'added'} {table}.{col}")
    conn.commit()

    # Views + preorder_release_dates. Full SCHEMA is the app's own idempotent
    # script (CREATE TABLE IF NOT EXISTS / DROP VIEW + CREATE VIEW).
    conn.executescript(SCHEMA)
    conn.commit()

    failures = []
    for table in ("owned_games", "games"):
        cols = _cols(conn, table)
        want = {c for t, c, _ in COLUMNS if t == table}
        missing = want - cols
        print(f"{table} columns: {sorted(want)} -> {'OK' if not missing else 'MISSING ' + str(sorted(missing))}")
        if missing:
            failures.append(f"{table} missing {sorted(missing)}")

    for view in ("catalog_games", "catalog_views"):
        cols = _cols(conn, view)
        missing = [c for c in EXPECTED_VIEW_COLUMNS if c not in cols]
        print(f"{view}: {len(cols)} cols, is_preorder={'is_preorder' in cols}, "
              f"preorder_release_ts={'preorder_release_ts' in cols}, "
              f"acquisition_date={'acquisition_date' in cols}"
              f"{'' if not missing else '  MISSING ' + str(missing)}")
        if missing:
            failures.append(f"{view} missing {missing}")

    if not _has_table(conn, "preorder_release_dates"):
        failures.append("preorder_release_dates table absent")
    else:
        print("preorder_release_dates: present")

    after = {
        "catalog_games": conn.execute("SELECT count(*) FROM catalog_games").fetchone()[0],
        "catalog_views": conn.execute("SELECT count(*) FROM catalog_views").fetchone()[0],
        "games": conn.execute("SELECT count(*) FROM games").fetchone()[0],
    }
    print(f"after:  {after}")
    if before != after:
        failures.append(f"row counts changed: {before} -> {after}")

    if refresh:
        from mailroom.verticals.game_catalog.preorder import refresh_preorder_flags

        res = refresh_preorder_flags(conn)
        conn.commit()
        print(f"refresh_preorder_flags: {res['before']} -> {res['after']} flagged")
        for row in res["preorders"]:
            print("  ", dict(row))
        print("flagged:",
              conn.execute("SELECT count(*) FROM owned_games WHERE is_preorder = 1").fetchone()[0],
              "owned /",
              conn.execute("SELECT count(*) FROM catalog_games WHERE is_preorder = 1").fetchone()[0],
              "catalog_games")

    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    print("migration OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--refresh", action="store_true",
                    help="run refresh_preorder_flags after DDL")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        return migrate(conn, refresh=args.refresh)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
