# Lockout

**Lockout/tagout for data pipelines.** An agent that refuses to let a model train on
data that quietly went bad — and records *why* in DataHub, so the catalog remembers.

Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/) —
category **Production ML Agents**.

> **Disclosure, up front.** The pipeline defect this demo catches is **not planted by
> me** — it ships inside DataHub's own `nyc_taxi_pipeline.db` sample, whose README says
> the staleness is *"invisible in metadata — you can only detect it by querying the
> actual data timestamps."* The **ML entities are mine**: no DataHub sample dataset
> ships an `mlModel`, `mlFeature` or `mlFeatureTable` anywhere, so `lockout seed`
> creates them. That seeder is being contributed upstream so the next person doesn't
> have to write one ([docs/UPSTREAM_PRS.md](docs/UPSTREAM_PRS.md)).

---

## The problem

A training job runs on schedule. The upstream table looks perfect in the catalog — it
has an owner, a description, tags, and full lineage. Every job in the pipeline is green.

The table is also nine days stale, and one of its partitions loaded two rows instead of
two thousand. Nothing in the catalog can tell you that, because *ingestion succeeded*.
The model trains anyway, ships, and degrades silently until someone notices a dashboard
looks wrong a week later.

## What Lockout does

The training job asks for a permit before it starts, and passes **only its own model
URN**. It never names a table. Lockout answers by walking DataHub's graph:

```
mlModel ──Consumes──▶ mlFeature ──DerivedFrom──▶ dataset ──UpstreamLineage──▶ dataset
   │                      │                          │
   │                      │                          └── any failing assertions here?
   │                      └── which column does this feature actually read?
   └── DENY, and name the path that caused it
```

That indirection is the whole point. The blocking fact can sit several hops from
anything the job knows about, so the decision has to come from the graph rather than
from configuration — and the denial has to be able to prove where it came from.

```
PERMIT DENIED   for urn:li:mlModel:(urn:li:dataPlatform:lockout,taxi_demand_v1,PROD)

  The training job did not name a table. It named itself.
  Lockout resolved its features to source datasets and walked upstream:

  ✗ FRESHNESS failed on main.staging_trips.trip_date (1 hop upstream)
      staging_trips has data through 2016-03-01, but raw_trips has data through
      2016-03-10 — 9 days behind (limit 2)
      observed: lag_days=9  downstream_max=2016-03-01  upstream_max=2016-03-10
      lineage path:
        mlModel:taxi_demand_v1
          └─▶ mlFeature:trips_7d
            └─▶ dataset:main.staging_trips
      features affected: trips_7d
      assertion: urn:li:assertion:4be40bde-8885-4311-bb8a-0accacfbd217

  ✗ VOLUME failed on main.staging_trips.trip_date (1 hop upstream)
      staging_trips loaded 2 rows on 2016-02-25, against a median daily volume of
      2257 — below the floor of 225

  decided in 773 ms
```

Every number above is measured from the shipped sample database, not illustrative.

## What the block is worth — measured, including a null result

`make counterfactual` trains the same model on DataHub's **clean** and **stale** taxi
databases and scores both on the same holdout.

**The MAE delta is exactly 0.00**, and that is reported rather than buried. Every day
the stale pipeline is missing falls inside the holdout window, so once holdout dates are
excluded from both arms the training sets are identical. At 42 daily rows across fifteen
months this dataset cannot support a degradation claim, so none is made.

What *is* measured is the actual harm:

| | |
|---|---|
| Staging rows silently dropped | **39,640 of 248,315 — 15.96%** |
| Recency lost | **9 days** |
| Worst partition | **2 rows**, against a median of 2,257 |
| Both training runs | complete successfully, reporting plausible metrics |

That last row is the whole point. Nothing downstream looks wrong. Lockout does not claim
to produce a better model — it refuses to produce a model **nobody can tell is broken**.

Raw numbers in [`examples/counterfactual.json`](examples/counterfactual.json); reasoning
in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## What it writes back

A block that only exists in a terminal is a private opinion. Lockout records the
decision in the graph:

| Written | Where it shows up |
|---|---|
| `assertion` + `assertionRunEvent` history | dataset → Validation tab |
| `incident` (ACTIVE → RESOLVED) | dataset → Incidents |
| `dataProcessInstance` with result `SKIPPED` | the run that never happened, and why |
| `lockout.trainingState` = `LOCKED` / `CLEAR` | structured property on model + datasets |
| `institutionalMemory` receipt | the model page, for whoever investigates next |
| `mlTrainingRunProperties` | metrics, on runs that were actually allowed to proceed |

Open-source DataHub models assertions well but **ships no scheduler** — nothing
evaluates them on a cadence. For the checks it arms, Lockout is that scheduler.

## Real-time, not polling

