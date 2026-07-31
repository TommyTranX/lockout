"""Everything Lockout writes back into the graph besides assertions.

The point of writing back at all: a block that only exists in a terminal is a private
opinion. A block recorded as an incident, a skipped run, a state badge and a receipt is
something the catalog remembers, and the next person to open that dataset sees it.
"""

from __future__ import annotations

import time

from datahub.emitter.mcp import MetadataChangeProposalWrapper as MCP
import datahub.metadata.schema_classes as models

from lockout import config, graph, urns
from lockout.policy.decision import Permit


def _now() -> int:
    return int(time.time() * 1000)


def _stamp(ts: int | None = None) -> models.AuditStampClass:
    return models.AuditStampClass(time=ts or _now(), actor=config.ACTOR)


# ----------------------------------------------------------------- incidents
def raise_incident(permit: Permit) -> str | None:
    """Open an incident on the dataset that caused the block.

    Emitted as an aspect rather than through the `raiseIncident` GraphQL mutation,
    which returns 403 Unauthorized on a default quickstart even with an actor header.
    The aspect path needs no auth and is what actually renders on the dataset page.

    The incident is attached to the *dataset*, not the model: `mlModel` has no
    `incidentsSummary` aspect in the entity registry, so a model-level incident would
    not roll up anywhere.
    """
    if permit.granted or not permit.evidence:
        return None

    top = permit.evidence[0]
    slug = f"{urns.table_of(top.dataset_urn).replace('.', '-')}-{top.rule.lower()}"
    incident_urn = urns.incident(slug)
    now = _now()

    where = f"{urns.table_of(top.dataset_urn)}.{top.column}" if top.column else urns.table_of(top.dataset_urn)
    observed = ", ".join(f"{k}={v}" for k, v in top.observed.items())

    graph.emit(
        [
            MCP(
                entityUrn=incident_urn,
                aspect=models.IncidentInfoClass(
                    type=models.IncidentTypeClass.FRESHNESS
                    if "FRESH" in top.rule.upper()
                    else models.IncidentTypeClass.OPERATIONAL,
                    title=f"Training blocked: {where}",
                    description=(
                        f"{top.description}\n\n"
                        f"Observed: {observed}\n\n"
                        f"Lockout denied a training permit for {permit.model_urn} because this "
                        f"asset is {top.hops} hop(s) upstream of it via "
                        f"{' -> '.join(top.lineage_path)}."
                    ),
                    entities=[top.dataset_urn],
                    status=models.IncidentStatusClass(
                        state=models.IncidentStateClass.ACTIVE,
                        lastUpdated=_stamp(now),
                    ),
                    created=_stamp(now),
                    source=models.IncidentSourceClass(
                        type=models.IncidentSourceTypeClass.MANUAL
                    ),
                ),
            )
        ]
    )
    return incident_urn


def resolve_incident(incident_urn: str, note: str = "Upstream data recovered.") -> None:
    """Flip an incident to RESOLVED once the data is healthy again."""
    now = _now()
    existing = graph.client().get_aspect(incident_urn, models.IncidentInfoClass)
    if existing is None:
        return
    existing.status = models.IncidentStatusClass(
        state=models.IncidentStateClass.RESOLVED,
        lastUpdated=_stamp(now),
        message=note,
    )
    graph.emit([MCP(entityUrn=incident_urn, aspect=existing)])


