#!/bin/sh
# Exit 0 when DATA_DIR/flag is 1. Same idea as a Grok Bot watcher tick.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT/data}"
FLAG="$DATA_DIR/flag"
if [ -f "$FLAG" ]; then
  value="$(tr -d '[:space:]' < "$FLAG")"
  if [ "$value" = "1" ]; then
    exit 0
  fi
fi
exit 1
