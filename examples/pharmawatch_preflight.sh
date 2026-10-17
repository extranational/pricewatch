#!/bin/bash
# Step-0 preflight for a PharmaWatch run.
#
# Call this BEFORE the pipeline spends anything. It reads the public OpenRouter catalog, so it
# needs no API key and costs nothing:
#
#   0 -> every watched endpoint still matches the price we accepted: run
#   1 -> a price/discount moved: HOLD, and re-accept deliberately (`pricewatch accept --job pharmawatch`)
#   2 -> the catalog could not be read: HOLD by default (add --on-unreachable pass to override)
#
# A hold is a hold, not a failure: nothing is wrong with the job, the price just needs a decision.
set -u

REPO="${PRICEWATCH_REPO:-$HOME/repos/pricewatch}"
JOB="${1:-pharmawatch}"

cd "$REPO" || { echo "pricewatch: repo not found at $REPO"; exit 2; }

python3 -m pricewatch.cli check --job "$JOB"
status=$?

case "$status" in
  0) echo "pricewatch: $JOB clear to run" ;;
  1) echo "pricewatch: HOLD — refusing to start $JOB until the new price is accepted" ;;
  2) echo "pricewatch: HOLD — could not verify pricing for $JOB" ;;
esac
exit "$status"
