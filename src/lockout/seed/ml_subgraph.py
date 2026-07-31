"""Seed the ML half of the graph.

No DataHub sample dataset ships ML entities — there is no mlModel, mlFeature or
mlFeatureTable anywhere in `static-assets`. Every ML entry in this hackathon therefore
has to invent its own, and this file is Lockout's. It is disclosed in the README and
contributed back upstream (see docs/UPSTREAM_PRS.md) so the next person doesn't have to.

What it builds:

    main.raw_trips ──(UpstreamLineage + FineGrainedLineage)──▶ main.staging_trips
                                                                     │
                                                            (DerivedFrom)
                                                                     ▼
                                     mlFeatureTable:taxi_features ─▶ mlFeature × 6
                                                                     │
                                                              (Consumes)
                                                                     ▼
                                                         mlModel:taxi_demand_v1
                                                                     │
                                                                     ▼
                                                    mlModelDeployment:prod-us-east

One correction worth recording: `mlFeature.sources` cannot hold schemaField URNs. Its
Avro annotation is `{"Relationship": {"/*": {"entityTypes": ["dataset"], ...}}}`, and
GMS rejects a schemaField URN with "is not a valid destination". Column granularity
therefore comes from three cooperating places: dataset-typed `sources` for the
traversable edge, `FineGrainedLineage` for real column->column lineage between
datasets, and a `lockout.source_column` custom property binding each feature to the
column it reads.
"""

from __future__ import annotations

import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper as MCP
import datahub.metadata.schema_classes as models

from lockout import config, graph, urns


def _now() -> int:
    return int(time.time() * 1000)


def _stamp() -> models.AuditStampClass:
    return models.AuditStampClass(time=_now(), actor=config.ACTOR)


def build_lineage() -> list[MCP]:
    """raw_trips -> staging_trips, at both table and column level.

    DataHub's own recipe for this dataset sets `include_view_lineage: false`, so these
    edges do not exist after ingestion. Lockout emits them.
    """
    raw, staging = urns.dataset(config.RAW_TABLE), urns.dataset(config.STAGING_TABLE)

    # Column-level: the columns staging actually derives from raw.
    column_pairs = [
        ("tpep_pickup_datetime", "trip_date"),
        ("fare_amount", "fare_amount"),
        ("trip_distance", "trip_distance"),
        ("passenger_count", "passenger_count"),
        ("total_amount", "total_amount"),
    ]
    fine_grained = [
        models.FineGrainedLineageClass(
            upstreamType=models.FineGrainedLineageUpstreamTypeClass.FIELD_SET,
            downstreamType=models.FineGrainedLineageDownstreamTypeClass.FIELD,
            upstreams=[urns.field(config.RAW_TABLE, up)],
            downstreams=[urns.field(config.STAGING_TABLE, down)],
            confidenceScore=1.0,
            transformOperation="TRANSFORM",
        )
        for up, down in column_pairs
    ]

    mart = urns.dataset(config.MART_TABLE)
    return [
        MCP(
            entityUrn=staging,
            aspect=models.UpstreamLineageClass(
                upstreams=[
                    models.UpstreamClass(
                        dataset=raw, type=models.DatasetLineageTypeClass.TRANSFORMED
                    )
                ],
                fineGrainedLineages=fine_grained,
            ),
        ),
        MCP(
            entityUrn=mart,
            aspect=models.UpstreamLineageClass(
                upstreams=[
                    models.UpstreamClass(
                        dataset=staging, type=models.DatasetLineageTypeClass.TRANSFORMED
                    )
                ],
            ),
        ),
    ]


