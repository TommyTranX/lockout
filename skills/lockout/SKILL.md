---
name: lockout-permit
description: Decide whether an ML model is safe to retrain, by resolving its features to source datasets through DataHub lineage and checking for failing assertions anywhere upstream. Use when asked "is it safe to retrain X", "why was this training run blocked", "what is upstream of this model", or when investigating a model whose inputs may have gone stale.
---

# Deciding whether a model is safe to retrain

A model is unsafe to retrain when a dataset it transitively depends on has failing data
quality assertions. The dependency is usually **not** visible from the training job —
the job knows its own URN and nothing else, so the answer has to come from the graph.

## Procedure

1. **Resolve what the model consumes.** Read `mlModel.properties.mlFeatures`.
   Do not guess from names, and do not resolve URNs by search — DataHub's search index
   is populated asynchronously and returns nothing while it lags.

2. **Resolve each feature to its source datasets.** Read
   `mlFeature.properties.sources`. Note that `sources` holds **dataset** URNs, never
   `schemaField` URNs — the field's relationship annotation is `entityTypes: ["dataset"]`
   and GMS rejects anything else. If the feature carries a `lockout.source_column`
   custom property, that is the specific column it reads.

3. **Walk upstream from every source dataset**, with `get_lineage` or
   `searchAcrossLineage(direction: UPSTREAM)`. The blocking fact is frequently two or
   three hops up, in a table the model's features never name directly.

4. **Check assertions on every reachable dataset**, not only the direct sources. Read
   the most recent `runEvents` for each; an assertion that exists but has never run
   tells you nothing.

5. **Decide, and show the path.** If any reachable dataset has a failing assertion, the
   model is unsafe. Report:
   - which rule failed and on which column,
   - the observed value against its threshold,
   - the full lineage path from the model to the failing dataset,
   - how many hops away it was.

   A verdict without the path is not auditable and should not be trusted.

## What to write back

Record the decision so the catalog remembers it:

- an `incident` on the **dataset** — not the model, which has no `incidentsSummary`
  aspect to roll it up;
- a `dataProcessInstance` with result `SKIPPED` for the run that did not happen;
- a structured property (`lockout.trainingState = LOCKED`) on the model and the
  implicated datasets;
- an `institutionalMemory` element on the model, so the next person investigating sees
  the reasoning.

## Gotchas that will cost you an hour

- `graph.report_assertion_result()` fails on open-source DataHub: the SDK sends a
  `severity` field typed `AssertionResultSeverity` that the OSS GraphQL schema does not
  define. Issue the `reportAssertionResult` mutation directly without it.
- A **column-scoped** custom assertion cannot have results reported at all through the
  public mutation: `upsertCustomAssertion(fieldPath=…)` sets `asserteeUrn` to the
  `schemaField`, but `assertionRunEvent` validation requires a `dataset`. Emit the
  `assertionRunEvent` aspect directly with the parent dataset as assertee.
- `upsertCustomAssertion` returns before the assertion is resolvable; an immediate
  report fails with "does not exist or is not associated with any entity". Retry.
- Every custom assertion reports `type: CUSTOM`, so the actual rule must be recovered
  from the description you wrote.
- `MLModelProperties.deployments` exists as an aspect field but is absent from the OSS
  GraphQL type.

## Related

Reference implementation: https://github.com/TommyTranX/lockout —
`src/lockout/policy/decision.py` is this procedure in code.
