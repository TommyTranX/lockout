"""Tests that need no DataHub instance.

The rule evaluators and URN construction are the two things that must never silently
change: the first produces every number the project claims, the second is what keeps
the project independent of DataHub's search index.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lockout import urns
from lockout.policy import rules

FIXTURE = Path(__file__).parent / "fixture.db"


@pytest.fixture(scope="module")
def db() -> str:
    """A tiny pipeline with a known-stale downstream and one collapsed day."""
    if FIXTURE.exists():
        FIXTURE.unlink()
    conn = sqlite3.connect(FIXTURE)
    conn.executescript(
        """
        CREATE TABLE raw_trips (tpep_pickup_datetime TEXT, fare_amount REAL);
        CREATE TABLE staging_trips (trip_date TEXT, fare_amount REAL, trip_distance REAL);
        """
    )
    conn.executemany(
        "INSERT INTO raw_trips VALUES (?, ?)",
        [("2026-01-10 10:00:00", 10.0)] + [("2026-01-01 10:00:00", 10.0)] * 5,
    )
    # staging stops on the 1st: nine days behind raw.
    rows = [("2026-01-01", 10.0, 2.0)] * 300
    rows += [("2026-01-02", 10.0, 2.0)] * 2  # the collapsed day
    conn.executemany("INSERT INTO staging_trips VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    yield str(FIXTURE)
    FIXTURE.unlink(missing_ok=True)


def test_freshness_detects_lag(db: str) -> None:
    result = rules.freshness(
        "staging_trips", "trip_date", "raw_trips", "tpep_pickup_datetime",
        max_lag_days=2, db_path=db,
    )
    assert not result.passed
    assert result.observed == 8  # 2026-01-10 minus 2026-01-02
    assert result.native["upstream_max"] == "2026-01-10"


def test_freshness_passes_when_current(db: str) -> None:
    result = rules.freshness(
        "staging_trips", "trip_date", "raw_trips", "tpep_pickup_datetime",
        max_lag_days=30, db_path=db,
    )
    assert result.passed


def test_volume_collapse_is_caught(db: str) -> None:
    result = rules.volume_collapse("staging_trips", "trip_date", db_path=db)
    assert not result.passed
    assert result.observed == 2
    assert result.native["worst_day"] == "2026-01-02"


def test_null_rate_passes_on_clean_column(db: str) -> None:
    assert rules.null_rate("staging_trips", "fare_amount", db_path=db).passed


def test_rule_results_carry_their_sql(db: str) -> None:
    """Every denial has to be able to show the query that produced it."""
    for result in (
        rules.freshness("staging_trips", "trip_date", "raw_trips",
                        "tpep_pickup_datetime", db_path=db),
        rules.volume_collapse("staging_trips", "trip_date", db_path=db),
        rules.null_rate("staging_trips", "fare_amount", db_path=db),
    ):
        assert "SELECT" in result.sql.upper()


# ------------------------------------------------------------------------ urns
def test_urn_round_trip() -> None:
    urn = urns.dataset("main.staging_trips")
    assert urn.startswith("urn:li:dataset:")
    assert urns.table_of(urn) == "main.staging_trips"


def test_field_urn_nests_the_dataset() -> None:
    assert urns.field("main.staging_trips", "trip_date").startswith("urn:li:schemaField:")


def test_no_module_resolves_urns_by_search() -> None:
    """Guards the rule that keeps Lockout independent of the search index.

    DataHub's search index is written asynchronously and can stall while writes keep
    succeeding. Any code path that discovers a URN by searching will silently return
    nothing in that state.
    """
    src = Path(__file__).parent.parent / "src" / "lockout"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text()
        for needle in ("searchAcrossEntities", "get_urns_by_filter", "list_all_entity_urns"):
            if needle in text:
                offenders.append(f"{path.name}:{needle}")
    assert not offenders, f"URNs must be constructed, not searched: {offenders}"