Lockout subscribes to DataHub's own `MetadataChangeLog` Kafka topic. When an ingestion
run touches an armed dataset, the relevant assertions are re-evaluated immediately.

```bash
lockout watch
```

There is no polling loop in this program. The wire format is Confluent-framed Avro and
the schema registry lives *inside GMS* at `:8080/schema-registry/api` — not the
conventional `:8081`, which is why following DataHub's Actions tutorial against a
quickstart install fails. That fix is one of the upstream contributions below.

---

## Quick start

Requires Docker (or colima) and Python 3.11.

```bash
git clone https://github.com/TommyTranX/lockout && cd lockout
make judge
```

`make judge` boots DataHub, fetches the sample databases, seeds the graph, arms the
assertions, and runs the blocked-vs-forced comparison end to end. Verified from a cold
clone against a nuked instance — step-by-step timings in
[docs/JUDGE_RUNBOOK.md](docs/JUDGE_RUNBOOK.md).

Step by step instead:

```bash
make data                 # fetch DataHub's nyc_taxi_pipeline.db sample (~86 MB)
datahub docker quickstart # DataHub OSS on localhost:9002 (datahub / datahub)
make install

lockout doctor            # verify GMS + the graph index are actually live
datahub ingest -c recipes/taxi.yml
lockout seed              # ML subgraph, lineage, structured properties
lockout arm               # evaluate rules -> assertions in DataHub
lockout permit            # ask for a training permit  -> DENIED
lockout train             # the job asks first, and does not start
lockout train --no-lockout # force it through, to measure what was avoided
```

### `lockout doctor`

DataHub can enter a state where **writes succeed but the search and graph indexes
silently stop updating** — the MAE consumer drops out of its Kafka consumer group and
nothing surfaces the failure. Everything then looks empty for no visible reason.
`lockout doctor` probes that path explicitly so you find out in two seconds rather than
after an hour of confusion. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

---

## How it's built

```
src/lockout/
├── cli.py                  seed | arm | permit | train | watch | status | doctor
├── config.py               thresholds, URNs, feature→column bindings
├── urns.py                 deterministic URN construction — never search
├── graph.py                DataHub client + the lineage/assertion reads the gate needs
├── policy/
│   ├── rules.py            freshness / volume / null-rate evaluators (plain SQL)
│   └── decision.py         ← THE GATE: transitive resolution over lineage
├── events/consumer.py      MetadataChangeLog subscription + Confluent Avro decode
├── seed/ml_subgraph.py     the ML entities DataHub's samples don't ship
├── writeback/
│   ├── assertions.py       assertions + run history (with two OSS workarounds)
│   └── state.py            incidents, run records, state, receipts
└── training/job.py         the model, and the counterfactual
```

**No model is in the decision path.** Whether data is bad, and whether a run is blocked,
are decided by deterministic SQL and comparisons. An LLM writes one paragraph of
incident narrative and nothing else. A safety interlock that could be talked out of its
decision is not a safety interlock, and numbers on screen have to be reproducible.

**URNs are built, never searched.** DataHub's search index is populated asynchronously;
when it lags or stalls, a search-based lookup returns nothing and the caller silently
does the wrong thing. It is the documented cause of `static-assets/add_lineage.py`
printing *"No datasets found"* right after a successful ingest.

---

## Contributions back to DataHub

Every one of these came out of building this, not from hunting for something to file.
Details and status in [docs/UPSTREAM_PRS.md](docs/UPSTREAM_PRS.md).

| # | Repo | What |
|---|---|---|
| 1 | `acryldata/mcp-server-datahub` | `report_assertion_result()` is unusable on OSS — the SDK sends a `severity` field typed `AssertionResultSeverity`, which the OSS GraphQL schema does not define, so every call fails validation |
| 2 | `datahub-project/datahub` | Column-scoped custom assertions can never have results reported: `upsertCustomAssertion(fieldPath=…)` sets `asserteeUrn` to a `schemaField`, but `assertionRunEvent` validation requires a `dataset` |
| 3 | `datahub-project/datahub` | The Actions quickstart points at `:8081` for the schema registry; quickstart serves it from `:8080/schema-registry/api` and `:8081` is closed |
| 4 | `datahub-project/datahub` | `GET /openapi/v1/events/poll` returns HTTP 500 on a default quickstart — the documented HTTP alternative to Kafka consumption |
| 5 | `datahub-project/static-assets` | `nyc-taxi/README.md` documents a 3-day lag and a `trip_count = 0` day; the shipped database has a **9-day** lag and **no zero-count day** (the empty load presents as 2 rows) |
| 6 | `datahub-project/static-assets` | `add_lineage.py` / `add_metadata.py` resolve URNs by search and fail immediately after a successful ingest |

---

## Licence

Apache-2.0. See [LICENSE](LICENSE).
