#!/bin/bash
# ─────────────────────────────────────────────────────────────
# agentX Paper Trader
#
# Scans for signals, simulates trades with position sizing + stop losses,
# tracks P&L in a CSV file. Runs on demand or scheduled at 11 AM daily.
#
# Usage:
#   ./paper_trade.sh                Run now (scan + open paper trades)
#   ./paper_trade.sh evaluate       Evaluate open trades against current prices
#   ./paper_trade.sh report         Show P&L summary
#   ./paper_trade.sh schedule       Install daily 11 AM cron job
#   ./paper_trade.sh unschedule     Remove the cron job
#   ./paper_trade.sh full           Scan + evaluate + report (daily routine)
# ─────────────────────────────────────────────────────────────

set -e

BACKEND_URL="${BACKEND_URL:-http://localhost:8020}"
DATA_DIR="backend/paper_trades"
TRADES_FILE="${DATA_DIR}/trades.csv"
DAILY_LOG="${DATA_DIR}/daily_log.csv"
SUMMARY_FILE="${DATA_DIR}/summary.txt"

# ── Risk parameters ──────────────────────────────────────────
CAPITAL=1000000            # 10 lakh paper capital
MAX_POSITION_PCT=3         # Max 3% of capital per trade
STOP_LOSS_PCT=3            # 3% stop loss
TARGET_PCT=5               # 5% target (risk:reward ~1:1.7)
MIN_SIGNAL_STRENGTH=6      # Only trade strength >= 6
MAX_OPEN_TRADES=15         # Max concurrent positions
HOLD_LIMIT_DAYS=10         # Auto-exit after 10 days
MIN_PRICE=10               # Minimum stock price in Rs. — avoid penny stocks
MAX_SECTOR_POSITIONS=2     # Max open positions per sector (prevent sector concentration risk)
MAX_PORTFOLIO_HEAT_PCT=6   # Max total open risk as % of capital (portfolio heat limit)
MAX_DRAWDOWN_PCT=10        # Circuit breaker: pause if equity drops 10% from peak
COOLDOWN_DAYS=2            # Resume after N trading days post circuit breaker

# ── Colors ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${CYAN}[paper]${NC} $*"; }
ok()   { echo -e "${GREEN}[paper]${NC} $*"; }
warn() { echo -e "${YELLOW}[paper]${NC} $*"; }
err()  { echo -e "${RED}[paper]${NC} $*" >&2; }

# ── Setup ────────────────────────────────────────────────────
mkdir -p "$DATA_DIR"

# Create CSV headers if files don't exist
if [ ! -f "$TRADES_FILE" ]; then
  echo "trade_id,symbol,direction,signal_type,strength,entry_price,entry_date,stop_loss,target,position_size,shares,status,exit_price,exit_date,pnl_pct,pnl_amount,exit_reason,trailing_stop" > "$TRADES_FILE"
  log "Created trades file: $TRADES_FILE"
fi
if [ ! -f "$DAILY_LOG" ]; then
  echo "date,open_trades,closed_today,total_closed,win_rate,total_pnl,capital" > "$DAILY_LOG"
fi

# ── Check backend ────────────────────────────────────────────
check_backend() {
  if ! curl -s --max-time 5 "${BACKEND_URL}/api/health" > /dev/null 2>&1; then
    err "Backend not reachable at ${BACKEND_URL}. Start it with: ./start.sh"
    exit 1
  fi
}

