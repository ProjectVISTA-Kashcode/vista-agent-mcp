#!/usr/bin/env bash
# Start the two fake VISTA analyzers used for local end-to-end testing.
#
# They implement the STANDARD analyzer contract (GET <base>/discover, POST <base>/run) and share
# ZERO code with the orchestrator — proving the orchestrator is truly generic.
#
# NOTE: the fakes use FastAPI, which lives in the *LogV* venv (the VISTA-MCP venv doesn't have it).
# Point PYBIN at any python that has fastapi + uvicorn + python-multipart.
#
# Usage:
#   ./fakes/run_fakes.sh            # start perf (8811) + eos (8812) in the background
#   ./fakes/run_fakes.sh stop       # stop them
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYBIN="${PYBIN:-/home/jaskirat/log_visio/LogAssist/backend/.venv/bin/python}"
LOGDIR="${LOGDIR:-/tmp}"

if [[ "${1:-start}" == "stop" ]]; then
  pkill -f "fakes/fake_analyzer.py" && echo "stopped fake analyzers" || echo "no fake analyzers running"
  exit 0
fi

ANALYZER_ID=perf ANALYZER_TITLE="Performance Analyzer" BASE=/perf PORT=8811 \
  WHEN="Use for FortiGate performance questions — CPU/memory pressure, conserve mode, session or resource exhaustion. Do NOT use for routine SLA/traffic/event log questions." \
  nohup "$PYBIN" "$HERE/fake_analyzer.py" > "$LOGDIR/fake_perf.log" 2>&1 &
echo "perf → http://127.0.0.1:8811/perf  (pid $!)"

ANALYZER_ID=eos ANALYZER_TITLE="End-of-Support Lookup" BASE=/eos PORT=8812 \
  WHEN="Use for FortiGate hardware end-of-support / end-of-life / lifecycle date questions about a model or serial. Do NOT use for log/SLA/performance questions." \
  nohup "$PYBIN" "$HERE/fake_analyzer.py" > "$LOGDIR/fake_eos.log" 2>&1 &
echo "eos  → http://127.0.0.1:8812/eos   (pid $!)"

echo "fake analyzers started (logs in $LOGDIR/fake_*.log)"
