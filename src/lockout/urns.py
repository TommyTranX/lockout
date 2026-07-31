"""Deterministic URN construction.

Every URN in Lockout is *built*, never *searched for*.

This is not a style preference. DataHub's search index is populated asynchronously by
the MAE consumer, and on a resource-constrained box that consumer can fall out of its
Kafka consumer group and stop indexing entirely while writes keep succeeding. In that
state a search-based URN lookup returns nothing and the caller silently does the wrong
thing. It is the documented cause of `static-assets/add_lineage.py` printing
"No datasets found" immediately after a successful ingest.

Building URNs from known identifiers removes the dependency on the index completely.
`tests/test_urns.py` asserts that no module in this package calls a search API.
"""

from __future__ import annotations

from datahub.emitter.mce_builder import (
    make_dataset_urn,
    make_ml_feature_table_urn,
    make_ml_feature_urn,
    make_ml_model_urn,
    make_schema_field_urn,
)

from lockout import config


def dataset(table: str) -> str:
    """`nyc_taxi.main.staging_trips` -> the sqlite dataset URN."""
    return make_dataset_urn(platform=config.PLATFORM, name=table, env=config.ENV)


def field(table: str, column: str) -> str:
    """A column URN, e.g. staging_trips.trip_date."""
    return make_schema_field_urn(dataset(table), column)


def feature(name: str) -> str:
    return make_ml_feature_urn(config.FEATURE_TABLE, name)


def feature_table() -> str:
    return make_ml_feature_table_urn(config.MODEL_PLATFORM, config.FEATURE_TABLE)


def model() -> str:
    return make_ml_model_urn(config.MODEL_PLATFORM, config.MODEL_NAME, config.ENV)


def deployment() -> str:
    return f"urn:li:mlModelDeployment:(urn:li:dataPlatform:{config.MODEL_PLATFORM},{config.MODEL_NAME}-prod-us-east,{config.ENV})"


def process_instance(run_id: str) -> str:
    return f"urn:li:dataProcessInstance:lockout-{run_id}"


def incident(slug: str) -> str:
    return f"urn:li:incident:lockout-{slug}"


def query(slug: str) -> str:
    return f"urn:li:query:lockout-{slug}"


def table_of(dataset_urn: str) -> str:
    """Inverse of `dataset()` — pull the table name back out of a dataset URN."""
    # urn:li:dataset:(urn:li:dataPlatform:sqlite,nyc_taxi.main.staging_trips,PROD)
    inner = dataset_urn[dataset_urn.index("(") + 1 : dataset_urn.rindex(")")]
    return inner.split(",")[1]
