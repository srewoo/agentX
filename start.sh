#!/bin/bash
# agentX Backend — start / stop / restart / clean
#
# Usage:
#   ./start.sh            Start the backend (default)
#   ./start.sh start      Start the backend
#   ./start.sh stop       Stop any running backend
#   ./start.sh restart    Stop then start
#   ./start.sh clean      Wipe DB + cache, reinstall deps, then start fresh
#   ./start.sh status     Show whether the backend is running

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
PID_FILE="$BACKEND_DIR/.agentx.pid"
DB_FILE="$BACKEND_DIR/stockpilot.db"
PORT=8020

# ── Helpers ──────────────────────────────────────────────────

log()  { echo -e "\033[1;36m[agentX]\033[0m $*"; }
warn() { echo -e "\033[1;33m[agentX]\033[0m $*"; }
err()  { echo -e "\033[1;31m[agentX]\033[0m $*" >&2; }

NEEDS_INSTALL=0
ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    log "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    NEEDS_INSTALL=1   # fresh venv → deps definitely missing
  fi
  source "$VENV_DIR/bin/activate"
}

# Only (re)install when the venv was just created or the caller forces it with
# AGENTX_FORCE_INSTALL=1. Running `pip install` on every auto-start added tens
# of seconds to the critical path — long enough that the paper-trade cron's
# health poll timed out before the server ever bound the port.
install_deps() {
  if [ "$NEEDS_INSTALL" != "1" ] && [ "${AGENTX_FORCE_INSTALL:-0}" != "1" ]; then
    return 0
  fi
  log "Installing dependencies..."
  pip install -r "$BACKEND_DIR/requirements.txt" -q --disable-pip-version-check
}

ensure_env() {
  if [ ! -f "$BACKEND_DIR/.env" ]; then
    cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
    warn "Created .env from .env.example"
    warn "Add your LLM API keys to backend/.env before using AI features"
    echo ""
  fi
}

# Find any process running on the backend port
find_pid() {
  # Check PID file first
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return
    fi
    rm -f "$PID_FILE"
  fi
  # Fallback: find by port
  lsof -ti :"$PORT" 2>/dev/null | head -1
}

# Every PID bound to the backend port (a wedged uvicorn can leave a parent +
# spawned child both LISTENing — stopping only one leaks the other).
all_port_pids() {
  lsof -ti :"$PORT" 2>/dev/null
}

# Is the backend actually answering HTTP, not just holding the socket? A hung
# process keeps the port bound but never replies — that is "down", not "up".
health_ok() {
  command -v curl &>/dev/null || return 1
  curl -fsS --max-time 4 "http://localhost:$PORT/api/health" >/dev/null 2>&1
}

# A single failed probe is not proof a backend is dead — it may just be busy
# (e.g. crunching a backtest). Retry a few times over ~15s before concluding it
# is genuinely wedged, so we never kill a healthy-but-busy server out from under
# the paper-trade cron.
health_ok_persistent() {
  local tries="${1:-5}" i
  for ((i = 1; i <= tries; i++)); do
    if health_ok; then return 0; fi
    sleep 3
  done
  return 1
}

# ── Commands ─────────────────────────────────────────────────

