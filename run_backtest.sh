#!/bin/bash
# ─────────────────────────────────────────────────────────────
# agentX Backtester — test signal accuracy against historical data
#
# Usage:
#   ./run_backtest.sh                  Run all NIFTY 50 stocks (1 year)
#   ./run_backtest.sh RELIANCE TCS     Run specific symbols
#   ./run_backtest.sh --period 2y      Change lookback period (6mo/1y/2y/5y)
#   ./run_backtest.sh --quick          Top 10 stocks only (fast test)
# ─────────────────────────────────────────────────────────────

set -e

BACKEND_URL="${BACKEND_URL:-http://localhost:8020}"
PERIOD="1y"
EVAL_DAYS=5
OUTPUT_DIR="backend/backtest_results"
SYMBOLS=()
QUICK=false

# ── Parse args ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --period)  PERIOD="$2"; shift 2 ;;
    --eval)    EVAL_DAYS="$2"; shift 2 ;;
    --quick)   QUICK=true; shift ;;
    --help|-h)
      echo "Usage: $0 [OPTIONS] [SYMBOL ...]"
      echo ""
      echo "Options:"
      echo "  --period PERIOD   Lookback period: 6mo, 1y, 2y, 5y (default: 1y)"
      echo "  --eval DAYS       Primary eval window in days (default: 5)"
      echo "  --quick           Top 10 stocks only"
      echo ""
      echo "Examples:"
      echo "  $0                        # All NIFTY 50, 1 year"
      echo "  $0 RELIANCE TCS INFY      # Specific stocks"
      echo "  $0 --period 2y --quick    # Quick test, 2 years"
      exit 0
      ;;
    *)  SYMBOLS+=("$1"); shift ;;
  esac
done

# ── Default symbol lists ─────────────────────────────────────
NIFTY_50=(
  RELIANCE TCS HDFCBANK INFY ICICIBANK HINDUNILVR SBIN BHARTIARTL
  ITC KOTAKBANK LT AXISBANK WIPRO ASIANPAINT MARUTI TATAMOTORS
  SUNPHARMA BAJFINANCE TITAN NESTLEIND TECHM HCLTECH ULTRACEMCO
  POWERGRID NTPC ONGC TATASTEEL JSWSTEEL ADANIENT ADANIPORTS
  COALINDIA DRREDDY CIPLA EICHERMOT HEROMOTOCO BAJAJFINSV BRITANNIA
  DIVISLAB GRASIM APOLLOHOSP HDFCLIFE SBILIFE TATACONSUM INDUSINDBK
  HINDALCO BPCL ZOMATO TRENT BEL HAL
)

TOP_10=(RELIANCE TCS HDFCBANK INFY ICICIBANK SBIN ITC BHARTIARTL LT BAJFINANCE)

