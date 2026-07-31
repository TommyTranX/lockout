"""Write assertions and their run history back into DataHub.

Open-source DataHub models assertions well but ships no scheduler — nothing evaluates
them on a cadence. Lockout is that scheduler for the checks it arms.

Two workarounds live here, both verified against GMS v1.5.0.6:

1. `DataHubGraph.report_assertion_result()` is unusable on open source. The SDK
   (acryl-datahub 1.6.0.16) sends a `severity` field typed `AssertionResultSeverity`,
   which the OSS GraphQL schema does not define, so every call fails validation:

       Unknown type 'AssertionResultSeverity'
       argument 'result' contains a field not in 'AssertionResultInput': 'severity'

   We issue the mutation directly without `severity`. Filed upstream — see
   docs/UPSTREAM_PRS.md.

2. `upsertCustomAssertion` returns before the assertion is resolvable, so an immediate
   `reportAssertionResult` fails with "does not exist or is not associated with any
   entity" even though it does exist. We retry with backoff.
"""

from __future__ import annotations

import time

from lockout import config, graph, urns
from lockout.policy.rules import RuleResult

_UPSERT = """
mutation($entityUrn:String!, $type:String!, $description:String!, $platform:String!,
         $field:String, $logic:String){
  upsertCustomAssertion(input:{
    entityUrn:$entityUrn, type:$type, description:$description,
    platform:{ name:$platform }, fieldPath:$field, logic:$logic
  }){ urn }
}
"""

_REPORT = """
mutation($urn:String!, $ts:Long!, $type:AssertionResultType!,
         $props:[StringMapEntryInput!]){
  reportAssertionResult(urn:$urn, result:{
    timestampMillis:$ts, type:$type, properties:$props
  })
}
"""


def upsert(result: RuleResult) -> str:
    """Create or update the assertion corresponding to a rule, return its URN."""
    dataset_urn = urns.dataset(_qualified(result.table))
    data = graph.gql(
        _UPSERT,
        {
            "entityUrn": dataset_urn,
            "type": result.rule,
            "description": result.assertion_description,
            "platform": "Lockout",
            "field": result.column,
            "logic": result.sql,
        },
    )
    return data["upsertCustomAssertion"]["urn"]


def report(assertion_urn: str, result: RuleResult, retries: int = 6) -> bool:
    """Record a run result, retrying while the assertion settles.

    Falls back to emitting the `assertionRunEvent` aspect directly, because
    `reportAssertionResult` cannot service column-scoped assertions at all — see
    `_report_via_aspect`.
    """
    props = [{"key": k, "value": str(v)} for k, v in result.native.items()]
    props.append({"key": "observed", "value": str(result.observed)})
    props.append({"key": "threshold", "value": str(result.threshold)})

    last: Exception | None = None
    for attempt in range(retries):
        try:
            graph.gql(
                _REPORT,
                {
                    "urn": assertion_urn,
                    "ts": int(time.time() * 1000),
                    "type": "SUCCESS" if result.passed else "FAILURE",
                    "props": props,
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 - we genuinely want to retry anything
            last = exc
            message = str(exc)
            if "Invalid entity type urn validation failure" in message:
                # Column-scoped assertion: the mutation can never succeed. Not worth
                # retrying — go straight to the aspect path.
                return _report_via_aspect(assertion_urn, result)
            if "does not exist" not in message:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"could not report assertion result after {retries} tries: {last}")


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
