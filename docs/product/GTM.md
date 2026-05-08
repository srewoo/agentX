# Go-to-Market

## Beachhead persona

**"Rohan, 32, Bangalore. 6 yrs in tech, ₹35L portfolio (₹20L equity, ₹10L MFs, ₹5L F&O margin). Trades 2–3 swings/week + 1–2 weekly options expiries. Active on fintwit, follows P R Sundar, pays for one Telegram channel he half-trusts. Uses screener.in + Kite + 1–2 Telegram tip groups + tradingview daily. Has tried Tickertape Pro, didn't renew."**

We win Rohan if:
1. The first time he opens screener.in/RELIANCE after install, he sees something useful he didn't already know (FII flow + OI buildup synthesised).
2. He gets a Telegram alert on a position **before** the move, not after.
3. The "why this BUY?" panel makes his Telegram tip channels feel slow.

## Distribution channels (ranked by expected efficiency)

### 1. Chrome Web Store SEO (highest leverage, lowest cost)
- Optimise listing for queries: "indian stock market chrome extension", "nse signals chrome", "screener.in helper", "options chain analyzer chrome".
- 60-second hero video showing the overlay on screener.in/RELIANCE and on tradingview BANKNIFTY.
- Solicit reviews via in-app prompt on day 14 of free use.
- Target: 5,000 installs in first 90 days organic + paid.

### 2. Fintwit (twitter.com/x.com)
- Founder's account posts daily: a chart screenshot with the agentX overlay visible — "this is what the score looked like yesterday on $RELIANCE before the 2% pop". Repeatable, free.
- Reply-guy strategy on existing fintwit threads about F&O — show the overlay as a comment, not a pitch.
- Sponsor 2–3 mid-tier fintwit accounts (10K–50K followers) at ₹15–30K/post.

### 3. YouTube creator collabs
Target list (in priority order):
- **P R Sundar** — F&O audience, 2.7M subs. Pricing TODO; expect ₹3–5L for a sponsored mention. High intent.
- **Akshat Shrivastava** — long-term equity, 1.5M subs. Lower intent on F&O but broad funnel.
- **Pranjal Kamra** — 5M subs, mass-market. Brand awareness, lower conversion.
- **CA Rachana Ranade** — 4M subs. Trust signal, especially for tax harvesting (when shipped).
- **Ravi Handa / Vivek Bajaj / Asmita Patel** — F&O-specific, smaller but converted audiences.

Allocate ₹15–20L for the first 4 creator collabs. Track every install with a per-creator UTM and a 90-day retention cohort.

### 4. Telegram
- Don't run a tip channel — it's a SEBI risk and a credibility risk. Run a **product channel**: daily F&O OI digest, RBI policy primer day-of, results-day reaction tracker.
- Cross-promote with 5–10 mid-size finance Telegram channels (10K–50K subs).

### 5. Content SEO (slow build, compounding)
- Long-tail blog/docs: "How to read OI buildup on Bank Nifty", "FII vs DII flow — what it means for your swing trades", "STT-aware backtest in Python".
- Open-source pieces of the engine (factor scorers in `recommendation_factors.py` are already isolated) → GitHub stars → developer credibility.

### 6. Broker partnership (later, 6–12mo)
Once we hit ~10K paid users, approach a tier-2 broker (5paisa, Dhan, Fyers) for a co-marketing deal — agentX listed in their app's "tools" surface. Tier-1 brokers (Zerodha, Groww) won't partner pre-scale.

## Launch sequence

### T-30 (one month before public launch)
- Closed beta: 100 invites from fintwit + Telegram. Free Elite for life in exchange for weekly feedback.
- Lock SEBI registration or RA partnership (`SEBI_COMPLIANCE.md` T1).
- Fix the 12 failing backend tests (`INTEGRATION_TODO.md`) — esp. screener and alert dedup.
- Ship the "Why this BUY?" panel (A1) — that's the demo moment.
- Record the 60-sec hero video.

### T0 (Public launch)
- Chrome Web Store goes live.
- Founder launches on fintwit + Hacker News + Reddit r/IndianStockMarket.
- First creator video lands within 7 days (book in advance).
- Razorpay Pro tier live; 14-day free trial.

### T+30
- Daily Telegram digest going out.
- 2nd and 3rd creator video.
- First retention check: D7, D14 cohorts. Iterate onboarding if D7 < 40%.

### T+90
- 4th–5th creator video.
- First referral programme.
- First paid case-study post: "Rohan made ₹X using agentX on RELIANCE Mar 14 OI signal" (with consent).
- Soft target: 1,000 paid users, ₹10L MRR run-rate.

## Counter-positioning lines (use sparingly)
- vs Tickertape: "Their score is a black box. Ours shows you the 8 factors and the weights — you decide."
- vs Telegram tip channels: "Every call is logged with a timestamp and the data behind it. No screenshots, no edits."
- vs Sensibull: "We're not trying to replace Sensibull on options strategy. We tell you which stock to look at — they help you express the trade."

## Anti-patterns to avoid
- Don't run paid Google search ads for "stock tips" — wrong audience and SEBI sensitivity.
- Don't lean on Instagram Reels — wrong density of viewer for our beachhead.
- Don't overspend on YouTube before the product has D30 ≥ 35% paid retention. Creator spend amplifies leaks.
