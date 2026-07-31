"""Central configuration.

Telemetry is disabled at *import* time, deliberately. The DataHub client's telemetry
path was measured at 54.32s per call versus 0.09s with it off; a single forgotten
export makes the tool unusable and the demo unrecordable. Setting it here means it
cannot be forgotten by any entry point.
"""

from __future__ import annotations

import os

os.environ["DATAHUB_TELEMETRY_ENABLED"] = "false"

# --- DataHub -----------------------------------------------------------------
GMS_URL: str = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
GMS_TOKEN: str | None = os.environ.get("DATAHUB_GMS_TOKEN") or None

# --- The demo pipeline -------------------------------------------------------
PLATFORM = "sqlite"
ENV = "PROD"
DB_PATH = os.environ.get("LOCKOUT_DB", "data/nyc_taxi_pipeline.db")

# These match the URNs produced by `recipes/taxi.yml` verbatim. The sqlalchemy source
# names sqlite datasets `<schema>.<table>` with no database prefix, so the dataset URN
# is e.g. urn:li:dataset:(urn:li:dataPlatform:sqlite,main.staging_trips,PROD).
RAW_TABLE = "main.raw_trips"
STAGING_TABLE = "main.staging_trips"
MART_TABLE = "main.mart_daily_summary"

# --- The model under protection ----------------------------------------------
MODEL_PLATFORM = "lockout"
MODEL_NAME = "taxi_demand_v1"
FEATURE_TABLE = "taxi_features"

# Feature name -> (source table, source column). This binding is what lets the gate
# stay column-aware even though mlFeature.sources can only hold *dataset* URNs
# (its Avro annotation is entityTypes:["dataset"] — schemaField URNs are rejected).
FEATURE_BINDINGS: dict[str, tuple[str, str]] = {
    "trips_7d": (STAGING_TABLE, "trip_date"),
    "avg_fare_7d": (STAGING_TABLE, "fare_amount"),
    "avg_distance_7d": (STAGING_TABLE, "trip_distance"),
    "avg_duration_7d": (STAGING_TABLE, "trip_duration_min"),
    "passenger_mean_7d": (STAGING_TABLE, "passenger_count"),
    "revenue_7d": (STAGING_TABLE, "total_amount"),
}

# --- Policy thresholds -------------------------------------------------------
# Deliberately explicit and boring: the gate decision must be auditable, so every
# threshold is a named constant rather than a heuristic.
FRESHNESS_MAX_LAG_DAYS = 2
VOLUME_MIN_ROWS_PER_DAY = 100
VOLUME_DROP_RATIO = 0.10  # a day below 10% of the trailing median is a collapse
NULL_RATE_MAX = 0.05

# --- Structured properties ---------------------------------------------------
SP_STATE = "urn:li:structuredProperty:lockout.trainingState"
SP_LAST_DECISION = "urn:li:structuredProperty:lockout.lastDecisionMs"

ACTOR = "urn:li:corpuser:lockout"
