# Competitive Teardown — Indian retail equity research tools

Snapshot, May 2026. Pricing/feature notes are best-effort and should be re-verified before any GTM doc goes external — flagged TODO where uncertain.

## Feature matrix

| Capability | agentX | Tickertape Pro | Stockedge Premium | Smallcase | Trendlyne Pro | Sensibull Pro | Streak |
|---|---|---|---|---|---|---|---|
| Distribution | **Chrome extension overlay** | Web + mobile app | Mobile-first | Web (broker-embedded) | Web | Web (broker-embedded) | Web (broker-embedded) |
| Multi-factor score | 8 factors, **decomposed + weights visible** | Proprietary score (opaque) | Scans, no single score | N/A (basket-level) | DVM / proprietary | N/A (options) | User builds own |
| F&O OI / heatmap | Q+1 (R1) | Limited | Yes | No | Yes | **Best-in-class** | Strategy backtest |
| Options chain analyzer | 6mo | Basic | Yes | No | Basic | **Best-in-class** | Yes |
| Indian events (RBI, results) | Q+1 | Calendar only | Yes | No | Yes | F&O expiry only | No |
| Insider/bulk/block deals | Q+1 | Yes | Yes | No | Yes | No | No |
| Pre-open / GIFT Nifty | Q+1 | No | Partial | No | Partial | No | No |
| Portfolio analytics (Sharpe, drawdown, beta, FIFO) | **Yes — shipped** (`portfolio.py`) | Basic | Holdings only | Basket P&L | Limited | No | Limited |
| Broker OAuth import | Kite Q+1, others 6mo | Yes (Smallcase-style) | Manual + import | Native (broker partner) | Limited | Native | Native |
| Tax harvesting (LTCG/STCG) | 6mo (partner Quicko) | No | No | No | No | No | No |
| CAS PDF import | Q+1 | No | Yes | No | Yes | No | No |
| NL screener ("midcaps with FII buying") | 6mo | No | Pre-built scans | No | Pre-built | No | DSL |
| Backtesting | **Shipped** (`backtester.py`, needs STT-aware fix) | Basic | Limited | No | Limited | Strategy-level | **Best-in-class** |
| Paper trading | 6mo | No | Yes | No | No | Yes | Yes |
| Multi-channel alerts (Telegram/email/WA/SMS) | **Shipped** (`notifications.py`) | Email + push | Push | Email | Email + push | Push + email | Email + push |
| Explainable AI ("why this BUY?") | **Q+1 (factor breakdown ready)** | No | No | No | No | No | No |
| News summarisation per ticker | 6mo | Headlines only | Headlines | Curated | Headlines | No | No |
| SEBI Research Analyst registration | TODO (`SEBI_COMPLIANCE.md`) | Yes / partnered | Partnered | Distributor | Yes / partnered | Yes | Distributor |
| Pricing (yr) | ₹999 Pro / ₹2,499 Elite (planned) | ~₹999 | ~₹1,500 | Fee per basket (~2.5% one-time) | ~₹1,500 | ~₹3,500 | ~₹3,000 |

## Our unfair advantage

**The overlay model.** Every competitor in the table wants the user to *come to their domain*. agentX runs **on the page the user is already on** — moneycontrol, screener.in, tickertape, tradingview, NSE, BSE, Kite, Upstox, Groww, Angel One (all 11 hosts wired in `extension/manifest.json`).

Concretely:
- A user on **screener.in/company/RELIANCE/** sees the agentX panel: 8-factor score, FII/DII flow, OI buildup, ATR-based entry/SL/targets, and their existing position context — without leaving the page.
- A user on **tradingview.com** charting BANKNIFTY sees the OI heatmap overlay and pre-open levels.
- A user on **kite.zerodha.com** sees risk context (portfolio beta, drawdown contribution) before placing the order.

No competitor can do this without becoming a browser extension themselves — and that move would cannibalise their own destination traffic. This is a structural moat.

## Where we will lose (and that's fine)
- **Sensibull on F&O strategy execution** — they own the options vertical; we partner or stay out of strategy builders.
- **Smallcase on baskets** — different game; we should integrate, not compete.
- **Streak on algo strategy backtesting** — heavy DSL is a different audience; we serve "I want signals, not to write code".

## Where we win
- **Explainability** — factor-decomposed score is 0 incumbents.
- **Workflow integration** — overlay on existing tabs.
- **Indian-context AI** — NL screener + news summarisation grounded in NSE/BSE entities, INR formatting, and lakh/crore (already in popup).
- **Portfolio analytics depth** — Sharpe/drawdown/beta/FIFO is shipped; most incumbents do P&L only.
