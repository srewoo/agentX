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

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    log "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
  fi
  source "$VENV_DIR/bin/activate"
}

install_deps() {
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

# ── Commands ─────────────────────────────────────────────────

do_stop() {
  local pid
  pid=$(find_pid)
  if [ -z "$pid" ]; then
    log "No running backend found"
    return
  fi

  log "Stopping backend (PID $pid)..."
  kill "$pid" 2>/dev/null || true

  # Wait up to 5 seconds for graceful shutdown
  local count=0
  while kill -0 "$pid" 2>/dev/null && [ $count -lt 10 ]; do
    sleep 0.5
    count=$((count + 1))
  done

  # Force kill if still running
  if kill -0 "$pid" 2>/dev/null; then
    warn "Graceful shutdown timed out, force killing..."
    kill -9 "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE"
  log "Backend stopped"
}

do_start() {
  # Check if already running
  local existing_pid
  existing_pid=$(find_pid)
  if [ -n "$existing_pid" ]; then
    warn "Backend already running on port $PORT (PID $existing_pid)"
    warn "Use './start.sh restart' to restart or './start.sh stop' to stop"
    return 1
  fi

  cd "$BACKEND_DIR"
  ensure_venv
  install_deps
  ensure_env

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
    log "Backend is RUNNING on port $PORT (PID $pid)"
    # Quick health check
    if command -v curl &>/dev/null; then
      local health
      health=$(curl -s --max-time 3 "http://localhost:$PORT/api/health" 2>/dev/null)
      if [ -n "$health" ]; then
        log "Health: $health"
      fi
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
