"""Pre-order capture tests (memos/pre-order-ingestion).

Contract the pshelf front-end codes against:
  is_preorder INTEGER 0/1  — materialized, no now() in either view
  preorder_release_ts INTEGER epoch SECONDS — the STORE's expected release
  acquisition_date — populated for pre-orders (the order moment)

Covered here: the materialization rule (purchased-before-release flags; a PS+
claim of the same unreleased title does not), the store release-date table,
the entitlement mapper, and the columns on both catalog views.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime

import pytest

from mailroom.clients import psn_library_item_to_game
from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog.preorder import (
    apply_store_release_dates,
    entitlement_preorder_values,
    refresh_preorder_flags,
    set_release_date,
    to_epoch_seconds,
)

RELEASE = "2026-11-18T00:00:00Z"
RELEASE_TS = 1794960000  # 2026-11-18T00:00:00Z
BOUGHT = "2026-11-15T18:04:11Z"


def _db():
    conn = connect(f"sqlite:///{tempfile.mkdtemp()}/p.db")
    init_db(conn)
    return conn


def _owned(conn, *, title, ownership_class="purchased", acquisition_date=BOUGHT,
           preorder_flag=1, preorder_release_ts=RELEASE_TS, igdb_id=None,
           content_id=None, source="psn_api", format_="digital"):
    return upsert_owned_game(
        conn,
        {
            "title": title,
            "normalized_title": title.lower(),
            "platform": "playstation 5",
            "format": format_,
            "ownership_class": ownership_class,
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": content_id,
            "igdb_id": igdb_id,
            "acquisition_date": acquisition_date,
            "price": None,
            "source": source,
            "source_ref": content_id,
            "status": "owned",
            "is_owned": 1,
            "provenance": f"{source}:{content_id or title}",
            "preorder_flag": preorder_flag,
            "preorder_release_ts": preorder_release_ts,
        },
    )


# --- the materialization rule -------------------------------------------------


def test_purchased_before_release_is_preorder_with_acquisition_date():
    conn = _db()
    oid = _owned(conn, title="Grand Theft Auto VI", content_id="UP1004-PPSA03420_00-GTAVI")
    report = refresh_preorder_flags(conn)
    row = conn.execute("SELECT * FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 1
    assert row["acquisition_date"] == BOUGHT  # fixed at order time
    assert row["preorder_release_ts"] == RELEASE_TS
    assert report["after"] == 1
    conn.close()


def test_psplus_claim_of_unreleased_title_is_not_a_preorder():
    """A PS+ claimed/extra title that is not out yet must NOT be a pre-order,
    even though it is owned and has an acquisition date before the release."""
    conn = _db()
    oid = _owned(
        conn, title="Some Future Claim", ownership_class="psplus_claimed",
        content_id="UP9000-PPSA00001_00-CLAIMFUTURE", source="ps_plus",
    )
    refresh_preorder_flags(conn)
    row = conn.execute("SELECT is_preorder FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 0
    conn.close()


def test_bought_after_release_is_not_a_preorder():
    conn = _db()
    oid = _owned(conn, title="Old Purchase", acquisition_date="2026-12-01T10:00:00Z")
    refresh_preorder_flags(conn)
    row = conn.execute("SELECT is_preorder FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 0
    conn.close()


def test_igdb_date_alone_cannot_invent_a_preorder():
    """The horizon gate: with no store date and no Sony preorderFlag, a later
    IGDB first_release_date must not turn an OLD purchase into a pre-order —
    a release years after the purchase is a wrong/remastered match, not a
    pre-order."""
    conn = _db()
    conn.execute(
        """INSERT INTO game_metadata(igdb_id, payload) VALUES (?, ?)""",
        (999, '{"first_release_date": 1800000000}'),
    )
    oid = _owned(
        conn, title="Remaster Oddity", preorder_flag=0, preorder_release_ts=None,
        igdb_id=999, acquisition_date="2020-01-01T00:00:00Z",
    )
    refresh_preorder_flags(conn)
    row = conn.execute("SELECT is_preorder FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 0
    conn.close()


def test_igdb_date_alone_flags_a_recent_preorder_when_sonys_flag_is_zero():
    """GTA VI, live 2026-11-16: the PSN entitlement carried preorderFlag = 0 and
    the payload no release date, so IGDB's first_release_date is the only
    evidence. Ordered 2026-11-15, releasing 2026-11-18 -> pre-order."""
    conn = _db()
    conn.execute(
        """INSERT INTO game_metadata(igdb_id, payload) VALUES (?, ?)""",
        (52189, '{"first_release_date": 1794960000}'),
    )  # 2026-11-18
    oid = _owned(
        conn, title="Grand Theft Auto VI", preorder_flag=0, preorder_release_ts=None,
        igdb_id=52189, acquisition_date="2026-11-15",
    )
    refresh_preorder_flags(conn)
    row = conn.execute(
        "SELECT is_preorder, preorder_release_ts, acquisition_date FROM owned_games WHERE id = ?",
        (oid,),
    ).fetchone()
    assert row["is_preorder"] == 1
    # the store/override column stays NULL — IGDB's date is never copied into it
    assert row["preorder_release_ts"] is None
    # and the order date is never rewritten
    assert row["acquisition_date"] == "2026-11-15"
    conn.close()


def test_igdb_date_confirms_flagged_preorder_when_store_date_missing():
    conn = _db()
    conn.execute(
        """INSERT INTO game_metadata(igdb_id, payload) VALUES (?, ?)""",
        (1000, '{"first_release_date": 1794960000}'),
    )
    oid = _owned(conn, title="Flagged, No Store Date", preorder_flag=1,
                 preorder_release_ts=None, igdb_id=1000)
    refresh_preorder_flags(conn)
    row = conn.execute("SELECT is_preorder FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 1
    conn.close()


def test_no_acquisition_date_is_not_a_preorder():
    conn = _db()
    oid = _owned(conn, title="Undated", acquisition_date=None)
    refresh_preorder_flags(conn)
    row = conn.execute("SELECT is_preorder FROM owned_games WHERE id = ?", (oid,)).fetchone()
    assert row["is_preorder"] == 0
    conn.close()


# --- the store release-date table --------------------------------------------


def test_store_release_date_is_stamped_onto_the_owned_row():
    conn = _db()
    oid = _owned(conn, title="Stamped", content_id="UP0000-PPSA00000_00-STAMP",
                 preorder_release_ts=None)
    set_release_date(conn, "UP0000-PPSA00000_00-STAMP", RELEASE_TS, source="psn_store")
    assert apply_store_release_dates(conn) == 1
    refresh_preorder_flags(conn)
    row = conn.execute(
        "SELECT preorder_release_ts, is_preorder FROM owned_games WHERE id = ?", (oid,)
    ).fetchone()
    assert row["preorder_release_ts"] == RELEASE_TS
    assert row["is_preorder"] == 1
    # idempotent — a second pass changes nothing
    assert apply_store_release_dates(conn) == 0
    conn.close()


# --- entitlement mapping ------------------------------------------------------


def test_entitlement_preorder_maps_active_date_and_flag():
    item = {
        "productId": "UP1004-PPSA03420_00-GTAVI",
        "preorderFlag": True,
        "preorderPlaceholderFlag": False,
        "activeDate": "2026-11-15T18:04:11Z",
        "gameMeta": {"name": "Grand Theft Auto VI", "type": "PSGD"},
    }
    values = entitlement_preorder_values(item)
    assert values["preorder_flag"] == 1
    assert values["acquisition_date"] == "2026-11-15T18:04:11Z"
    game = psn_library_item_to_game(item)
    assert game["acquisition_date"] == "2026-11-15T18:04:11Z"
    assert game["preorder_flag"] == 1
    assert game["source"] == "psn_api"
    assert game["ownership_class"] == "purchased"


def test_entitlement_non_preorder_leaves_acquisition_date_null():
    item = {
        "productId": "UP9000-CUSA07408_00-00000000GODOFWAR",
        "preorderFlag": False,
        "activeDate": "2018-04-20T00:00:00Z",
        "gameMeta": {"name": "God of War", "type": "PS4GD"},
    }
    game = psn_library_item_to_game(item)
    assert game["acquisition_date"] is None  # purchase_date_backfill owns these
    assert game["preorder_flag"] == 0


@pytest.mark.parametrize(
    ("value", "epoch"),
    [
        ("2026-11-18T00:00:00Z", RELEASE_TS),
        ("2026-11-18", RELEASE_TS),
        (RELEASE_TS, RELEASE_TS),
        ("1794960000", RELEASE_TS),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_to_epoch_seconds(value, epoch):
    assert to_epoch_seconds(value) == epoch


def test_iso_date_release_is_a_midnight_utc_epoch():
    assert to_epoch_seconds("2026-11-18T00:00:00Z") == RELEASE_TS
    assert datetime.fromtimestamp(RELEASE_TS, UTC).date().isoformat() == "2026-11-18"


# --- the views (pshelf's contract) -------------------------------------------


def test_catalog_views_exposes_preorder_columns():
    conn = _db()
    _owned(conn, title="Grand Theft Auto VI", content_id="UP1004-PPSA03420_00-GTAVI")
    refresh_preorder_flags(conn)
    row = conn.execute(
        """SELECT is_preorder, preorder_release_ts, acquisition_date
             FROM catalog_views WHERE title = 'Grand Theft Auto VI'"""
    ).fetchone()
    assert row is not None
    assert row["is_preorder"] == 1
    assert row["preorder_release_ts"] == RELEASE_TS
    assert row["acquisition_date"] == BOUGHT
    conn.close()


def test_catalog_games_exposes_preorder_columns_and_present_before_release():
    """The row must be in the view BEFORE release (no now()/date filter), with
    acquisition_date surfaced under the contract's column name."""
    from mailroom.verticals.game_catalog.assets import (
        catalog_games as catalog_games_asset,
    )

    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _owned(conn, title="Grand Theft Auto VI", content_id="UP1004-PPSA03420_00-GTAVI")
    conn.close()

    from dagster import build_op_context

    catalog_games_asset(build_op_context(resources={"db_url": f"sqlite:///{db}"}))

    conn = connect(f"sqlite:///{db}")
    row = conn.execute(
        """SELECT is_preorder, preorder_release_ts, acquisition_date, purchased
             FROM catalog_games WHERE title = 'Grand Theft Auto VI'"""
    ).fetchone()
    assert row is not None
    assert row["is_preorder"] == 1
    assert row["preorder_release_ts"] == RELEASE_TS
    assert row["acquisition_date"] is not None
    assert row["purchased"] == 1
    # and the per-edition view agrees
    assert conn.execute(
        "SELECT is_preorder FROM catalog_views WHERE title = 'Grand Theft Auto VI'"
    ).fetchone()["is_preorder"] == 1
    conn.close()
