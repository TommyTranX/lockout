#!/usr/bin/env bash
# A paced walkthrough for screen recording.
#
# Commands normally finish faster than a viewer can read. This runs the real demo -
# nothing here is faked or pre-recorded - but holds each beat long enough to film,
# and prints a section title before each one so the video is self-narrating even
# with the sound off.
#
#   ./scripts/film.sh          # paced for recording (~2m15s of terminal)
#   ./scripts/film.sh --fast   # no pauses, for checking it still works
#
# Record at 1920x1080 with a large terminal font. Everything below is real output.

set -uo pipefail
cd "$(dirname "$0")/.."

LOCKOUT=".venv/bin/lockout"
PAUSE=${PAUSE:-6}
[[ "${1:-}" == "--fast" ]] && PAUSE=0

export DATAHUB_TELEMETRY_ENABLED=false   # or every call stalls ~54s on camera

bold() { printf "\n\033[1;36m%s\033[0m\n\n" "$1"; }
hold() { [[ "$PAUSE" != "0" ]] && sleep "$PAUSE"; }

clear
bold "1/5  The catalog says this table is fine."
echo "     Open http://localhost:9002 -> main.staging_trips"
echo "     Owner, description, tags, full lineage. All green."
echo
echo "     It is also 9 days stale, and one day loaded 2 rows instead of 2,257."
echo "     DataHub's own README for this sample says the staleness is"
echo "     \"invisible in metadata - you can only detect it by querying the data.\""
hold; hold

bold "2/5  Lockout arms the checks. DataHub ships no assertion scheduler."
$LOCKOUT arm
hold; hold

bold "3/5  The training job asks whether it may start."
echo "     It passes only its own model URN. It never names a table."
echo
hold
$LOCKOUT permit
hold; hold

bold "4/5  The decision is written back into the graph."
echo "     Refresh the UI: an ACTIVE incident on staging_trips, the failing"
echo "     assertions with run history, a SKIPPED run, a receipt on the model."
echo
$LOCKOUT train 2>&1 | tail -12
hold; hold

bold "5/5  What the block was worth - measured, including a null result."
PYTHONPATH=src .venv/bin/python - <<'PY'
import json, pathlib
from lockout.training import counterfactual
d = counterfactual.run()
loss = d["silent_data_loss"]
print(f"  staging rows, clean pipeline : {loss['staging_rows_clean']:,}")
print(f"  staging rows, stale pipeline : {loss['staging_rows_stale']:,}")
print(f"  silently dropped             : {loss['rows_missing']:,}  ({loss['rows_missing_pct']}%)")
print(f"  recency lost                 : 9 days")
print()
print(f"  MAE delta between the two    : {d['delta']['mae_absolute']}   <- exactly zero")
print()
print("  Every day the stale pipeline is missing falls inside the holdout window,")
print("  so both arms train on identical rows. This dataset cannot support a")
print("  degradation claim, so none is made.")
print()
print("  The harm is that BOTH runs complete successfully, reporting plausible")
print("  metrics, having silently lost 16% of the training data.")
print("  Lockout does not promise a better model. It refuses to produce one")
print("  that nobody can tell is broken.")
PY
hold; hold

bold "Filed upstream while building this"
echo "  datahub#18785       column-scoped assertions can never report results"
echo "  datahub#18786       Actions quickstart points at the wrong registry port"
echo "  static-assets#222   sample README does not match the shipped database"
echo
echo "  github.com/TommyTranX/lockout   -   Apache-2.0   -   make judge"
echo
