# Judge runbook

Timings are from an actual cold-clone rehearsal: fresh `git clone`, `datahub docker
nuke`, then every step below in order, on an M-series Mac (colima, 4 CPU / 9.3 GB).

## The fast path

```bash
git clone https://github.com/TommyTranX/lockout && cd lockout
make judge
```

## Step by step, with real timings

| Step | Command | Time | What you should see |
|---|---|---|---|
| 1 | `make install` | ~60 s | uv venv + dependencies |
| 2 | `make data` | ~6 s | two ~86 MB sqlite files in `data/` |
| 3 | `make quickstart` | 2–6 min | six healthy containers; UI on :9002 (`datahub`/`datahub`) |
| 4 | `make ingest` | ~3 s | `produced 35 events` |
| 5 | `make seed` | ~5 s | `structured_properties 2 · lineage 2 · ml_entities 11` |
| 6 | `make arm` | ~10 s | a table of 5 assertions: **FRESHNESS and VOLUME FAIL**, three null-rate checks PASS |
| 7 | `make permit` | ~1–3 s | **PERMIT DENIED**, with the lineage path printed |
| 8 | `make demo` | ~30 s | blocked run, forced run, counterfactual |

> Step 3 dominates. If DataHub is already running, `make quickstart` is a no-op.
> Allow ~60 s after step 5 before step 7 — DataHub indexes lineage asynchronously.

## What to look at in the UI (localhost:9002, `datahub` / `datahub`)

1. **`main.staging_trips` → Validation** — five assertions, each with run history.
   Freshness and Volume are red.
2. **`main.staging_trips` → Incidents** — one ACTIVE incident,
   *"Training blocked: main.staging_trips.trip_date"*.
3. **`taxi_demand_v1` (ML Model) → Lineage** — model ← features ← `staging_trips` ←
   `raw_trips`. This is the path the gate walks.
4. **`taxi_demand_v1` → Documentation** — the decision receipt.

## The one command that matters

```bash
lockout permit
```

The training job passes **only its own model URN**. It never names a table. Everything
in the denial — the failing rule, the column, the observed values, the hop count and the
path — is resolved from the DataHub graph.

## If something looks empty

```bash
lockout doctor
```

DataHub can enter a state where **writes succeed but the search and graph indexes
silently stop updating** (the MAE consumer drops out of its Kafka consumer group). The
UI and API both look healthy; results are just missing. `doctor` probes that path
explicitly. The reliable recovery is:

```bash
datahub docker nuke && make quickstart && make ingest && make seed && make arm
```

`--restore-indices` did not recover it in testing, and neither did restarting GMS.

## Verifying without running anything

Everything the demo claims is committed:

| File | Contains |
|---|---|
| `examples/03-rule-evaluations.json` | every rule's observed value, threshold, and the SQL that produced it |
| `examples/04-permit-denied.json` | the full permit, with evidence and lineage paths |
| `examples/counterfactual.json` | clean-vs-stale comparison, **including a null result and why** |
| `examples/02-mcl-watch.log` | the live MetadataChangeLog reaction |
| `examples/blocked-run/`, `examples/forced-run/` | the two training outcomes |

## Tests

```bash
make test    # 8 tests, no DataHub required, ~0.2 s
```

One of them asserts that **no module in the package ever resolves a URN by search** —
the property that keeps Lockout working when DataHub's index is stale.