do_stop() {
  # Collect every PID on the port plus the PID-file PID, deduped — so a wedged
  # parent+child pair is fully torn down, not half-killed.
  local pids
  pids=$(
    {
      [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null
      all_port_pids
    } | grep -E '^[0-9]+$' | sort -u
  )

  if [ -z "$pids" ]; then
    log "No running backend found"
    rm -f "$PID_FILE"
    return
  fi

  log "Stopping backend (PIDs: $(echo "$pids" | tr '\n' ' '))..."
  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
  done

  # Wait up to 5 seconds for graceful shutdown of all of them.
  local count=0
  while [ -n "$(all_port_pids)" ] && [ $count -lt 10 ]; do
    sleep 0.5
    count=$((count + 1))
  done

  # Force kill anything still holding the port.
  local remaining
  remaining=$(all_port_pids)
  if [ -n "$remaining" ]; then
    warn "Graceful shutdown timed out, force killing..."
    for pid in $remaining; do
      kill -9 "$pid" 2>/dev/null || true
    done
  fi

  rm -f "$PID_FILE"
  log "Backend stopped"
}

do_start() {
  # Check if already running — but "the port is bound" is NOT the same as
  # "the backend is up". A hung process keeps the socket while answering
  # nothing, which previously deadlocked auto-start: do_start saw the port
  # taken and refused to launch a healthy replacement. So probe health.
  local existing_pid
  existing_pid=$(find_pid)
  if [ -n "$existing_pid" ]; then
    # Probe persistently: a busy backend (mid-backtest) can miss one health
    # check without being wedged. Only tear it down if it stays unresponsive.
    if health_ok_persistent 5; then
      warn "Backend already running and healthy on port $PORT (PID $existing_pid)"
      warn "Use './start.sh restart' to restart or './start.sh stop' to stop"
      return 1
    fi
    warn "Port $PORT held by PID $existing_pid but /api/health stayed unresponsive (~15s)"
    warn "Treating it as wedged — stopping it and starting a fresh backend"
    do_stop
    sleep 1
  fi

  cd "$BACKEND_DIR"
  ensure_venv
  install_deps
  ensure_env

  # Local-dev default: auto-generate a Fernet master key under ~/.agentx/secrets.key
  # if AGENTX_SECRETS_KEY isn't already set. Override by exporting either var.
  export AGENTX_DEV="${AGENTX_DEV:-1}"

  log "Starting agentX backend on http://localhost:$PORT ..."
  log "API docs: http://localhost:$PORT/docs"
  log "Health:   http://localhost:$PORT/api/health"
  echo ""

  # Start in background, save PID
  python run.py &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  # Wait a moment and verify it started
  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    err "Backend failed to start. Check logs above."
    rm -f "$PID_FILE"
    return 1
  fi

  log "Backend running (PID $pid)"
  log "Press Ctrl+C or run './start.sh stop' to stop"

  # Wait for the process (so Ctrl+C works)
  wait "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
}

do_clean() {
  log "Cleaning up..."

  do_stop

  # Remove database
  if [ -f "$DB_FILE" ]; then
    rm -f "$DB_FILE" "${DB_FILE}-wal" "${DB_FILE}-shm"
    log "Removed database: $DB_FILE"
  fi

  # Remove venv and reinstall
  if [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR"
    log "Removed virtual environment"
  fi

  # Remove __pycache__
  find "$BACKEND_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  log "Removed Python caches"

  echo ""
  log "Clean complete. Starting fresh..."
  echo ""
  do_start
}

do_status() {
  local pid
  pid=$(find_pid)
  if [ -n "$pid" ]; then
    if health_ok; then
      log "Backend is RUNNING and healthy on port $PORT (PID $pid)"
      if command -v curl &>/dev/null; then
        local health
        health=$(curl -s --max-time 3 "http://localhost:$PORT/api/health" 2>/dev/null)
        [ -n "$health" ] && log "Health: $health"
      fi
    else
      warn "Backend process is alive (PID $pid) but WEDGED — /api/health not responding"
      warn "Run './start.sh restart' to recover"
    fi
  else
    log "Backend is NOT running"
  fi
}

# ── Main ─────────────────────────────────────────────────────

CMD="${1:-start}"

case "$CMD" in
  start)   do_start   ;;
  stop)    do_stop    ;;
  restart)
    do_stop
    sleep 1
    do_start
    ;;
  clean)   do_clean   ;;
  status)  do_status  ;;
  *)
    err "Unknown command: $CMD"
    echo "Usage: $0 {start|stop|restart|clean|status}"
    exit 1
    ;;
esac
