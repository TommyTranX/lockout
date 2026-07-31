# Limitations, and what this project does not claim

Stated plainly, because a safety tool that oversells itself is worse than none.

## The counterfactual is a null result, and that is the finding

`make counterfactual` trains the same model on DataHub's clean and stale taxi databases
and scores both on the same holdout. **The MAE delta is exactly 0.00.**

That is not a disappointing result being buried — it is reported in
`examples/counterfactual.json` and explained there. Every day the stale pipeline is
missing falls inside the holdout window, so once holdout dates are excluded from both
arms the two training sets are identical and the models are equivalent. At this
dataset's granularity — 42 daily rows across fifteen months — a degradation claim is
not supportable, so none is made.

**What is measured, and is the real harm:**

| | |
|---|---|
| Staging rows silently dropped | **39,640 of 248,315 — 15.96%** |
| Recency lost | **9 days** |
| Worst partition | **2 rows**, against a median of 2,257 |
| Both training runs | complete successfully and report plausible metrics |

That last row is the point. Nothing downstream looks wrong. Lockout's claim is not that
it produces a better model; it is that it refuses to produce a model **nobody can tell
is broken**.

If you want a headline metric delta, this dataset cannot honestly provide one. A larger
dataset with dense daily rows would.

## Scope

- **The rules are deliberately few.** Freshness, volume collapse, and null rate. They
  are not a data-quality suite and are not trying to be; they exist to give the gate
  something real to reason about.
- **Thresholds are static constants** in `config.py`, not learned baselines. A
  production version would derive them from history.
- **Freshness is measured relative to the upstream table**, not wall-clock. The sample
  data is from 2016, so an absolute check would flag everything and prove nothing.
- **The training job is small on purpose** — a 200-tree GBR over daily aggregates. The
  interesting part is that it asks permission, not that it is a good forecaster.
- **The demo covers one model and one pipeline.** Nothing in the design is specific to
  them, but nothing has been tested at scale either.

## Environment

- Verified against **DataHub OSS quickstart, GMS v1.5.0.6**, `acryl-datahub==1.6.0.16`,
  Python 3.11, on colima (4 CPU / 9.3 GB). Not tested against DataHub Cloud.
- **DataHub can silently stop indexing.** Writes keep succeeding and stay readable by
  URN while the search and graph indexes go stale, because the MAE consumer drops out
  of its Kafka consumer group. `docker restart` does not fix it and
  `--restore-indices` failed; only `datahub docker nuke` + a fresh `quickstart` did.
  Run `lockout doctor` before trusting an empty-looking result.
- **`aiAgent` and `api` entities are unavailable.** DataHub's agent-registry entities
  were added to `entity-registry.yml` on 2026-07-19 but are in **no released version** —
  not v1.6.0, not v1.6.0.1rc1. Registering Lockout itself as an `aiAgent` would have
  been a natural fit; it 422s on every server a judge can actually run.
- **Structured property badges** aren't set: `StructuredPropertyDefinitionClass` in this
  SDK has no `settings` kwarg, so `showAsAssetBadge` cannot be enabled from Python.
- **Redefining a structured property with narrower `allowedValues` is rejected**
  ("Cannot restrict values that were previously allowed"). If you seed, edit the
  definition, and re-seed, use a new qualified name.

## Known rough edges

- Every custom assertion reports `type: CUSTOM`, so the rule name is recovered from the
  description Lockout wrote. It works, but it is a string convention, not a contract.
- `lockout watch` re-evaluates the full rule set on any relevant change rather than only
  the assertions bound to the changed column. Correct, but coarser than intended.
- Incidents attach to datasets, never models — `mlModel` has no `incidentsSummary`
  aspect, so a model-level incident would not roll up anywhere.
- The permit is advisory. A job can ignore it, and `--no-lockout` does exactly that on
  purpose, so the counterfactual can be measured.