# ── Drawdown Circuit Breaker Check ──────────────────────────
check_circuit_breaker() {
  python3 -c "
import csv, sys, os
from datetime import datetime, date, timedelta

trades_file = '${TRADES_FILE}'
daily_log = '${DAILY_LOG}'
capital = ${CAPITAL}
max_dd_pct = ${MAX_DRAWDOWN_PCT}
cooldown_days = ${COOLDOWN_DAYS}

if not os.path.exists(trades_file):
    sys.exit(0)

# Compute current equity from closed trades
total_pnl = 0.0
last_close_date = None
with open(trades_file, 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row.get('status') == 'closed':
            try:
                total_pnl += float(row.get('pnl_amount', 0))
                ed = row.get('exit_date', '')
                if ed and (last_close_date is None or ed > last_close_date):
                    last_close_date = ed
            except ValueError:
                pass

current_equity = capital + total_pnl

# Peak equity from daily log (or just capital if no history)
peak_equity = capital
if os.path.exists(daily_log):
    with open(daily_log, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                cap = float(row.get('capital', capital))
                pnl = float(row.get('total_pnl', 0))
                eq = cap + pnl
                if eq > peak_equity:
                    peak_equity = eq
            except (ValueError, KeyError):
                pass

drawdown_pct = (peak_equity - current_equity) / peak_equity * 100 if peak_equity > 0 else 0

if drawdown_pct >= max_dd_pct:
    # Check if cooldown has elapsed
    if last_close_date:
        try:
            last_close = datetime.strptime(last_close_date, '%Y-%m-%d').date()
            days_since = (date.today() - last_close).days
            if days_since < cooldown_days:
                print(f'CIRCUIT_BREAKER:Drawdown={drawdown_pct:.1f}%,Cooldown={cooldown_days - days_since}d remaining')
                sys.exit(1)
        except Exception:
            pass
    print(f'CIRCUIT_BREAKER:Drawdown={drawdown_pct:.1f}%')
    sys.exit(1)

sys.exit(0)
" 2>/dev/null
  return $?
}

# ── Scan & Open Trades ───────────────────────────────────────
do_scan() {
  check_backend

  # ── Drawdown circuit breaker ──────────────────────────────
  CB_RESULT=$(check_circuit_breaker 2>/dev/null; echo $?)
  if [ "${CB_RESULT}" = "1" ]; then
    CB_INFO=$(python3 -c "
import csv, os
from datetime import datetime, date
trades_file = '${TRADES_FILE}'
capital = ${CAPITAL}
total_pnl = 0.0
if os.path.exists(trades_file):
    with open(trades_file) as f:
        for row in csv.DictReader(f):
            if row.get('status') == 'closed':
                try: total_pnl += float(row.get('pnl_amount', 0))
                except: pass
print(f'Equity: Rs.{capital + total_pnl:,.0f}  Loss: Rs.{total_pnl:+,.0f}')
" 2>/dev/null)
    warn "CIRCUIT BREAKER ACTIVE — drawdown exceeds ${MAX_DRAWDOWN_PCT}%. ${CB_INFO}"
    warn "Pausing new trades for ${COOLDOWN_DAYS} trading days. Use 'evaluate' and 'report' to monitor."
    return
  fi

  log "Triggering market scan..."

  SCAN_RESULT=$(curl -s --max-time 120 -X POST "${BACKEND_URL}/api/scan/trigger" 2>/dev/null)
  SIGNALS_FOUND=$(echo "$SCAN_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('signals_found',0))" 2>/dev/null || echo 0)
  log "Scan complete: ${SIGNALS_FOUND} signals detected"

  # Fetch top signals (unread, undismissed)
  SIGNALS_JSON=$(curl -s --max-time 30 "${BACKEND_URL}/api/signals/latest?limit=30" 2>/dev/null)

  # Count current open trades
  OPEN_COUNT=$(awk -F',' '$12=="open" {count++} END {print count+0}' "$TRADES_FILE")

  if [ "$OPEN_COUNT" -ge "$MAX_OPEN_TRADES" ]; then
    warn "Already at max open trades ($MAX_OPEN_TRADES). Skipping new entries."
    return
  fi

  SLOTS=$((MAX_OPEN_TRADES - OPEN_COUNT))
  POSITION_SIZE=$(( CAPITAL * MAX_POSITION_PCT / 100 ))
  TODAY=$(date +%Y-%m-%d)
  NEW_TRADES=0

  # Process signals and open paper trades
  echo "$SIGNALS_JSON" | python3 -c "
import sys, json, csv, uuid, os

# Sector lookup for major Indian stocks (NSE)
SECTOR_MAP = {
    'RELIANCE': 'Energy', 'TCS': 'IT', 'HDFCBANK': 'Banking', 'INFY': 'IT',
    'ICICIBANK': 'Banking', 'HINDUNILVR': 'FMCG', 'SBIN': 'Banking',
    'BHARTIARTL': 'Telecom', 'ITC': 'FMCG', 'KOTAKBANK': 'Banking',
    'LT': 'Infrastructure', 'AXISBANK': 'Banking', 'WIPRO': 'IT',
    'ASIANPAINT': 'Consumer', 'MARUTI': 'Auto', 'TATAMOTORS': 'Auto',
    'SUNPHARMA': 'Pharma', 'BAJFINANCE': 'Finance', 'TITAN': 'Consumer',
    'NESTLEIND': 'FMCG', 'TECHM': 'IT', 'HCLTECH': 'IT',
    'ULTRACEMCO': 'Cement', 'POWERGRID': 'Power', 'NTPC': 'Power',
    'ONGC': 'Energy', 'TATASTEEL': 'Metals', 'JSWSTEEL': 'Metals',
    'ADANIENT': 'Conglomerate', 'ADANIPORTS': 'Infrastructure',
    'COALINDIA': 'Mining', 'DRREDDY': 'Pharma', 'CIPLA': 'Pharma',
    'EICHERMOT': 'Auto', 'HEROMOTOCO': 'Auto', 'BAJAJFINSV': 'Finance',
    'BRITANNIA': 'FMCG', 'DIVISLAB': 'Pharma', 'GRASIM': 'Cement',
    'APOLLOHOSP': 'Healthcare', 'HDFCLIFE': 'Insurance', 'SBILIFE': 'Insurance',
    'TATACONSUM': 'FMCG', 'INDUSINDBK': 'Banking', 'HINDALCO': 'Metals',
    'BPCL': 'Energy', 'ZOMATO': 'Consumer', 'TRENT': 'Consumer',
    'BEL': 'Defense', 'HAL': 'Defense', 'PNB': 'Banking', 'BANKBARODA': 'Banking',
    'CANBK': 'Banking', 'UNIONBANK': 'Banking', 'YESBANK': 'Banking',
    'FEDERALBNK': 'Banking', 'BANDHANBNK': 'Banking', 'IDFCFIRSTB': 'Banking',
}

data = json.load(sys.stdin)
signals = data.get('signals', [])
trades_file = '${TRADES_FILE}'
min_strength = ${MIN_SIGNAL_STRENGTH}
stop_pct = ${STOP_LOSS_PCT}
target_pct = ${TARGET_PCT}
position_size = ${POSITION_SIZE}
slots = ${SLOTS}
today = '${TODAY}'
min_price = ${MIN_PRICE}
max_sector_positions = ${MAX_SECTOR_POSITIONS}
capital = ${CAPITAL}
max_heat_pct = ${MAX_PORTFOLIO_HEAT_PCT}

# Load existing open symbols and sector counts to avoid duplicates + sector concentration
open_symbols = set()
sector_counts = {}  # sector -> count of open positions
current_heat = 0.0  # Rs. total open risk (portfolio heat)
if os.path.exists(trades_file):
    with open(trades_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('status') == 'open':
                sym = row['symbol']
                open_symbols.add(sym)
                sector = SECTOR_MAP.get(sym, 'Unknown')
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                # Portfolio heat = shares × |entry - stop|
                try:
                    heat = int(row.get('shares', 0)) * abs(float(row.get('entry_price', 0)) - float(row.get('stop_loss', 0)))
                    current_heat += heat
                except (ValueError, TypeError):
                    pass

new_count = 0
with open(trades_file, 'a', newline='') as f:
    writer = csv.writer(f)
    for sig in signals:
        if new_count >= slots:
            break

        strength = sig.get('strength', 0)
        if strength < min_strength:
            continue

        symbol = sig['symbol']
        if symbol in open_symbols:
            continue

        direction = sig.get('direction', 'neutral')
        if direction == 'neutral':
            continue  # Only trade directional signals

        price = sig.get('current_price')
        if not price or price <= 0:
            continue

        # ── Minimum price filter (avoid penny stocks) ──────────────────
        if price < min_price:
            print(f'  SKIP  {symbol:14s} — price Rs.{price:.2f} below minimum Rs.{min_price}')
            continue

        # ── Sector concentration limit ─────────────────────────────────
        sector = SECTOR_MAP.get(symbol, 'Unknown')
        if sector_counts.get(sector, 0) >= max_sector_positions:
            print(f'  SKIP  {symbol:14s} — sector {sector} at max {max_sector_positions} positions')
            continue

        signal_type = sig.get('signal_type', 'unknown')
        trade_id = str(uuid.uuid4())[:8]

        # Calculate stop loss and target
        if direction == 'bullish':
            stop = round(price * (1 - stop_pct/100), 2)
            target = round(price * (1 + target_pct/100), 2)
        else:
            stop = round(price * (1 + stop_pct/100), 2)
            target = round(price * (1 - target_pct/100), 2)

        shares = int(position_size / price)
        if shares <= 0:
            continue

        # ── Portfolio heat limit ───────────────────────────────────────
        trade_heat = shares * abs(price - stop)
        if (current_heat + trade_heat) / capital * 100 > max_heat_pct:
            print(f'  SKIP  {symbol:14s} — portfolio heat limit {max_heat_pct}% would be exceeded')
            continue

        actual_position = round(shares * price, 2)

        writer.writerow([
            trade_id, symbol, direction, signal_type, strength,
            price, today, stop, target, actual_position, shares,
            'open', '', '', '', '', '', stop  # trailing_stop = initial stop
        ])

        open_symbols.add(symbol)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        current_heat += trade_heat
        new_count += 1
        print(f'  OPEN {direction.upper():7s} {symbol:14s} @ {price:>10.2f}  SL={stop:>10.2f}  TGT={target:>10.2f}  Qty={shares}  [{signal_type}] ({sector})')

print(f'TOTAL:{new_count}')
" 2>/dev/null | while IFS= read -r line; do
    if [[ "$line" == TOTAL:* ]]; then
      NEW_TRADES="${line#TOTAL:}"
    else
      echo -e "  ${GREEN}$line${NC}"
    fi
  done

  ok "Opened paper trades for today"
}

# ── Evaluate Open Trades ─────────────────────────────────────
do_evaluate() {
  check_backend
  log "Evaluating open trades..."

  OPEN_COUNT=$(awk -F',' '$12=="open" {count++} END {print count+0}' "$TRADES_FILE")
  if [ "$OPEN_COUNT" -eq 0 ]; then
    log "No open trades to evaluate"
    return
  fi

  log "Checking ${OPEN_COUNT} open positions..."

  python3 -c "
import csv, json, sys, os
import urllib.request

backend = '${BACKEND_URL}'
trades_file = '${TRADES_FILE}'
stop_pct = ${STOP_LOSS_PCT}
target_pct = ${TARGET_PCT}
hold_limit = ${HOLD_LIMIT_DAYS}
today = '$(date +%Y-%m-%d)'

# Read all trades
rows = []
with open(trades_file, 'r') as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        rows.append(row)

closed_today = 0
total_pnl = 0.0

for row in rows:
    if row['status'] != 'open':
        continue

    symbol = row['symbol']
    entry_price = float(row['entry_price'])
    direction = row['direction']
    stop_loss = float(row['stop_loss'])
    target = float(row['target'])
    shares = int(row['shares'])
    entry_date = row['entry_date']

    # Calculate hold days
    from datetime import datetime
    try:
        entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        hold_days = (today_dt - entry_dt).days
    except:
        hold_days = 0

    # Fetch current price + intraday high/low for accurate stop-loss detection
    try:
        req = urllib.request.Request(f'{backend}/api/stocks/{symbol}/quote')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        current_price = data.get('price')
        if not current_price:
            continue
        # day_low and day_high for intraday stop-loss check
        # Falls back to current_price if not provided by the API
        day_low = data.get('day_low') or current_price
        day_high = data.get('day_high') or current_price
    except:
        print(f'  SKIP {symbol:14s} — could not fetch price')
        continue

    # Update trailing stop before checking exit conditions
    try:
        trailing_stop = float(row.get('trailing_stop') or stop_loss)
    except (ValueError, TypeError):
        trailing_stop = stop_loss

    # Trailing stop update rules (bullish: lock in profits as price rises)
    if direction == 'bullish':
        move_pct = (current_price - entry_price) / entry_price * 100
        if move_pct >= 3.0:
            new_trail = round(current_price * 0.97, 2)
            trailing_stop = max(trailing_stop, new_trail)
        elif move_pct >= 1.5:
            trailing_stop = max(trailing_stop, entry_price)  # breakeven
    else:
        move_pct = (entry_price - current_price) / entry_price * 100
        if move_pct >= 3.0:
            new_trail = round(current_price * 1.03, 2)
            trailing_stop = min(trailing_stop, new_trail)
        elif move_pct >= 1.5:
            trailing_stop = min(trailing_stop, entry_price)

    row['trailing_stop'] = str(trailing_stop)

    # Check exit conditions — use intraday high/low to detect stop triggers
    # that occurred during the day even if current price has moved away.
    exit_reason = None
    exit_price = current_price

    if direction == 'bullish':
        pnl_pct = (current_price - entry_price) / entry_price * 100
        # Check trailing stop first (tighter), then original stop
        if day_low <= trailing_stop:
            exit_reason = 'trailing_stop' if trailing_stop > stop_loss else 'stop_loss'
            exit_price = trailing_stop
            pnl_pct = (trailing_stop - entry_price) / entry_price * 100
        elif day_low <= stop_loss:
            exit_reason = 'stop_loss'
            exit_price = stop_loss
            pnl_pct = (stop_loss - entry_price) / entry_price * 100
        elif current_price >= target:
            exit_reason = 'target_hit'
    else:
        pnl_pct = (entry_price - current_price) / entry_price * 100
        if day_high >= trailing_stop:
            exit_reason = 'trailing_stop' if trailing_stop < stop_loss else 'stop_loss'
            exit_price = trailing_stop
            pnl_pct = (entry_price - trailing_stop) / entry_price * 100
        elif day_high >= stop_loss:
            exit_reason = 'stop_loss'
            exit_price = stop_loss
            pnl_pct = (entry_price - stop_loss) / entry_price * 100
        elif current_price <= target:
            exit_reason = 'target_hit'

    if hold_days >= hold_limit:
        exit_reason = 'time_exit'

    pnl_pct = round(pnl_pct, 2)
    pnl_amount = round(pnl_pct / 100 * float(row['position_size']), 2)

    if exit_reason:
        row['status'] = 'closed'
        row['exit_price'] = str(exit_price)
        row['exit_date'] = today
        row['pnl_pct'] = str(pnl_pct)
        row['pnl_amount'] = str(pnl_amount)
        row['exit_reason'] = exit_reason
        closed_today += 1
        total_pnl += pnl_amount

        color = '\033[0;32m' if pnl_pct > 0 else '\033[0;31m'
        reset = '\033[0m'
        print(f'  CLOSE {symbol:14s} {direction:7s}  Entry={entry_price:>8.2f}  Exit={exit_price:>8.2f}  {color}PnL={pnl_pct:+.2f}% (Rs.{pnl_amount:+.0f}){reset}  [{exit_reason}] ({hold_days}d)')
    else:
        color = '\033[0;32m' if pnl_pct > 0 else '\033[0;31m'
        reset = '\033[0m'
        print(f'  HOLD  {symbol:14s} {direction:7s}  Entry={entry_price:>8.2f}  Now={current_price:>8.2f}  {color}PnL={pnl_pct:+.2f}%{reset}  (day {hold_days}/{hold_limit})')

# Write back
with open(trades_file, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f'---')
print(f'Closed today: {closed_today}, PnL today: Rs.{total_pnl:+.0f}')
" 2>&1 | while IFS= read -r line; do
    echo -e "  $line"
  done

  ok "Evaluation complete"
}

# ── Report ───────────────────────────────────────────────────
do_report() {
  if [ ! -f "$TRADES_FILE" ]; then
    warn "No trades file found. Run './paper_trade.sh' first."
    return
  fi

  python3 -c "
import csv, sys

trades_file = '${TRADES_FILE}'
capital = ${CAPITAL}

open_trades = []
closed_trades = []

with open(trades_file, 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row['status'] == 'open':
            open_trades.append(row)
        elif row['status'] == 'closed':
            closed_trades.append(row)

total_closed = len(closed_trades)
wins = sum(1 for t in closed_trades if float(t.get('pnl_pct',0)) > 0)
losses = total_closed - wins
win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

total_pnl = sum(float(t.get('pnl_amount',0)) for t in closed_trades)
total_pnl_pct = total_pnl / capital * 100

avg_win = 0
avg_loss = 0
if wins > 0:
    avg_win = sum(float(t['pnl_pct']) for t in closed_trades if float(t.get('pnl_pct',0)) > 0) / wins
if losses > 0:
    avg_loss = sum(float(t['pnl_pct']) for t in closed_trades if float(t.get('pnl_pct',0)) <= 0) / losses

# By exit reason
by_reason = {}
for t in closed_trades:
    reason = t.get('exit_reason', 'unknown')
    by_reason.setdefault(reason, {'count': 0, 'pnl': 0})
    by_reason[reason]['count'] += 1
    by_reason[reason]['pnl'] += float(t.get('pnl_amount', 0))

# By signal type
by_type = {}
for t in closed_trades:
    stype = t.get('signal_type', 'unknown')
    by_type.setdefault(stype, {'count': 0, 'wins': 0, 'pnl': 0})
    by_type[stype]['count'] += 1
    if float(t.get('pnl_pct',0)) > 0:
        by_type[stype]['wins'] += 1
    by_type[stype]['pnl'] += float(t.get('pnl_amount', 0))

# Max drawdown (sequential losses)
running_pnl = 0
peak = 0
max_dd = 0
for t in closed_trades:
    running_pnl += float(t.get('pnl_amount', 0))
    if running_pnl > peak:
        peak = running_pnl
    dd = peak - running_pnl
    if dd > max_dd:
        max_dd = dd

print()
print('\033[1m' + '=' * 55)
print('  PAPER TRADING REPORT')
print('=' * 55 + '\033[0m')
print()
print(f'  Starting Capital:     Rs.{capital:>12,}')
print(f'  Current P&L:          Rs.{total_pnl:>+12,.0f}  ({total_pnl_pct:+.2f}%)')
print(f'  Max Drawdown:         Rs.{max_dd:>12,.0f}')
print()
print(f'  Open Positions:       {len(open_trades):>5}')
print(f'  Closed Trades:        {total_closed:>5}')
print(f'  Wins / Losses:        {wins} / {losses}')

wr_color = '\033[0;32m' if win_rate >= 50 else '\033[0;31m'
print(f'  Win Rate:             {wr_color}{win_rate:.1f}%\033[0m')
print(f'  Avg Win:              {avg_win:+.2f}%')
print(f'  Avg Loss:             {avg_loss:+.2f}%')

if by_reason:
    print()
    print('  \033[1mBy Exit Reason:\033[0m')
    for reason, data in sorted(by_reason.items(), key=lambda x: -x[1]['count']):
        print(f'    {reason:15s}  {data[\"count\"]:>3} trades  Rs.{data[\"pnl\"]:>+10,.0f}')

if by_type:
    print()
    print('  \033[1mBy Signal Type (top 10):\033[0m')
    sorted_types = sorted(by_type.items(), key=lambda x: -x[1]['count'])[:10]
    for stype, data in sorted_types:
        wr = data['wins'] / data['count'] * 100 if data['count'] > 0 else 0
        print(f'    {stype:22s}  {data[\"count\"]:>3} trades  WR={wr:>5.1f}%  Rs.{data[\"pnl\"]:>+10,.0f}')

if open_trades:
    print()
    print('  \033[1mOpen Positions:\033[0m')
    total_heat_pct = 0.0
    for t in open_trades:
        try:
            entry = float(t['entry_price'])
            sl = float(t['stop_loss'])
            shares = int(t['shares'])
            heat = shares * abs(entry - sl)
            heat_pct = heat / capital * 100
            total_heat_pct += heat_pct
            tsl = t.get('trailing_stop', '')
            tsl_str = f'  TSL={float(tsl):>8.2f}' if tsl else ''
            print(f'    {t[\"symbol\"]:14s}  {t[\"direction\"]:7s}  Entry={entry:>8.2f}  SL={sl:>8.2f}  TGT={float(t[\"target\"]):>8.2f}  Qty={shares}{tsl_str}  Heat={heat_pct:.1f}%')
        except (ValueError, TypeError):
            print(f'    {t[\"symbol\"]:14s}  {t[\"direction\"]:7s}  Entry={t[\"entry_price\"]:>8s}  SL={t[\"stop_loss\"]:>8s}  TGT={t[\"target\"]:>8s}  Qty={t[\"shares\"]}')
    heat_color = '\033[0;31m' if total_heat_pct > ${MAX_PORTFOLIO_HEAT_PCT} else '\033[0;32m'
    print(f'  Portfolio Heat:       {heat_color}{total_heat_pct:.1f}%\033[0m  (max: ${MAX_PORTFOLIO_HEAT_PCT}%)')

print()

# ── Sharpe & Sortino ratios ──────────────────────────────────────────────
if len(closed_trades) >= 3:
    import math
    risk_free_daily = 0.07 / 252  # India 10Y bond yield ~7%

    # Daily returns: pnl_pct / hold_days for each trade
    daily_returns = []
    for t in closed_trades:
        try:
            pnl = float(t.get('pnl_pct', 0))
            hold = max(1, int(t.get('hold_days', 1) or 1))
            daily_returns.append(pnl / hold / 100)
        except (ValueError, TypeError):
            pass

    if len(daily_returns) >= 3:
        mean_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0001

        # Sharpe ratio
        sharpe = (mean_ret - risk_free_daily) / std_dev * math.sqrt(252) if std_dev > 0 else 0.0

        # Sortino ratio (downside deviation only)
        neg_returns = [r for r in daily_returns if r < risk_free_daily]
        if neg_returns:
            downside_var = sum((r - risk_free_daily) ** 2 for r in neg_returns) / len(neg_returns)
            downside_std = math.sqrt(downside_var)
            sortino = (mean_ret - risk_free_daily) / downside_std * math.sqrt(252) if downside_std > 0 else 0.0
        else:
            sortino = 999.0  # No losing days

        print(f'  Sharpe Ratio:         {sharpe:>+.2f}  (>1.0 = viable, >1.5 = good, >2.0 = excellent)')
        print(f'  Sortino Ratio:        {sortino:>+.2f}  (penalizes only downside volatility)')

print()
print('=' * 55)
" 2>&1
}

# ── Full daily routine ───────────────────────────────────────
do_full() {
  log "${BOLD}Running daily paper trading routine${NC}"
  echo ""
  do_evaluate
  echo ""
  do_scan
  echo ""
  do_report

  # Append to daily log
  python3 -c "
import csv, os
from datetime import date

trades_file = '${TRADES_FILE}'
daily_log = '${DAILY_LOG}'
capital = ${CAPITAL}
today = str(date.today())

open_count = 0
closed_total = 0
closed_today_count = 0
total_pnl = 0.0
wins = 0

with open(trades_file, 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row['status'] == 'open':
            open_count += 1
        elif row['status'] == 'closed':
            closed_total += 1
            total_pnl += float(row.get('pnl_amount', 0))
            if float(row.get('pnl_pct', 0)) > 0:
                wins += 1
            if row.get('exit_date') == today:
                closed_today_count += 1

win_rate = (wins / closed_total * 100) if closed_total > 0 else 0

with open(daily_log, 'a', newline='') as f:
    writer = csv.writer(f)
    writer.writerow([today, open_count, closed_today_count, closed_total,
                     f'{win_rate:.1f}', f'{total_pnl:.0f}', capital])
" 2>/dev/null
  log "Daily log updated: ${DAILY_LOG}"
}

# ── Schedule / Unschedule cron ───────────────────────────────
do_schedule() {
  SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/paper_trade.sh"
  LOG_PATH="$(cd "$(dirname "$0")" && pwd)/backend/paper_trades/cron.log"

  # Cron: Mon-Fri at 11:00 AM IST
  CRON_LINE="0 11 * * 1-5 cd $(cd "$(dirname "$0")" && pwd) && ${SCRIPT_PATH} full >> ${LOG_PATH} 2>&1"

  # Check if already scheduled
  if crontab -l 2>/dev/null | grep -q "paper_trade.sh"; then
    warn "Cron job already exists. Use 'unschedule' to remove first."
    crontab -l 2>/dev/null | grep "paper_trade.sh"
    return
  fi

  (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
  ok "Cron job installed: Mon-Fri at 11:00 AM"
  ok "Logs: ${LOG_PATH}"
  echo ""
  echo "  Current crontab:"
  crontab -l 2>/dev/null | grep "paper_trade" || echo "  (none)"
}

do_schedule_realtime() {
  SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/paper_trade.sh"
  LOG_PATH="$(cd "$(dirname "$0")" && pwd)/backend/paper_trades/cron_realtime.log"
  PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

  # Check if already scheduled
  if crontab -l 2>/dev/null | grep -q "paper_trade.sh evaluate"; then
    warn "Realtime cron already exists. Use 'unschedule' to remove first."
    crontab -l 2>/dev/null | grep "paper_trade.sh"
    return
  fi

  # Daily full routine at 11:00 AM (scan + open trades)
  DAILY_LINE="0 11 * * 1-5 cd ${PROJECT_DIR} && ${SCRIPT_PATH} full >> ${LOG_PATH} 2>&1"
  # Evaluate open positions every 15 minutes during market hours (9:30 - 15:30 IST)
  EVAL_LINE="*/15 9-15 * * 1-5 cd ${PROJECT_DIR} && ${SCRIPT_PATH} evaluate >> ${LOG_PATH} 2>&1"

  (crontab -l 2>/dev/null; echo "$DAILY_LINE"; echo "$EVAL_LINE") | crontab -
  ok "Realtime evaluation cron installed:"
  ok "  Daily scan: Mon-Fri at 11:00 AM"
  ok "  Evaluate: Mon-Fri every 15 min, 9:30 AM - 3:30 PM"
  ok "  Logs: ${LOG_PATH}"
  echo ""
  echo "  Current crontab:"
  crontab -l 2>/dev/null | grep "paper_trade" || echo "  (none)"
}

do_unschedule() {
  if ! crontab -l 2>/dev/null | grep -q "paper_trade.sh"; then
    log "No paper_trade cron job found"
    return
  fi

  crontab -l 2>/dev/null | grep -v "paper_trade.sh" | crontab -
  ok "Cron job removed (both daily and realtime)"
}

# ── Main ─────────────────────────────────────────────────────
CMD="${1:-scan}"

case "$CMD" in
  scan|open)     do_scan ;;
  evaluate|eval) do_evaluate ;;
  report)        do_report ;;
  full|daily)    do_full ;;
  schedule)      do_schedule ;;
  realtime)      do_schedule_realtime ;;
  unschedule)    do_unschedule ;;
  -h|--help|help)
    echo "Usage: $0 {scan|evaluate|report|full|schedule|realtime|unschedule}"
    echo ""
    echo "Commands:"
    echo "  scan         Trigger scan & open paper trades for new signals"
    echo "  evaluate     Check open trades against current prices, close if SL/TGT/time hit"
    echo "  report       Show full P&L report"
    echo "  full         Run evaluate + scan + report (daily routine)"
    echo "  schedule     Install daily 11 AM cron (Mon-Fri)"
    echo "  realtime     Install daily 11 AM scan + 15-min evaluations during market hours"
    echo "  unschedule   Remove all cron jobs"
    echo ""
    echo "Risk Parameters (edit in script):"
    echo "  Capital:              Rs.${CAPITAL}"
    echo "  Max per trade:        ${MAX_POSITION_PCT}% (Rs.$((CAPITAL * MAX_POSITION_PCT / 100)))"
    echo "  Stop loss:            ${STOP_LOSS_PCT}%"
    echo "  Target:               ${TARGET_PCT}%"
    echo "  Min strength:         ${MIN_SIGNAL_STRENGTH}/10"
    echo "  Max open trades:      ${MAX_OPEN_TRADES}"
    echo "  Hold limit:           ${HOLD_LIMIT_DAYS} days"
    echo "  Min price:            Rs.${MIN_PRICE}"
    echo "  Max sector positions: ${MAX_SECTOR_POSITIONS}"
    echo "  Portfolio heat limit: ${MAX_PORTFOLIO_HEAT_PCT}%"
    echo "  Drawdown breaker:     ${MAX_DRAWDOWN_PCT}%"
    echo ""
    echo "Files:"
    echo "  Trades:      ${TRADES_FILE}"
    echo "  Daily log:   ${DAILY_LOG}"
    ;;
  *)
    err "Unknown command: $CMD"
    echo "Usage: $0 {scan|evaluate|report|full|schedule|realtime|unschedule}"
    exit 1
    ;;
esac
