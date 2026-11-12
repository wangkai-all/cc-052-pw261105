#!/usr/bin/env bash
# Demonstrate a hard process kill at a configurable pause point, then restart
# and show that startup recovery finishes (or diagnoses) the release.
#
# A single server process performs register -> upload -> seal -> publish with
# the crash point armed for the whole flow, so every point (including the
# upload-phase and commit-phase points) is actually reached.
#
# Usage: ./scripts/crash_demo.sh [after_files_written|after_seal|after_move|before_commit|after_commit]
set -euo pipefail

POINT="${1:-after_move}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
DATA="$(mktemp -d /tmp/batch-desk-demo-XXXXXX)"
PORT="${PORT:-8011}"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in "$BACKEND/.venv/bin/python" "$REPO_ROOT/.venv/bin/python"; do
    if [[ -x "$candidate" ]]; then PYTHON="$candidate"; break; fi
  done
fi
PYTHON="${PYTHON:-python3}"

export BATCH_DESK_DATA_DIR="$DATA"
export BATCH_DESK_FRONTEND_DIR="$REPO_ROOT/frontend/dist"
export PYTHONPATH="$BACKEND"

BATCH="demo-$(date +%H%M%S)"
FILE_A="metrics/daily.csv"
FILE_B="notes/readme.txt"

start_server() {
  local crash_points="$1"
  BATCH_DESK_CRASH_POINTS="$crash_points" \
    "$PYTHON" -m uvicorn app.main:create_app --factory \
    --host 127.0.0.1 --port "$PORT" --log-level warning \
    >>"$DATA/demo.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 300); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  echo "server failed to start; see $DATA/demo.log" >&2
  tail -30 "$DATA/demo.log" >&2 || true
  exit 1
}

stop_server() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
}

trap 'stop_server' EXIT

cd "$BACKEND"
echo "== data dir: $DATA"
echo "== crash point: $POINT"

printf 'date,value\n2026-11-12,42\n' > "$DATA/f1"
printf 'release notes' > "$DATA/f2"
SIZE_A=$(wc -c < "$DATA/f1")
SIZE_B=$(wc -c < "$DATA/f2")

echo "-- one server, crash point armed for the entire flow"
start_server "$POINT"

curl -fsS -X POST "http://127.0.0.1:$PORT/api/batches" \
  -H 'Content-Type: application/json' \
  -d "{\"batch_id\":\"$BATCH\",\"files\":[{\"rel_path\":\"$FILE_A\",\"size\":$SIZE_A},{\"rel_path\":\"$FILE_B\",\"size\":$SIZE_B}]}" \
  >/dev/null
# Record publication intent first so recovery resumes even if the kill lands
# during uploads.
curl -sS -X POST "http://127.0.0.1:$PORT/api/batches/$BATCH/publish" \
  -H 'Content-Type: application/json' -d '{}' >/dev/null || true

set +e
curl -fsS -X POST "http://127.0.0.1:$PORT/api/batches/$BATCH/files" \
  -F "rel_path=$FILE_A" -F "file=@$DATA/f1" >/dev/null
STEP_STATUS=$?
if [[ $STEP_STATUS -eq 0 ]]; then
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/batches/$BATCH/files" \
    -F "rel_path=$FILE_B" -F "file=@$DATA/f2" >/dev/null
  STEP_STATUS=$?
fi
if [[ $STEP_STATUS -eq 0 ]]; then
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/batches/$BATCH/seal" \
    -H 'Content-Type: application/json' -d '{}' >/dev/null
  STEP_STATUS=$?
fi
if [[ $STEP_STATUS -eq 0 ]]; then
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/batches/$BATCH/publish" \
    -H 'Content-Type: application/json' -d '{}' >/dev/null
  STEP_STATUS=$?
fi
set -e
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
echo "-- crashed flow ended with curl status $STEP_STATUS (52/56 = process died at the pause point)"

echo "-- restarting without crash points; startup recovery runs"
start_server ""
RECOVERY="$(curl -fsS "http://127.0.0.1:$PORT/api/health" | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["startup_recovery"]["summary"]))')"
echo "-- startup recovery: $RECOVERY"

curl -fsS "http://127.0.0.1:$PORT/api/batches/$BATCH" | "$PYTHON" -c '
import json, sys
d = json.load(sys.stdin)
print("   phase=%s publish_state=%s downloadable=%s" % (d["phase"], d["publish_state"], d["downloadable"]))
print("   snapshots:", [s["stage"] for s in d["snapshots"]])
for f in d["files"]:
    print("   %s: %s sha256=%s..." % (f["rel_path"], f["state"], f["sha256"][:16]))
for x in d["diagnostics"]:
    print("   diagnostic: %s %s %s" % (x["code"], x["rel_path"], x["detail"]))
'

echo "-- verifying download bytes"
for path in "$FILE_A" "$FILE_B"; do
  printf '   %s -> ' "$path"
  curl -fsS "http://127.0.0.1:$PORT/api/downloads/$BATCH/files/$path" | sha256sum
done

stop_server
echo "== done. data preserved at $DATA"