def build_ml_entities() -> list[MCP]:
    """The feature table, its features, the model, and the deployment."""
    staging = urns.dataset(config.STAGING_TABLE)
    feature_urns = [urns.feature(name) for name in config.FEATURE_BINDINGS]

    mcps: list[MCP] = []

    for name, (table, column) in config.FEATURE_BINDINGS.items():
        mcps.append(
            MCP(
                entityUrn=urns.feature(name),
                aspect=models.MLFeaturePropertiesClass(
                    description=f"{name} derived from {table}.{column}",
                    dataType=models.MLFeatureDataTypeClass.CONTINUOUS,
                    # dataset URNs only — schemaField URNs are rejected here.
                    sources=[urns.dataset(table)],
                    customProperties={
                        "lockout.source_column": column,
                        "lockout.source_table": table,
                        # Recorded so the gate can report the exact column URN it
                        # blamed without reconstructing it.
                        "lockout.source_field_urn": urns.field(table, column),
                    },
                ),
            )
        )

    mcps.append(
        MCP(
            entityUrn=urns.feature_table(),
            aspect=models.MLFeatureTablePropertiesClass(
                description="Rolling 7-day demand features for NYC taxi forecasting.",
                mlFeatures=feature_urns,
            ),
        )
    )

    mcps.append(
        MCP(
            entityUrn=urns.model(),
            aspect=models.MLModelPropertiesClass(
                description=(
                    "Gradient-boosted daily taxi demand forecaster. Trains on "
                    "main.staging_trips; serves prod-us-east."
                ),
                mlFeatures=feature_urns,
                deployments=[urns.deployment()],
                customProperties={"lockout.protected": "true"},
            ),
        )
    )

    mcps.append(
        MCP(
            entityUrn=urns.deployment(),
            aspect=models.MLModelDeploymentPropertiesClass(
                description="Production deployment, us-east.",
                customProperties={"region": "us-east", "replicas": "3"},
            ),
        )
    )

    # The transform that produced the staging column, kept as a first-class query
    # entity so a denial can quote the SQL rather than describe it.
    mcps.append(
        MCP(
            entityUrn=urns.query("staging-transform"),
            aspect=models.QueryPropertiesClass(
                statement=models.QueryStatementClass(
                    value=(
                        "INSERT INTO staging_trips\n"
                        "SELECT *, DATE(tpep_pickup_datetime) AS trip_date,\n"
                        "       (julianday(tpep_dropoff_datetime) - julianday(tpep_pickup_datetime)) * 1440\n"
                        "         AS trip_duration_min,\n"
                        "       'staged' AS pipeline_status\n"
                        "FROM raw_trips\n"
                        "WHERE fare_amount > 0 AND trip_distance > 0 AND passenger_count IS NOT NULL;"
                    ),
                    language=models.QueryLanguageClass.SQL,
                ),
                source=models.QuerySourceClass.SYSTEM,
                name="staging_trips transform",
                created=_stamp(),
                lastModified=_stamp(),
            ),
        )
    )
    mcps.append(
        MCP(
            entityUrn=urns.query("staging-transform"),
            aspect=models.QuerySubjectsClass(
                subjects=[
                    models.QuerySubjectClass(entity=urns.dataset(config.STAGING_TABLE)),
                    models.QuerySubjectClass(entity=urns.dataset(config.RAW_TABLE)),
                ]
            ),
        )
    )
    return mcps


def build_structured_properties() -> list[MCP]:
    """Lockout's own additions to the metadata model.

    `showAsAssetBadge` is not settable from acryl-datahub 1.6.0.16 (the
    StructuredPropertyDefinitionClass constructor has no `settings` kwarg), so the
    state is carried as a plain allowed-value property.
    """
    return [
        MCP(
            entityUrn=config.SP_STATE,
            aspect=models.StructuredPropertyDefinitionClass(
                qualifiedName="lockout.trainingState",
                displayName="Lockout State",
                description="Whether Lockout currently permits training on this asset.",
                valueType="urn:li:dataType:datahub.string",
                entityTypes=[
                    "urn:li:entityType:datahub.mlModel",
                    "urn:li:entityType:datahub.dataset",
                ],
                cardinality="SINGLE",
                allowedValues=[
                    models.PropertyValueClass(value="LOCKED", description="Training is blocked."),
                    models.PropertyValueClass(value="CLEAR", description="Training is permitted."),
                ],
            ),
        ),
        MCP(
            entityUrn=config.SP_LAST_DECISION,
            aspect=models.StructuredPropertyDefinitionClass(
                qualifiedName="lockout.lastDecisionMs",
                displayName="Lockout Last Decision",
                description="Epoch millis of the last permit decision Lockout made.",
                valueType="urn:li:dataType:datahub.number",
                entityTypes=[
                    "urn:li:entityType:datahub.mlModel",
                    "urn:li:entityType:datahub.dataset",
                ],
                cardinality="SINGLE",
            ),
        ),
    ]


def seed() -> dict[str, int]:
    """Emit the whole subgraph. Safe to re-run."""
    counts = {
        "structured_properties": graph.emit(build_structured_properties()),
        "lineage": graph.emit(build_lineage()),
        "ml_entities": graph.emit(build_ml_entities()),
    }
    return counts
