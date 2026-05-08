# agentX — PRD & Roadmap

90-day / 6-month / 12-month plan. Every item has a user problem, a metric to move, an effort estimate (S = ≤1wk, M = 2–4wk, L = 6+wk), and a hard dependency. Themes are interleaved so we ship across alpha, portfolio, AI, distribution, and trust each cycle.

---

## 90-day (Q-now → Q+1) — Land the wedge

Goal: Ship a paid tier, prove the overlay loop, register or partner for SEBI compliance.

### Recommendations & alpha
| # | Feature | User problem | Metric | Effort | Dep |
|---|---|---|---|---|---|
| R1 | **F&O OI heatmap** (top 50 F&O names, change-in-OI vs price) | Retail F&O traders can't read OI buildup at a glance | DAU on Signals tab +30% | M | NSE OI feed (already wired in `nse_fetcher.py`, 52% covered — needs hardening) |
| R2 | **Indian events calendar overlay** — RBI policy, results day, F&O expiry, Budget | Users get blindsided by events on their open positions | Pre-event alert open rate ≥ 50% | S | None |
| R3 | **Pre-open + GIFT Nifty pre-market panel** | "How will my watchlist open?" | Live tab D7 retention +10% | S | GIFT Nifty data via SGX/NSE IFSC scrape |
| R4 | **Insider/bulk/block deals feed** | Surface large prints retail can't see | Alert CTR | S | NSE/BSE filings scrape |
| R5 | **Fix recommendation factor coverage gap** — `signal_tracker.py` 22%, `backtester.py` 17%, `market_regime.py` 12% (`COVERAGE_REPORT.md`) | Trust in scores | NPS on Signals tab | M | Backend test sweep |

### Portfolio & risk
| # | Feature | User problem | Metric | Effort | Dep |
|---|---|---|---|---|---|
| P1 | **Zerodha Kite OAuth** — first broker; ~70% of agentX target audience | "I don't want to type 47 trades by hand" | % of users with ≥1 holding imported | M | Kite Connect API key (₹2K/mo) |
| P2 | **CAS PDF import (CDSL/NSDL)** | Multi-broker users; tax season | One-click portfolio onboard | M | PDF parser + holding mapper |
| P3 | **STT-aware backtest** — current backtester ignores STT/brokerage | Realistic P&L | Backtest accuracy delta vs actual broker P&L | S | Extend `backtester.py` |

### AI & personalisation
| # | Feature | User problem | Metric | Effort | Dep |
|---|---|---|---|---|---|
| A1 | **"Why this BUY?" panel** — render factor contributions from `Recommendation.signals` | Black-box distrust | Signals tab → Recommendation detail click-through | S | UI only; backend already returns contributions |
| A2 | **Daily AI digest (Telegram + email)** — top 5 ideas + portfolio risk note | Re-engagement | D7 / D30 retention | M | Existing `notifications.py` channels |

### Trust & compliance
| # | Feature | User problem | Metric | Effort | Dep |
|---|---|---|---|---|---|
| T1 | **SEBI RA registration OR partner with registered RA** | Legal blocker for pricing tier | Legal sign-off | L | See `SEBI_COMPLIANCE.md` |
| T2 | **Disclaimer kit + audit log of every recommendation** | Regulatory + user trust | 100% of recos logged | S | Append-only log, retention 5y |

### Monetisation & distribution
| # | Feature | User problem | Metric | Effort | Dep |
|---|---|---|---|---|---|
| M1 | **Razorpay Pro tier (₹999/yr)** | Revenue | First 100 paid users | M | T1 must be done first |
| M2 | **Chrome Web Store optimised listing** + 60-sec demo | CAC | Install → activation rate | S | Marketing video |

---

## 6-month (Q+1 → Q+2) — Deepen the moat

### Recommendations & alpha
- **Sector rotation dashboard** — money flow across Nifty sector indices (M).
- **Options chain analyzer** — IV smile, max pain, PCR per strike (M). Compete with Sensibull on equities options for free, leave index F&O to Sensibull initially.
- **Earnings reaction model** — historical post-results drift per stock (S).

### Portfolio & risk
- **Upstox + Groww + Angel One OAuth** (M each, can run in parallel) — cover 90% of retail.
- **Tax harvesting (LTCG/STCG) view** — partner with Quicko or build (M). Indian-specific: ₹1L LTCG exemption, STCG 20% post-Budget 2024 — flag tunable in code.
- **Paper trading league** — leaderboard, weekly reset (M). Distribution loop.

### AI
- **Natural-language screener** — "midcaps with FII buying and rising delivery%" → translates to filter set on existing `screener.py` (M). Pricing wedge for Pro tier.
- **News summarisation with entity tagging** — `llm_analyst.py` already exists; needs entity linker (S).
- **Watchlist suggestions** based on similarity + flow (M).

### Distribution
- **Telegram channel sync** — bring-your-own-channel, agentX cross-posts your alerts (S).
- **Slack integration** for the prosumer / family-office segment (S).
- **Mobile-web companion** (read-only first) (L).
- **Referral programme** — 1 month Pro for each paid referral (S).

---

## 12-month (Q+2 → Q+4) — Platform plays

- **Strategy marketplace** — users publish their factor weights as "blueprints"; revenue share. Direct shot at Smallcase / Streak.
- **API access for prosumers** — sell the recommendation API to small RIAs and family offices.
- **Custom alert grammar** (DSL) — "alert me when RELIANCE has FII net buy ≥ ₹500Cr AND OI in 2800CE drops 20%" (M).
- **Multi-portfolio + family accounts** (M).
- **Regional expansion**: extend to GIFT Nifty / SGX dual-listed products; do NOT touch US equities until India is dominant.
- **Compliance v2** — quarterly RA audit, data lineage UI for every score, SEBI cohort report automation.

---

## Sequencing rules
- Anything that affects "what we recommend" ships **after** SEBI compliance (T1, T2). Until then: framing is "analytics + signals", not advice.
- Each broker integration ships **independently** — never block on three at once.
- AI features ship behind a feature flag with cost cap from day one (`llm_client.py` already has usage tracking).
