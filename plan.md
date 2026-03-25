# StockPilot - Implementation Plan

## Context

Build a Chrome extension (Manifest V3) that acts as an AI-powered trading copilot for Indian stock markets (NSE/BSE). The existing **FinSight** project (`/Users/sharajrewoo/DemoReposQA/FinSight`) already has a mature Python/FastAPI backend with signal detection, multi-LLM integration, market data, watchlist, and alerts. We'll **fork and slim down** that backend rather than rewriting from scratch, saving ~80% of backend effort.

---

## PRD/TRD Critique & Refinements

**What's solid:**
- Core philosophy (LLM explains signals, doesn't invent them) is well-defined
- Layer separation rules are clear and enforceable
- Signal → LLM → UI flow is sound

**What needs refinement:**
- **TRD says Node.js** → Replaced with Python/FastAPI fork (FinSight already has everything working)
- **TRD says "Minimal DB: SQLite/Redis"** → Use SQLite as primary (single user, no MongoDB needed), Redis as optional cache
- **TRD says "Max 1 LLM call per cycle"** → Keep this, but clarify: on-demand AI analysis (user-triggered search) is unlimited; only the background scan cycle is limited to 1 LLM call
- **PRD's "Floating Pill" opening popup** → `chrome.action.openPopup()` requires Chrome 127+. Fallback: open extension in new tab for older Chrome
- **PRD says "works with browser inactive"** → Backend accumulates signals; extension fetches on open. No push notifications needed
- **Missing from PRD/TRD:** Market hours awareness (9:15 AM–3:30 PM IST), stale data flagging, SQLite WAL mode for concurrency, service worker ephemeral lifecycle handling

---

## Project Structure

```
/Users/sharajrewoo/DemoReposQA/agentX/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app, CORS, lifespan, router mount
│   │   ├── config.py                # pydantic-settings, env validation
│   │   ├── database.py              # SQLite via aiosqlite, schema init
│   │   ├── models.py                # Pydantic request/response models
│   │   ├── utils.py                 # safe_float, sanitize_symbol, parse_llm_json
│   │   ├── services/
│   │   │   ├── data_fetcher.py      # yfinance wrappers (from server.py:535-636)
│   │   │   ├── technicals.py        # RSI, MACD, ADX, Bollinger, SMA (from server.py:638-793 + math_utils.py)
│   │   │   ├── market_regime.py     # Market regime detection (fork of market_regime.py)
│   │   │   ├── signal_engine.py     # NEW: deterministic signal detectors
│   │   │   ├── llm_client.py        # Multi-provider LLM dispatch (fork of llm_client.py)
│   │   │   ├── llm_analyst.py       # NEW: prompt builder + JSON parser
│   │   │   ├── sentiment.py         # RSS scraping + keyword sentiment (fork)
│   │   │   ├── orchestrator.py      # NEW: scheduler → signals → LLM → cache → serve
│   │   │   └── cache.py             # Redis with graceful degradation (fork)
│   │   └── routers/
│   │       ├── stocks.py            # /stocks/search, quote, history, technicals
│   │       ├── analysis.py          # /stocks/{symbol}/ai-analysis
│   │       ├── signals.py           # /signals/latest, read, dismiss, trigger
│   │       ├── watchlist.py         # /watchlist CRUD
│   │       ├── market.py            # /market/indices, news
│   │       └── settings.py          # /settings GET/POST
│   ├── tests/
│   ├── requirements.txt
│   ├── .env.example
│   └── run.py
│
├── extension/
│   ├── manifest.json                # Manifest V3
│   ├── vite.config.ts
│   ├── package.json
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   ├── src/
│   │   ├── background/
│   │   │   └── service-worker.ts    # chrome.alarms polling, badge, message passing
│   │   ├── content/
│   │   │   ├── mount.ts             # Shadow DOM injection
│   │   │   └── pill.tsx             # Floating pill overlay
│   │   ├── popup/
│   │   │   ├── index.html
│   │   │   ├── main.tsx
│   │   │   ├── App.tsx              # Tab navigation (Dashboard/Search/Watchlist/Settings)
│   │   │   ├── pages/
│   │   │   │   ├── Dashboard.tsx    # Signal feed
│   │   │   │   ├── Search.tsx       # Stock search + AI analysis
│   │   │   │   ├── Watchlist.tsx    # Watchlist management
│   │   │   │   └── Settings.tsx     # All preferences
│   │   │   ├── components/
│   │   │   │   ├── SignalCard.tsx
│   │   │   │   ├── StockQuote.tsx
│   │   │   │   ├── AnalysisPanel.tsx
│   │   │   │   └── SearchBar.tsx
│   │   │   └── hooks/
│   │   │       ├── useApi.ts
│   │   │       ├── useSignals.ts
│   │   │       └── useSettings.ts
│   │   └── shared/
│   │       ├── api.ts               # Backend API client
│   │       ├── types.ts             # TypeScript interfaces
│   │       ├── constants.ts
│   │       └── storage.ts           # chrome.storage helpers
│   └── assets/
├── .env.example
└── .gitignore
```

---

## Backend Fork Strategy

### Copy & adapt from FinSight:

| FinSight Source | StockPilot Target | Changes |
|---|---|---|
| `llm_client.py` | `services/llm_client.py` | Remove image support, keep all 3 providers |
| `server.py:535-636` (resilient_fetch_history) | `services/data_fetcher.py` | Extract as standalone, yfinance-only initially |
| `server.py:638-793` (compute_technicals, ADX, S/R) | `services/technicals.py` | Merge with math_utils.py |
| `server.py:1984-2061` (detect_breakout) | `services/signal_engine.py` | Extend with price/volume spike detectors |
| `server.py:1383-1442` (AI analysis prompt) | `services/llm_analyst.py` | Extract prompt building, decouple from endpoint |
| `market_regime.py` | `services/market_regime.py` | Copy with minor cleanup |
| `sentiment.py` | `services/sentiment.py` | Default to keyword-only, LLM opt-in for watchlist |
| `cache.py` | `services/cache.py` | Copy verbatim |

### Do NOT copy:
- `auth.py` → Replace with simple API key middleware
- `broker.py` → No trading
- `options.py` → Not needed
- Firebase/FCM → Not needed
- `encryption.py` → Not needed (single user)

---

## New Backend Components

### SQLite Schema (database.py)

```sql
CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,    -- price_spike, volume_spike, breakout, rsi_extreme, macd_crossover, sentiment_shift
    direction TEXT NOT NULL,      -- bullish, bearish, neutral
    strength INTEGER NOT NULL,    -- 1-10
    reason TEXT NOT NULL,
    risk TEXT,
    llm_summary TEXT,
    current_price REAL,
    metadata TEXT,                -- JSON blob
    created_at TEXT NOT NULL,
    read BOOLEAN DEFAULT 0,
    dismissed BOOLEAN DEFAULT 0
);

CREATE TABLE watchlist (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT DEFAULT 'NSE',
    added_at TEXT NOT NULL
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### Signal Orchestrator (orchestrator.py)

Runs every N minutes (configurable, default 30) via APScheduler:

1. Get scan list = watchlist stocks + NIFTY 50 top movers
2. Fetch data via `data_fetcher` (semaphore=5 concurrent, 0.5s delay between batches)
3. Run `signal_engine` detectors per symbol (deterministic): price spike (>3%), volume spike (>2x avg), RSI extreme, MACD crossover, breakout
4. Filter by risk mode: conservative (strength >= 7), balanced (>= 5), aggressive (>= 3)
5. Pick strongest signal → 1 LLM call via `llm_analyst.enrich_signal` for narrative
6. Store signals in SQLite
7. Cache technical data in Redis (TTL 30 min)

Market hours awareness: 9:15 AM–3:30 PM IST, Mon-Fri. Outside hours, flag signals as stale.

### Signal Engine (signal_engine.py)

```python
detect_price_spike(current, previous, threshold_pct=3.0) -> Signal | None
detect_volume_spike(current_vol, avg_vol, threshold_ratio=2.0) -> Signal | None
detect_rsi_extreme(rsi, overbought=70, oversold=30) -> Signal | None
detect_macd_crossover(macd_curr, macd_prev, signal_curr, signal_prev) -> Signal | None
detect_breakout(df, support_resistance, technicals) -> Signal | None  # from FinSight
scan_symbol(symbol, df, previous_price) -> list[Signal]  # runs all detectors
```

---

## API Contract

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Health check (db, cache, last_scan) |
| GET | `/api/signals/latest?since=&limit=50` | Latest signals |
| POST | `/api/signals/{id}/read` | Mark read |
| POST | `/api/signals/{id}/dismiss` | Dismiss |
| POST | `/api/scan/trigger` | Manual scan |
| GET | `/api/stocks/search?q=` | Search symbols |
| GET | `/api/stocks/{symbol}/quote` | Current quote |
| GET | `/api/stocks/{symbol}/technicals` | Technical indicators |
| POST | `/api/stocks/{symbol}/ai-analysis` | On-demand AI analysis |
| GET | `/api/watchlist` | Get watchlist |
| POST | `/api/watchlist` | Add stock |
| DELETE | `/api/watchlist/{symbol}` | Remove stock |
| GET | `/api/market/indices` | NIFTY/SENSEX |
| GET | `/api/market/news?limit=10` | News with sentiment |
| GET | `/api/settings` | Get settings |
| POST | `/api/settings` | Update settings |

### Signal Type (TypeScript)

```typescript
interface Signal {
  id: string;
  symbol: string;
  signal_type: 'price_spike' | 'volume_spike' | 'breakout' | 'rsi_extreme' | 'macd_crossover' | 'sentiment_shift';
  direction: 'bullish' | 'bearish' | 'neutral';
  strength: number; // 1-10
  reason: string;
  risk: string;
  llm_summary: string | null;
  current_price: number;
  created_at: string; // ISO
  read: boolean;
}
```

---

## Chrome Extension Architecture

### Service Worker (background)
- Uses `chrome.alarms` (NOT `setInterval` — service workers are ephemeral in MV3)
- On alarm: poll `/api/signals/latest?since=lastPoll`, write to `chrome.storage.local`, update badge
- Message handlers for popup/content script communication

### Content Script (floating pill)
- Injects via Shadow DOM (style isolation)
- 48px circle, bottom-right corner, shows unread count
- Pulses on new signals via `chrome.storage.onChanged`
- Click → `chrome.action.openPopup()` (Chrome 127+) or fallback to new tab

### Popup (400x600px, React + Tailwind)
- 4 tabs: Dashboard | Search | Watchlist | Settings
- Dashboard: signal feed from chrome.storage, expandable SignalCards
- Search: debounced ticker search → quote + "Run AI Analysis" button
- Watchlist: add/remove with current prices
- Settings: alert frequency, risk mode, signal toggles, LLM config

---

## Implementation Phases

### Phase 1: Backend Skeleton (2 days)
- Project structure, `config.py`, `database.py`, `main.py`, `utils.py`, `run.py`
- `requirements.txt`, `.env.example`

### Phase 2: Fork Data Services (2 days)
- Fork: `llm_client.py`, `data_fetcher.py`, `technicals.py`, `market_regime.py`, `sentiment.py`, `cache.py`
- Unit tests for technicals with fixture data

### Phase 3: Signal Engine + Orchestrator (3 days)
- Build `signal_engine.py` with all detectors
- Build `llm_analyst.py` (prompt builder)
- Build `orchestrator.py` (scheduler + full scan cycle)
- Unit tests for each detector, integration test for full cycle

### Phase 4: API Routers (2 days)
- All endpoints from the API contract
- Integration tests with httpx.AsyncClient

### Phase 5: Extension Scaffold (1 day) — can start parallel with Phase 3
- Vite + React + TypeScript + Tailwind setup
- `manifest.json`, `shared/` (api, types, storage, constants)

### Phase 6: Service Worker + Content Script (2 days)
- `chrome.alarms` polling, badge updates, message passing
- Shadow DOM pill with pulse animation

### Phase 7: Popup UI (3 days)
- Dashboard, Search, Watchlist, Settings pages
- SignalCard, AnalysisPanel, SearchBar, StockQuote components
- Hooks: useApi, useSignals, useSettings

### Phase 8: Integration Testing + Polish (2 days)
- E2E: backend → scan → signals → extension → popup display
- CORS, service worker lifecycle, settings persistence
- Error states: backend down, LLM key missing, no internet
- Loading/error/empty states in all views

---

## Key Gotchas

- **MV3 service workers are ephemeral** — use `chrome.alarms`, never `setInterval`
- **CORS** — backend must allow `chrome-extension://*` origin; use `allow_origins=["*"]` for dev
- **yfinance rate limiting** — semaphore=5, 0.5s delays between batches, handle empty DataFrames
- **SQLite concurrency** — enable WAL mode (`PRAGMA journal_mode=WAL`) on init
- **Popup closes on outside click** — in-flight API calls cancel; use service worker for must-complete operations
- **`chrome.action.openPopup()`** — Chrome 127+ only; fallback to tab for older versions
- **Extension bundle** — all data processing stays on backend; extension is a thin UI client

---

## Verification

1. **Backend unit tests**: `pytest backend/tests/` — signal detectors, technicals, prompt building
2. **Backend integration**: start server, hit each endpoint with curl/httpx, verify response schemas
3. **Extension manual test**: load unpacked in `chrome://extensions`, verify pill appears, popup opens, signals display
4. **Full flow**: backend running → trigger manual scan → signals appear in Dashboard → search a stock → AI analysis returns
5. **Edge cases**: backend down (extension shows cached signals), no LLM key (raw signals without narrative), market closed (stale data flagged)