# --------------------------------------------------------------- run records
def record_run(permit: Permit, run_id: str, metrics: dict[str, float] | None = None) -> str:
    """Record the training run as a dataProcessInstance.

    A denied run is written as SKIPPED rather than simply not written. The absence of a
    run is indistinguishable from nobody trying; a SKIPPED run with a reason is a fact
    the catalog can show later.
    """
    dpi = urns.process_instance(run_id)
    now = _now()

    aspects: list[MCP] = [
        MCP(
            entityUrn=dpi,
            aspect=models.DataProcessInstancePropertiesClass(
                name=f"{config.MODEL_NAME} training ({run_id})",
                type=models.DataProcessTypeClass.BATCH_SCHEDULED,
                created=_stamp(now),
                customProperties={
                    "lockout.verdict": permit.verdict,
                    "lockout.decision_ms": str(permit.elapsed_ms),
                    "lockout.evidence_count": str(len(permit.evidence)),
                },
            ),
        ),
        # No `dataProcessInstanceRelationships` aspect here: its `parentTemplate` field
        # only accepts dataJob/dataFlow URNs, and rejects an mlModel with
        # "is not a valid destination for field path: /parentTemplate". The link back
        # to the model is carried by `mlTrainingRunProperties` on runs that proceeded,
        # and by the custom properties above on runs that did not.
        MCP(
            entityUrn=dpi,
            aspect=models.DataProcessInstanceRunEventClass(
                timestampMillis=now,
                status=models.DataProcessRunStatusClass.COMPLETE,
                result=models.DataProcessInstanceRunResultClass(
                    type=(
                        models.RunResultTypeClass.SUCCESS
                        if permit.granted
                        else models.RunResultTypeClass.SKIPPED
                    ),
                    nativeResultType="lockout-granted" if permit.granted else "lockout-denied",
                ),
            ),
        ),
    ]

    if permit.granted and metrics:
        aspects.append(
            MCP(
                entityUrn=dpi,
                aspect=models.MLTrainingRunPropertiesClass(
                    id=run_id,
                    trainingMetrics=[
                        models.MLMetricClass(name=k, value=str(round(v, 6)))
                        for k, v in metrics.items()
                    ],
                ),
            )
        )

    graph.emit(aspects)
    return dpi


# ------------------------------------------------------------------- state
def set_state(permit: Permit) -> None:
    """Stamp lockout.trainingState on the model and the implicated datasets."""
    value = "CLEAR" if permit.granted else "LOCKED"
    targets = [urns.model()] + [e.dataset_urn for e in permit.evidence]
    now = _now()

    for target in dict.fromkeys(targets):
        graph.emit(
            [
                MCP(
                    entityUrn=target,
                    aspect=models.StructuredPropertiesClass(
                        properties=[
                            models.StructuredPropertyValueAssignmentClass(
                                propertyUrn=config.SP_STATE, values=[value]
                            ),
                            models.StructuredPropertyValueAssignmentClass(
                                propertyUrn=config.SP_LAST_DECISION, values=[float(now)]
                            ),
                        ]
                    ),
                )
            ]
        )


# ------------------------------------------------------------------ receipt
def write_receipt(permit: Permit, run_id: str, narrative: str | None = None) -> str:
    """Persist the decision as documentation on the model.

    Uses `institutionalMemory` rather than the `document` entity: documents are
    available on this server, but institutional memory renders directly on the model
    page where someone investigating a blocked run will actually look.
    """
    model = urns.model()
    now = _now()

    existing = graph.client().get_aspect(model, models.InstitutionalMemoryClass)
    elements = list(existing.elements) if existing else []

    summary = (
        f"{permit.verdict} run {run_id}: "
        + ("; ".join(e.summary() for e in permit.evidence) if permit.evidence else "all checks passed")
    )
    if narrative:
        summary = f"{summary} — {narrative}"

    elements.append(
        models.InstitutionalMemoryMetadataClass(
            url=f"https://github.com/TommyTranX/lockout#decision-{run_id}",
            description=summary[:900],
            createStamp=_stamp(now),
        )
    )
    graph.emit(
        [MCP(entityUrn=model, aspect=models.InstitutionalMemoryClass(elements=elements))]
    )
    return summary


def commit_decision(
    permit: Permit, run_id: str, metrics: dict[str, float] | None = None
) -> dict[str, str | None]:
    """Write the whole decision to the graph in one call."""
    return {
        "incident": raise_incident(permit),
        "run": record_run(permit, run_id, metrics),
        "receipt": write_receipt(permit, run_id),
        "state": "LOCKED" if not permit.granted else "CLEAR",
    } | ({"state_written": str(set_state(permit) or "ok")})
