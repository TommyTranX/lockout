"""Deterministic data-quality evaluators.

Every rule here is plain SQL plus a comparison. No model is involved in deciding
whether data is bad, and no model is involved in deciding whether to block a training
run. That is deliberate: the numbers a judge sees on screen have to be reproducible,
and a blocking decision that an LLM could talk itself out of is not a safety interlock.

Each evaluator returns a `RuleResult` carrying the observed value, the threshold it was
compared against, and the SQL that produced it — so the eventual denial message can
name its own evidence rather than assert a conclusion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from lockout import config

RuleType = Literal["FRESHNESS", "VOLUME", "NULL_RATE", "SCHEMA"]


@dataclass
class RuleResult:
    rule: RuleType
    table: str
    column: str
    passed: bool
    observed: Any
    threshold: Any
    sql: str
    detail: str = ""
    native: dict[str, str] = field(default_factory=dict)

    @property
    def assertion_description(self) -> str:
        return f"{self.rule.title()} check on {self.table}.{self.column}"


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or config.DB_PATH)


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------- freshness
def freshness(
    table: str,
    column: str,
    reference_table: str,
    reference_column: str,
    max_lag_days: int = config.FRESHNESS_MAX_LAG_DAYS,
    db_path: str | None = None,
) -> RuleResult:
    """Is `table` keeping up with its upstream?

    Deliberately *relative*: it compares MAX(timestamp) against the upstream table
    rather than against wall-clock now. The sample data is from 2016, so an absolute
    "is it less than 24h old" check would flag everything and prove nothing. Lag
    against the source is the real question a pipeline owner asks.
    """
    sql = (
        f"SELECT (SELECT MAX({column}) FROM {table}) AS downstream_max,\n"
        f"       (SELECT MAX({reference_column}) FROM {reference_table}) AS upstream_max"
    )
    with _conn(db_path) as c:
        downstream_raw, upstream_raw = c.execute(sql).fetchone()

    downstream, upstream = _as_date(downstream_raw), _as_date(upstream_raw)
    if downstream is None or upstream is None:
        return RuleResult(
            "FRESHNESS", table, column, False, str(downstream_raw), str(upstream_raw),
            sql, "could not parse timestamps",
        )

    lag = (upstream - downstream).days
    return RuleResult(
        rule="FRESHNESS",
        table=table,
        column=column,
        passed=lag <= max_lag_days,
        observed=lag,
        threshold=max_lag_days,
        sql=sql,
        detail=(
            f"{table} has data through {downstream}, but {reference_table} has data "
            f"through {upstream} — {lag} days behind (limit {max_lag_days})"
        ),
        native={
            "lag_days": str(lag),
            "downstream_max": str(downstream),
            "upstream_max": str(upstream),
            "max_lag_days": str(max_lag_days),
        },
    )


# ------------------------------------------------------------------------ volume
def volume_collapse(
    table: str,
    date_column: str,
    min_rows: int = config.VOLUME_MIN_ROWS_PER_DAY,
    drop_ratio: float = config.VOLUME_DROP_RATIO,
    db_path: str | None = None,
) -> RuleResult:
    """Did a load complete successfully but bring almost nothing?

    The canonical silent failure: the job is green, the row count is not zero (so a
    naive "is it empty" check passes), but the partition is a rounding error against
    its own history.
    """
    sql = (
        f"SELECT {date_column} AS d, COUNT(*) AS n FROM {table}\n"
        f"GROUP BY {date_column} ORDER BY d"
    )
    with _conn(db_path) as c:
        rows = c.execute(sql).fetchall()

    if not rows:
        return RuleResult("VOLUME", table, date_column, False, 0, min_rows, sql, "no rows at all")

    counts = [n for _, n in rows]
    ordered = sorted(counts)
    median = ordered[len(ordered) // 2]
    worst_day, worst_count = min(rows, key=lambda r: r[1])
    floor = max(min_rows, int(median * drop_ratio))

    return RuleResult(
        rule="VOLUME",
        table=table,
        column=date_column,
        passed=worst_count >= floor,
        observed=worst_count,
        threshold=floor,
        sql=sql,
        detail=(
            f"{table} loaded {worst_count} rows on {worst_day}, against a median daily "
            f"volume of {median} — below the floor of {floor}"
        ),
        native={
            "worst_day": str(worst_day),
            "worst_day_rows": str(worst_count),
            "median_daily_rows": str(median),
            "floor": str(floor),
        },
    )


# --------------------------------------------------------------------- null rate
def null_rate(
    table: str,
    column: str,
    max_rate: float = config.NULL_RATE_MAX,
    db_path: str | None = None,
) -> RuleResult:
    sql = (
        f"SELECT COUNT(*) AS total, SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) "
        f"AS nulls FROM {table}"
    )
    with _conn(db_path) as c:
        total, nulls = c.execute(sql).fetchone()
    nulls = nulls or 0
    rate = (nulls / total) if total else 1.0
    return RuleResult(
        rule="NULL_RATE",
        table=table,
        column=column,
        passed=rate <= max_rate,
        observed=round(rate, 6),
        threshold=max_rate,
        sql=sql,
        detail=f"{table}.{column} is {rate:.2%} null across {total} rows (limit {max_rate:.0%})",
        native={"null_rate": f"{rate:.6f}", "null_count": str(nulls), "row_count": str(total)},
    )


# ------------------------------------------------------------------- the ruleset
def evaluate_all(db_path: str | None = None) -> list[RuleResult]:
    """The rules Lockout arms for the taxi pipeline.

    Kept as an explicit list rather than something inferred, so that what is being
    checked is auditable from the source.
    """
    staging = config.STAGING_TABLE.split(".")[-1]
    raw = config.RAW_TABLE.split(".")[-1]

    return [
        freshness(staging, "trip_date", raw, "tpep_pickup_datetime", db_path=db_path),
        volume_collapse(staging, "trip_date", db_path=db_path),
        null_rate(staging, "fare_amount", db_path=db_path),
        null_rate(staging, "trip_distance", db_path=db_path),
        null_rate(staging, "passenger_count", db_path=db_path),
    ]