if [ ${#SYMBOLS[@]} -eq 0 ]; then
  if [ "$QUICK" = true ]; then
    SYMBOLS=("${TOP_10[@]}")
  else
    SYMBOLS=("${NIFTY_50[@]}")
  fi
fi

# ── Helpers ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[backtest]${NC} $*"; }
ok()   { echo -e "${GREEN}[backtest]${NC} $*"; }
warn() { echo -e "${YELLOW}[backtest]${NC} $*"; }
err()  { echo -e "${RED}[backtest]${NC} $*" >&2; }

# ── Check backend is running ─────────────────────────────────
log "Checking backend at ${BACKEND_URL}..."
if ! curl -s --max-time 5 "${BACKEND_URL}/api/health" > /dev/null 2>&1; then
  err "Backend not reachable at ${BACKEND_URL}"
  err "Start it with: ./start.sh"
  exit 1
fi
ok "Backend is running"

# ── Create output directory ──────────────────────────────────
mkdir -p "$OUTPUT_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SUMMARY_FILE="${OUTPUT_DIR}/summary_${TIMESTAMP}.txt"
JSON_DIR="${OUTPUT_DIR}/json_${TIMESTAMP}"
mkdir -p "$JSON_DIR"

# ── Run backtests ────────────────────────────────────────────
TOTAL=${#SYMBOLS[@]}
PASSED=0
FAILED=0
SKIPPED=0

echo ""
log "${BOLD}Running backtest: ${TOTAL} symbols, period=${PERIOD}, eval=${EVAL_DAYS}d${NC}"
echo "─────────────────────────────────────────────────────────────"
printf "%-14s %8s %8s %8s %8s %8s %10s\n" \
  "SYMBOL" "SIGNALS" "WIN_1D" "WIN_5D" "WIN_10D" "AVG_PNL" "BEST_TYPE"
echo "─────────────────────────────────────────────────────────────"

# Accumulate for final summary
declare -a ALL_WIN_RATES_5D
declare -a ALL_AVG_PNL_5D
declare -a ALL_SIGNALS

for i in "${!SYMBOLS[@]}"; do
  SYM="${SYMBOLS[$i]}"
  NUM=$((i + 1))

  # Call the backtest API (can take 10-60s per symbol)
  RESPONSE=$(curl -s --max-time 120 -X POST \
    "${BACKEND_URL}/api/backtest/${SYM}?period=${PERIOD}&eval_days=${EVAL_DAYS}" \
    2>/dev/null)

  if [ -z "$RESPONSE" ]; then
    printf "%-14s ${RED}%8s${NC}\n" "$SYM" "TIMEOUT"
    FAILED=$((FAILED + 1))
    continue
  fi

  # Check for error
  ERROR=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('detail',''))" 2>/dev/null)
  if [ -n "$ERROR" ]; then
    printf "%-14s ${YELLOW}%8s${NC} %s\n" "$SYM" "SKIP" "$ERROR"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Save raw JSON
  echo "$RESPONSE" | python3 -m json.tool > "${JSON_DIR}/${SYM}.json" 2>/dev/null

  # Extract metrics
  METRICS=$(echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
o = d.get('overall', {})
total = o.get('total_signals', 0)
wr1 = o.get('win_rate_1d', 0)
wr5 = o.get('win_rate_5d', 0)
wr10 = o.get('win_rate_10d', 0)
pnl5 = o.get('avg_pnl_5d', 0)
best = o.get('best_signal_type', '-')
print(f'{total}|{wr1}|{wr5}|{wr10}|{pnl5}|{best}')
" 2>/dev/null)

  if [ -z "$METRICS" ]; then
    printf "%-14s ${RED}%8s${NC}\n" "$SYM" "ERROR"
    FAILED=$((FAILED + 1))
    continue
  fi

  IFS='|' read -r SIG_COUNT WR1 WR5 WR10 PNL5 BEST_TYPE <<< "$METRICS"

  # Color code win rate
  if (( $(echo "$WR5 >= 55" | bc -l 2>/dev/null || echo 0) )); then
    WR_COLOR="${GREEN}"
  elif (( $(echo "$WR5 >= 45" | bc -l 2>/dev/null || echo 0) )); then
    WR_COLOR="${YELLOW}"
  else
    WR_COLOR="${RED}"
  fi

  # Color code PnL
  if (( $(echo "$PNL5 > 0" | bc -l 2>/dev/null || echo 0) )); then
    PNL_COLOR="${GREEN}"
  else
    PNL_COLOR="${RED}"
  fi

  printf "%-14s %8s ${WR_COLOR}%7s%%${NC} ${WR_COLOR}%7s%%${NC} ${WR_COLOR}%7s%%${NC} ${PNL_COLOR}%9s%%${NC} %s\n" \
    "$SYM" "$SIG_COUNT" "$WR1" "$WR5" "$WR10" "$PNL5" "$BEST_TYPE"

  ALL_WIN_RATES_5D+=("$WR5")
  ALL_AVG_PNL_5D+=("$PNL5")
  ALL_SIGNALS+=("$SIG_COUNT")
  PASSED=$((PASSED + 1))
done

echo "─────────────────────────────────────────────────────────────"

# ── Compute summary ──────────────────────────────────────────
echo ""
if [ ${#ALL_WIN_RATES_5D[@]} -gt 0 ]; then
  SUMMARY=$(python3 -c "
import sys
win_rates = [${ALL_WIN_RATES_5D[*]/%/,}]
avg_pnls = [${ALL_AVG_PNL_5D[*]/%/,}]
signals = [${ALL_SIGNALS[*]/%/,}]

avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0
avg_pnl = sum(avg_pnls) / len(avg_pnls) if avg_pnls else 0
total_signals = sum(int(s) for s in signals)
best_wr = max(win_rates) if win_rates else 0
worst_wr = min(win_rates) if win_rates else 0
profitable = sum(1 for p in avg_pnls if p > 0)

print(f'avg_wr={avg_wr:.1f}')
print(f'avg_pnl={avg_pnl:.4f}')
print(f'total_signals={total_signals}')
print(f'best_wr={best_wr:.1f}')
print(f'worst_wr={worst_wr:.1f}')
print(f'profitable={profitable}')
print(f'total_stocks={len(win_rates)}')
" 2>/dev/null)

  eval "$SUMMARY"

  echo -e "${BOLD}BACKTEST SUMMARY${NC}"
  echo "═══════════════════════════════════════════"
  echo -e "Period:              ${BOLD}${PERIOD}${NC}"
  echo -e "Stocks tested:       ${BOLD}${PASSED}${NC} passed, ${SKIPPED} skipped, ${FAILED} failed"
  echo -e "Total signals:       ${BOLD}${total_signals}${NC}"
  echo ""
  echo -e "Avg 5-day win rate:  ${BOLD}${avg_wr}%${NC}"
  echo -e "Avg 5-day PnL:       ${BOLD}${avg_pnl}%${NC}"
  echo -e "Best win rate:       ${best_wr}%"
  echo -e "Worst win rate:      ${worst_wr}%"
  echo -e "Stocks profitable:   ${profitable}/${total_stocks}"
  echo "═══════════════════════════════════════════"

  # Verdict
  echo ""
  if (( $(echo "$avg_wr > 55" | bc -l 2>/dev/null || echo 0) )); then
    ok "${BOLD}VERDICT: Signal engine has edge (>55% win rate)${NC}"
  elif (( $(echo "$avg_wr > 50" | bc -l 2>/dev/null || echo 0) )); then
    warn "${BOLD}VERDICT: Marginal edge (50-55% win rate) — needs tuning${NC}"
  else
    err "${BOLD}VERDICT: Below coin-flip (<50% win rate) — signals need improvement${NC}"
  fi

  # Save summary to file
  {
    echo "agentX Backtest Summary — $(date)"
    echo "Period: ${PERIOD}, Eval: ${EVAL_DAYS}d"
    echo "Stocks: ${PASSED} passed, ${SKIPPED} skipped, ${FAILED} failed"
    echo "Total signals: ${total_signals}"
    echo "Avg 5d win rate: ${avg_wr}%"
    echo "Avg 5d PnL: ${avg_pnl}%"
    echo "Best: ${best_wr}%, Worst: ${worst_wr}%"
    echo "Profitable stocks: ${profitable}/${total_stocks}"
  } > "$SUMMARY_FILE"

  echo ""
  log "Results saved to:"
  log "  Summary: ${SUMMARY_FILE}"
  log "  Per-stock JSON: ${JSON_DIR}/"
else
  err "No successful backtests. Check backend logs."
fi

echo ""
log "Done. Took ${SECONDS}s total."
