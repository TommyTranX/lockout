# Contributions back to DataHub

Every item here was hit while building Lockout. None was found by going looking for
something to file, and each one has a reproduction, a diagnosis, and a workaround that
is already live in this repo.

Status is kept honest, including when it goes against me: **one of these was closed as a
duplicate of a bug DataHub had already fixed**, and that is recorded below rather than
quietly dropped.

| # | Status |
|---|---|
| 1 & 2 — assertion reporting | **CLOSED as duplicate** of [#18674](https://github.com/datahub-project/datahub/issues/18674), fixed on `master` by [#18697](https://github.com/datahub-project/datahub/pull/18697) three days before I filed. Real bug, confirmed by a maintainer, but not a novel find. Still affects `quickstart`. |
| 3 & 4 — Actions registry port, events/poll 500 | **OPEN** — [#18786](https://github.com/datahub-project/datahub/issues/18786) |
| 5 — sample data vs its README | **OPEN** — [static-assets#222](https://github.com/datahub-project/static-assets/issues/222) |
| 6 — search-based URN discovery | not filed |

---

## 1. `report_assertion_result()` is unusable against open-source DataHub

**Repo:** `acryldata/mcp-server-datahub` / `datahub-project/datahub` (Python SDK)
**Severity:** blocks the whole assertion-reporting path on the version `quickstart` ships
**Status:** **CLOSED — duplicate.** Reported in
https://github.com/datahub-project/datahub/issues/18785; maintainer confirmed it is real
on v1.5.0.6 and pointed to severity support landing in
https://github.com/datahub-project/datahub/pull/17335, present on current `master`.

`DataHubGraph.report_assertion_result()` in `acryl-datahub==1.6.0.16` sends a `severity`
field in `AssertionResultInput` and references the type `AssertionResultSeverity`.
Neither exists in the open-source GraphQL schema, so every call fails validation:

```
Validation error (UnknownType): Unknown type 'AssertionResultSeverity'
Validation error (WrongType@[reportAssertionResult]): argument 'result' contains a
  field not in 'AssertionResultInput': 'severity'
```

**Reproduce** — against `datahub docker quickstart` (GMS v1.5.0.6):

```python
graph = DataHubGraph(DatahubClientConfig(server="http://localhost:8080"))
res = graph.upsert_custom_assertion(urn=None, entity_urn=DATASET, type="FRESHNESS",
                                    description="x", platform_name="Test")
graph.report_assertion_result(urn=res["urn"], timestamp_millis=..., type="FAILURE")
# -> GraphError: Unknown type 'AssertionResultSeverity'
```

**Fix:** omit `severity` when the server schema does not declare it, or gate the field
on server version the way other version-sensitive fields are handled.

**Workaround in this repo:** `src/lockout/writeback/assertions.py` issues the mutation
directly without `severity`.

---

## 2. Column-scoped custom assertions can never have run results reported

**Repo:** `datahub-project/datahub`
**Severity:** a documented feature combination that cannot work on v1.5.0.6
**Status:** **CLOSED — duplicate of
[#18674](https://github.com/datahub-project/datahub/issues/18674)**, fixed on `master` by
[#18697](https://github.com/datahub-project/datahub/pull/18697) (merged 2026-07-28,
three days before I filed [#18785](https://github.com/datahub-project/datahub/issues/18785)).

Maintainer response, verbatim: *"this is a real bug on v1.5.0.6, but it's already fixed on
master … That change stops `reportAssertionResult` from walking the `Asserts` graph (which
can return a `schemaField` URN for column-scoped custom assertions) and instead resolves
the assertee from `AssertionInfo` / `customAssertion.entity` (always the parent dataset)."*

**Why the workaround stays in this repo:** `datahub docker quickstart` still pulls
**v1.5.0.6**, which predates the fix. Anyone running the documented quickstart — including
a hackathon judge — hits it. Lockout's aspect-emission path is version-independent and
works on both sides of the fix.

`upsertCustomAssertion(fieldPath: "trip_date")` creates an assertion whose `asserteeUrn`
is the **schemaField** URN. The `assertionRunEvent` aspect validator requires that same
field to be a **dataset**:

```
Failed to validate MCP due to: AspectValidationException(
  aspectName=assertionRunEvent, subType=VALIDATION,
  msg=Invalid entity type urn validation failure (Required: [dataset]).
      Path: /asserteeUrn
      Urn: urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:sqlite,
           main.staging_trips,PROD),trip_date))
```

So through the public API a column-scoped custom assertion can be *created* but its
results can never be *reported* — the two halves of the feature disagree about what
`asserteeUrn` means.

**Fix:** `reportAssertionResult` should resolve a `schemaField` assertee to its parent
dataset before constructing the run event.

**Workaround in this repo:** `_report_via_aspect()` emits `assertionRunEvent` directly
with the parent dataset as assertee, preserving column scope on the definition.

---

## 3. The Actions quickstart points at a schema-registry port that is closed

**Repo:** `datahub-project/datahub` (docs + action templates)
**Severity:** every user following the tutorial against quickstart fails
**Status:** **FILED** — https://github.com/datahub-project/datahub/issues/18786

The Actions documentation and YAML templates default to
`${SCHEMA_REGISTRY_URL:-http://localhost:8081}`. A quickstart install serves the schema
registry from **inside GMS**, and nothing listens on 8081:

```
curl http://localhost:8081/subjects                      -> connection refused
curl http://localhost:8080/schema-registry/api/subjects  -> 200
  ["MetadataChangeProposal_v1-value","FailedMetadataChangeProposal_v1-value",
   "MetadataChangeLog_Versioned_v1-value","PlatformEvent_v1-value",
   "MetadataChangeEvent_v4-value","FailedMetadataChangeEvent_v4-value",
   "MetadataAuditEvent_v4-value","DataHubUpgradeHistory_v1-value"]
```

**Fix:** default to `http://localhost:8080/schema-registry/api` in the quickstart docs
and templates, or document the override prominently.

---

## 4. `GET /openapi/v1/events/poll` returns HTTP 500 on a default quickstart

**Repo:** `datahub-project/datahub`
**Severity:** the documented HTTP alternative to Kafka consumption is unusable
**Status:** **FILED** (reported within https://github.com/datahub-project/datahub/issues/18786)

```
GET /openapi/v1/events/poll               -> 500 {"error":"Internal server error occurred"}
GET /openapi/v1/events/poll?limit=1       -> 500
GET /openapi/v1/events/poll?offsetId=0&limit=1 -> 500
```

This matters for anyone who cannot reach Kafka directly — it is the sanctioned fallback
and it fails on the default local install.

---

## 5. `nyc-taxi` sample data does not match its own README

**Repo:** `datahub-project/static-assets`
**Severity:** documentation vs shipped data
**Status:** **FILED** — https://github.com/datahub-project/static-assets/issues/222

`datasets/nyc-taxi/README.md` documents the planted defects in `nyc_taxi_pipeline.db` as:

| Claimed | Actually shipped |
|---|---|
| "staging_trips stops **3 days** before raw_trips" | **9 days** — staging `MAX(trip_date)` is `2016-03-01`, raw `MAX(tpep_pickup_datetime)` is `2016-03-10` |
| "One day in the mart shows **0 trips**… `trip_count = 0`" | **No row has `trip_count = 0`.** The empty load presents as **2 rows** on `2016-02-25`, against a median daily volume of 2,257 |

Anyone writing a detector against the documented values (`lag == 3`, `trip_count == 0`)
will silently fail to catch the actual defect.

**Reproduce:**

```sql
SELECT MAX(trip_date) FROM staging_trips;              -- 2016-03-01
SELECT MAX(tpep_pickup_datetime) FROM raw_trips;       -- 2016-03-10 10:48:55
SELECT COUNT(*) FROM mart_daily_summary WHERE trip_count = 0;  -- 0
SELECT trip_date, trip_count FROM mart_daily_summary ORDER BY trip_count LIMIT 1;
                                                       -- 2016-02-25 | 2
```

**Fix:** correct the README, or regenerate the database to match it.

---

## 6. `add_lineage.py` / `add_metadata.py` resolve URNs by search and fail after ingest

**Repo:** `datahub-project/static-assets`
**Severity:** the documented three-step quick start does not work as written
**Status:** _to file_

`discover_urns()` finds datasets via search. DataHub's search index is written
asynchronously by the MAE consumer, so immediately after a *successful* ingest the
script prints `No datasets found (run ingestion first)`.

**Fix:** construct URNs deterministically —
`builder.make_dataset_urn("sqlite", "main.raw_trips", "PROD")` — as this repo does in
`src/lockout/urns.py`, which is also enforced by a unit test.

---

## Also worth reporting: silent MAE-consumer stall

Not yet a filed issue because the trigger is not fully isolated, but recorded here
because it cost hours. After a failed ingestion through the Actions executor, GMS
entered a state where **writes succeeded and were readable by URN, while the search and
graph indexes silently stopped updating** — new entities never appeared in
`datasetindex_v2`, and new edges never appeared in `graph_service_v1`.

```
[Consumer clientId=consumer-generic-mae-consumer-job-client-5,
 groupId=generic-mae-consumer-job-client] Asynchronous auto-commit of offsets failed:
 ... consumer was kicked out of the group
ERROR c.l.m.s.e.update.BulkListener - Failed to feed bulk request ...
  type=version_conflict_engine_exception ... [datasetindex_v2]
```

`docker restart` did not recover it, and `datahub docker quickstart --restore-indices`
exited status 1. Only `nuke` + fresh `quickstart` did.

The lasting consequence for users is that there is **no surfaced signal** — the UI and
the API both look healthy. `lockout doctor` exists because of this.
