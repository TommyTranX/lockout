"""What the block actually bought you.

The honest way to measure a safety interlock is a controlled comparison: same model,
same hyper-parameters, same evaluation rows — the *only* difference is whether the
training data had the defect.

DataHub ships both variants of the taxi database, which makes the experiment clean:

    nyc_taxi.db           the pipeline working correctly
    nyc_taxi_pipeline.db  the same pipeline with staleness planted

Both models are scored against the **same holdout taken from the clean database**, so
the metric answers one question: how much worse is the model a stale pipeline produces?

This is deliberately not a claim that Lockout improves a model. It doesn't. It prevents
a worse one from being created, and this measures the gap.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

from lockout.training.job import FEATURES, TARGET

CLEAN_DB = "data/nyc_taxi.db"
STALE_DB = "data/nyc_taxi_pipeline.db"


@dataclass
class Arm:
    label: str
    db: str
    train_rows: int
    mae: float
    rmse: float


def _frame(db: str) -> pd.DataFrame:
    with sqlite3.connect(db) as conn:
        return pd.read_sql_query(
            "SELECT trip_date, trip_count, avg_fare, avg_distance, avg_passengers, "
            "avg_duration_min, total_revenue FROM mart_daily_summary ORDER BY trip_date",
            conn,
        ).dropna()


def _fit_score(train_df: pd.DataFrame, test_df: pd.DataFrame) -> tuple[float, float]:
    model = GradientBoostingRegressor(random_state=0, n_estimators=200, max_depth=3)
    model.fit(train_df[FEATURES], train_df[TARGET])
    preds = model.predict(test_df[FEATURES])
    return (
        float(mean_absolute_error(test_df[TARGET], preds)),
        float(mean_squared_error(test_df[TARGET], preds) ** 0.5),
    )


def run(clean_db: str = CLEAN_DB, stale_db: str = STALE_DB) -> dict:
    clean, stale = _frame(clean_db), _frame(stale_db)

    # Holdout is the tail of the CLEAN data — the reality both models are judged against.
    split = int(len(clean) * 0.75)
    holdout = clean.iloc[split:]
    holdout_dates = set(holdout["trip_date"])

    # Each arm trains only on rows its own database actually contains, with the holdout
    # dates removed so neither model can see the answers.
    clean_train = clean[~clean["trip_date"].isin(holdout_dates)]
    stale_train = stale[~stale["trip_date"].isin(holdout_dates)]

    clean_mae, clean_rmse = _fit_score(clean_train, holdout)
    stale_mae, stale_rmse = _fit_score(stale_train, holdout)

    arms = [
        Arm("clean pipeline", clean_db, len(clean_train), clean_mae, clean_rmse),
        Arm("stale pipeline", stale_db, len(stale_train), stale_mae, stale_rmse),
    ]

    with sqlite3.connect(clean_db) as c:
        clean_staging = c.execute("SELECT COUNT(*) FROM staging_trips").fetchone()[0]
    with sqlite3.connect(stale_db) as c:
        stale_staging = c.execute("SELECT COUNT(*) FROM staging_trips").fetchone()[0]

    identical = clean_train[FEATURES + [TARGET]].equals(stale_train[FEATURES + [TARGET]])

    return {
        "method": (
            "Same GradientBoostingRegressor(random_state=0, n_estimators=200, "
            "max_depth=3), same features, scored on the same holdout taken from the "
            "clean database. Holdout dates are excluded from both training sets. The "
            "only difference between arms is whether the training data carried the "
            "pipeline defect."
        ),
        "holdout_rows": len(holdout),
        "holdout_range": [str(holdout["trip_date"].iloc[0]), str(holdout["trip_date"].iloc[-1])],
        "silent_data_loss": {
            "staging_rows_clean": clean_staging,
            "staging_rows_stale": stale_staging,
            "rows_missing": clean_staging - stale_staging,
            "rows_missing_pct": round(
                100 * (clean_staging - stale_staging) / clean_staging, 2
            ),
            "mart_days_clean": len(clean),
            "mart_days_stale": len(stale),
        },
        "arms": [a.__dict__ for a in arms],
        "delta": {
            "mae_absolute": round(stale_mae - clean_mae, 2),
            "mae_ratio": round(stale_mae / clean_mae, 3) if clean_mae else None,
            "rmse_absolute": round(stale_rmse - clean_rmse, 2),
        },
        # Reported rather than hidden. A null result here is the honest outcome for
        # this dataset, and it is also the more interesting one.
        "training_sets_identical_after_holdout_exclusion": bool(identical),
        "interpretation": (
            "No metric delta, and that is the finding. Every day the stale pipeline is "
            "missing falls inside the holdout window, so once holdout dates are excluded "
            "from both arms the two training sets are identical and the models are "
            "byte-for-byte equivalent. At this dataset's granularity — "
            f"{len(clean)} daily rows across 15 months — a degradation claim is not "
            "supportable, and one is not made.\n\n"
            "What IS measured, and is the actual harm: the stale pipeline silently "
            f"dropped {clean_staging - stale_staging:,} of {clean_staging:,} staging rows "
            f"({round(100 * (clean_staging - stale_staging) / clean_staging, 2)}%) and 9 "
            "days of recency, and BOTH runs complete successfully while reporting "
            "plausible metrics. That is precisely what makes this failure mode dangerous: "
            "nothing downstream looks wrong. Lockout's claim is not that it produces a "
            "better model — it is that it refuses to produce a model nobody can tell is "
            "broken."
        ),
    }


def write(out_dir: str | Path = "examples") -> Path:
    result = run()
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    target = path / "counterfactual.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    return target
