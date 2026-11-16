"""Pre-order capture (memos/pre-order-ingestion).

Three facts live on `owned_games` and are exposed by `catalog_games` /
`catalog_views` for pshelf:

  preorder_flag        Sony's own `preorderFlag` on the PSN entitlement — the
                       authoritative "this purchase was a pre-order" signal.
  preorder_release_ts  the STORE's expected release date, epoch SECONDS. Kept
                       separate from IGDB's `release_ts` (which stays on the
                       views as-is): if PlayStation and IGDB disagree, both
                       survive. NULL when the store date is unknown.
  is_preorder          MATERIALIZED here (never in a view): purchased, dated,
                       and acquired before the expected release.

Why materialized: pshelf caches on the SQLite file's mtime, so a view that
computed `now()` would freeze its answer. pshelf derives "still unreleased"
itself from the dates; a stale `is_preorder` can therefore never strand a game.

The expected release date is resolved from `preorder_release_dates` (product id
-> epoch seconds), which is filled by whatever store source is available and can
always be set by hand through `POST /manual/preorder-release`. It is deliberately
NEVER written from IGDB: the contract keeps the two dates distinguishable. IGDB's
date is used only as the comparison fallback when materializing the flag, and
only for rows Sony has already flagged as pre-orders.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

# Fields the PSN entitlement payload *might* carry a release date in on some
# account/region. Verified live 2026-11-16 on this account: none of these are
# present (gameMeta/titleMeta/conceptMeta expose name/type/icon/packageType
# only), so the store lookup/override is the real source — this list is a
# defensive hint, never required.
_RELEASE_HINT_FIELDS = ("releaseDate", "release_date", "releaseDateTime", "releaseTs")


def to_epoch_seconds(value: Any) -> int | None:
    """Parse an ISO-8601 string ('2026-11-18T00:00:00Z') or epoch number."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text)  # 3.11+ accepts a trailing 'Z'
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def set_release_date(
    conn: sqlite3.Connection, product_id: str, release_ts: int, source: str = "manual"
) -> None:
    """Record the store's expected release date for a product id."""
    conn.execute(
        """INSERT INTO preorder_release_dates(product_id, release_ts, source, updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(product_id) DO UPDATE SET
             release_ts = excluded.release_ts,
             source = excluded.source,
             updated_at = datetime('now')""",
        (product_id, int(release_ts), source),
    )
    conn.commit()


def release_ts_for(conn: sqlite3.Connection, product_id: str | None) -> int | None:
    if not product_id:
        return None
    row = conn.execute(
        "SELECT release_ts FROM preorder_release_dates WHERE product_id = ?", (product_id,)
    ).fetchone()
    return int(row["release_ts"]) if row else None


def apply_store_release_dates(conn: sqlite3.Connection) -> int:
    """Copy known store dates onto owned rows (by PSN content/product id).

    Idempotent; only touches rows whose value actually differs. Returns the
    number of rows updated."""
    cur = conn.execute(
        """UPDATE owned_games
              SET preorder_release_ts = (
                      SELECT r.release_ts FROM preorder_release_dates r
                       WHERE r.product_id = owned_games.psn_content_id),
                  updated_at = datetime('now')
            WHERE psn_content_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM preorder_release_dates r
                   WHERE r.product_id = owned_games.psn_content_id
                     AND r.release_ts IS NOT owned_games.preorder_release_ts)"""
    )
    conn.commit()
    return cur.rowcount


def refresh_preorder_flags(conn: sqlite3.Connection) -> dict[str, Any]:
    """Materialize `owned_games.is_preorder` from the stored facts.

    Rule (conservative, and deliberately NOT time-dependent):

        is_preorder = 1  iff  is_owned = 1
                         and  ownership_class = 'purchased'   (never PS+ claims)
                         and  acquisition_date is set (the order moment)
                         and  (preorder_release_ts IS NOT NULL OR preorder_flag = 1)
                         and  acquisition_date < COALESCE(preorder_release_ts,
                                                          IGDB first_release_date)

    The `preorder_flag` interlock means a wrong/stale IGDB date on an old game
    can never invent a pre-order; IGDB's date only ever *confirms* a pre-order
    Sony already declared. PS+ claimed/extra rows are excluded outright.
    """
    before = conn.execute("SELECT COUNT(*) AS n FROM owned_games WHERE is_preorder = 1").fetchone()["n"]
    conn.execute(
        """UPDATE owned_games
              SET is_preorder = CASE WHEN
                      is_owned = 1
                      AND ownership_class = 'purchased'
                      AND acquisition_date IS NOT NULL
                      AND acquisition_date != ''
                      AND (preorder_release_ts IS NOT NULL OR COALESCE(preorder_flag, 0) = 1)
                      AND julianday(acquisition_date) < julianday(
                              COALESCE(
                                  preorder_release_ts,
                                  (SELECT CAST(json_extract(m.payload, '$.first_release_date') AS INTEGER)
                                     FROM game_metadata m
                                    WHERE m.igdb_id = owned_games.igdb_id)),
                              'unixepoch')
                  THEN 1 ELSE 0 END
            WHERE is_owned = 1"""
    )
    conn.commit()
    rows = conn.execute(
        "SELECT id, title, acquisition_date, preorder_release_ts, preorder_flag"
        "  FROM owned_games WHERE is_preorder = 1 ORDER BY acquisition_date DESC"
    ).fetchall()
    return {"before": before, "after": len(rows), "preorders": [dict(r) for r in rows]}


def entitlement_preorder_values(item: dict[str, Any]) -> dict[str, Any]:
    """Pre-order fields for an owned_games row from a PSN entitlement.

    - `preorder_flag`      <- the entitlement's `preorderFlag`
    - `acquisition_date`   <- `activeDate` **for pre-orders only**: Sony creates
      the entitlement when the pre-order is placed, so activeDate is the order
      moment (the date the contract says must never be rewritten at release).
      Non-pre-orders keep acquisition_date NULL, exactly as before — the
      purchase-date backfill owns those, and rewriting 1000+ rows from activeDate
      would change the existing purchase-date story.
    - `preorder_release_ts` <- a release-date hint if the payload ever carries
      one (none do on this account, verified 2026-11-16); else omitted so the
      store lookup/override fills it.
    """
    is_preorder = bool(item.get("preorderFlag") or item.get("preorderPlaceholderFlag"))
    out: dict[str, Any] = {"preorder_flag": 1 if is_preorder else 0}
    if not is_preorder:
        return out
    active = item.get("activeDate")
    if active:
        out["acquisition_date"] = str(active)
    for section in ("gameMeta", "titleMeta", "conceptMeta", item):
        if not isinstance(section, dict):
            continue
        for field in _RELEASE_HINT_FIELDS:
            ts = to_epoch_seconds(section.get(field))
            if ts:
                out["preorder_release_ts"] = ts
                return out
    return out
