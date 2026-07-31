"""Write assertions and their run history back into DataHub.

Open-source DataHub models assertions well but ships no scheduler — nothing evaluates
them on a cadence. Lockout is that scheduler for the checks it arms.

Both halves of this module — creating an assertion and recording its result — are done
by **emitting aspects**, not by calling the GraphQL mutations that nominally exist for
the purpose. That is not a stylistic choice; the mutation path is unusable here for
four independent reasons, each verified against OSS GMS v1.5.0.6 and written up in
docs/UPSTREAM_PRS.md:

1. `upsertCustomAssertion` returns **403 Unauthorized on a freshly booted quickstart**.
   It only begins working after somebody logs into the UI, which bootstraps the actor's
   policies — so anyone cloning this repo and running `make judge` on a clean install
   would fail at the first assertion. This was caught by a cold-clone rehearsal, not in
   normal development, because a long-running dev instance has always been logged into.

2. `DataHubGraph.report_assertion_result()` sends a `severity` field typed
   `AssertionResultSeverity`, which the OSS GraphQL schema does not define:

       Unknown type 'AssertionResultSeverity'
       argument 'result' contains a field not in 'AssertionResultInput': 'severity'

3. `reportAssertionResult` cannot service a **column-scoped** assertion at all:
   `fieldPath` makes `asserteeUrn` a `schemaField`, but `assertionRunEvent` validation
   requires a `dataset`.

4. `upsertCustomAssertion` returns before the assertion is resolvable, so an immediate
   report fails with "does not exist or is not associated with any entity".

Aspect emission goes through the REST sink, needs no auth on a default quickstart,
produces the same entities, and is immune to all four.
"""

from __future__ import annotations

import time

from lockout import config, graph, urns
from lockout.policy.rules import RuleResult

def upsert(result: RuleResult) -> str:
    """Create or update the assertion corresponding to a rule, return its URN.

    Emitted as an `assertionInfo` aspect rather than through the `upsertCustomAssertion`
    GraphQL mutation. The mutation returns **403 Unauthorized on a freshly booted
    quickstart** — it only starts working once someone has logged into the UI, which
    bootstraps the actor's policies:

        {'message': 'Unauthorized to perform this action.',
         'path': ['upsertCustomAssertion'], 'extensions': {'code': 403}}

    A judge cloning this repo and running `make judge` would hit that on a clean
    install. Aspect emission goes through the REST sink, needs no auth on the default
    quickstart, and yields the same entity — so the whole arming path is auth-free.

    The URN is derived deterministically from (table, column, rule) so re-arming
    updates the existing assertion instead of creating duplicates.
    """
    import datahub.metadata.schema_classes as models
    from datahub.emitter.mcp import MetadataChangeProposalWrapper as MCP

    dataset_urn = urns.dataset(_qualified(result.table))
    assertion_urn = _assertion_urn(result)

    graph.emit(
        [
            MCP(
                entityUrn=assertion_urn,
                aspect=models.AssertionInfoClass(
                    type=models.AssertionTypeClass.CUSTOM,
                    description=result.assertion_description,
                    customAssertion=models.CustomAssertionInfoClass(
                        type=result.rule,
                        entity=dataset_urn,
                        field=urns.field(_qualified(result.table), result.column)
                        if result.column
                        else None,
                        logic=result.sql,
                    ),
                    source=models.AssertionSourceClass(
                        type=models.AssertionSourceTypeClass.EXTERNAL
                    ),
                    lastUpdated=models.AuditStampClass(
                        time=int(time.time() * 1000), actor=config.ACTOR
                    ),
                    customProperties={
                        "lockout.rule": result.rule,
                        "lockout.table": result.table,
                        "lockout.column": result.column or "",
                    },
                ),
            )
        ]
    )
    return assertion_urn


def _assertion_urn(result: RuleResult) -> str:
    """Stable URN so re-arming updates rather than duplicates."""
    slug = f"{result.table}-{result.column}-{result.rule}".lower()
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug)
    return f"urn:li:assertion:lockout-{slug}"


def report(assertion_urn: str, result: RuleResult) -> bool:
    """Record a run result.

    Goes straight to the aspect path. `reportAssertionResult` is unusable here for
    three independent reasons, all verified against OSS GMS v1.5.0.6 and documented in
    docs/UPSTREAM_PRS.md:

      1. 403 Unauthorized on a freshly booted quickstart;
      2. the SDK helper sends a `severity` field the OSS schema does not define;
      3. it rejects column-scoped assertions outright, because `asserteeUrn` resolves
         to a `schemaField` while `assertionRunEvent` requires a `dataset`.

    Emitting the aspect sidesteps all three and is what actually renders run history.
    """
    return _report_via_aspect(assertion_urn, result)


def _report_via_aspect(assertion_urn: str, result: RuleResult) -> bool:
    """Emit `assertionRunEvent` directly, with a *dataset* assertee.

    `upsertCustomAssertion(fieldPath=...)` produces an assertion whose `asserteeUrn` is
    the schemaField, but the `assertionRunEvent` aspect validator requires
    `Required: [dataset]` on that same field:

        Invalid entity type urn validation failure (Required: [dataset]).
        Path: /asserteeUrn
        Urn: urn:li:schemaField:(urn:li:dataset:(...,main.staging_trips,PROD),trip_date)

    So through the public mutation, a column-scoped custom assertion can never have a
    run result recorded. Emitting the aspect ourselves with the parent dataset as the
    assertee satisfies the validator and keeps the column scoping on the assertion
    definition, which is where it belongs. Filed upstream — see docs/UPSTREAM_PRS.md.
    """
    import datahub.metadata.schema_classes as models
    from datahub.emitter.mcp import MetadataChangeProposalWrapper as MCP

    now = int(time.time() * 1000)
    native = {k: str(v) for k, v in result.native.items()}
    native["observed"] = str(result.observed)
    native["threshold"] = str(result.threshold)

    graph.emit(
        [
            MCP(
                entityUrn=assertion_urn,
                aspect=models.AssertionRunEventClass(
                    timestampMillis=now,
                    runId=f"lockout-{now}",
                    assertionUrn=assertion_urn,
                    asserteeUrn=urns.dataset(_qualified(result.table)),
                    status=models.AssertionRunStatusClass.COMPLETE,
                    result=models.AssertionResultClass(
                        type=(
                            models.AssertionResultTypeClass.SUCCESS
                            if result.passed
                            else models.AssertionResultTypeClass.FAILURE
                        ),
                        nativeResults=native,
                    ),
                ),
            )
        ]
    )
    return True


def arm(results: list[RuleResult]) -> list[dict]:
    """Upsert every rule as an assertion and record its current run result."""
    armed = []
    for r in results:
        urn = upsert(r)
        report(urn, r)
        armed.append(
            {
                "assertion_urn": urn,
                "rule": r.rule,
                "table": r.table,
                "column": r.column,
                "passed": r.passed,
                "observed": r.observed,
                "threshold": r.threshold,
            }
        )
    return armed


def _qualified(table: str) -> str:
    """`staging_trips` -> `main.staging_trips` (the name the URN uses)."""
    if "." in table:
        return table
    for candidate in (config.RAW_TABLE, config.STAGING_TABLE, config.MART_TABLE):
        if candidate.split(".")[-1] == table:
            return candidate
    return table
