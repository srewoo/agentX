# Monetisation

## Tiers

| Capability | Free | **Pro — ₹999/yr** | **Elite — ₹2,499/yr** |
|---|---|---|---|
| Multi-factor score | 5 watchlist symbols | 50 | Unlimited |
| Recommendation horizons | Swing only | Intraday + Swing + Positional | All |
| F&O OI heatmap (R1) | View only, top 10 | Top 50 + alerts | Custom strikes + intraday refresh |
| Options chain analyzer | — | Basic | Full IV smile + max pain |
| Portfolio analytics (Sharpe, drawdown, beta, FIFO) | 1 portfolio, manual | 3 portfolios, broker OAuth | Unlimited + family accounts |
| Broker OAuth (Kite/Upstox/Groww/Angel) | — | Yes | Yes |
| Tax harvesting (LTCG/STCG) | — | View only | Optimiser + CSV export |
| Backtester | — | 5 runs / mo | Unlimited + STT-aware |
| Alerts — Telegram/email | 5 active | 50 | Unlimited |
| Alerts — WhatsApp/SMS | — | 100 / mo | 1,000 / mo |
| NL screener ("midcaps with FII buying") | — | 20 queries / day | Unlimited |
| AI news summarisation | Headlines only | 100 summaries / mo | Unlimited |
| API access | — | — | 10K calls / mo |
| SEBI audit log export (own trades) | — | Yes | Yes |
| Priority support | — | Email | Email + Telegram channel |

## Anchor pricing rationale

| Comp | Price/yr | What we beat them on |
|---|---|---|
| Tickertape Pro | ~₹999 | Same anchor; we offer F&O + portfolio analytics + overlay |
| Stockedge Premium | ~₹1,500 | We're cheaper at Pro, deeper at Elite |
| Sensibull Pro | ~₹3,500 | We're not Sensibull on options; Elite ₹2,499 covers equity F&O for users who don't trade index strategies |
| Smallcase | Fee-based (2.5% one-time per basket) | Different model; we integrate |

**ARPU target:** ₹1,200 blended (assumes 70% Pro, 30% Elite among paid). Tickertape's reported blended ARPU is ~₹950 (TODO — verify before any external doc). Our higher anchor is justified by F&O alpha + portfolio depth.

## Free → Paid funnel

1. **Hook (Free):** 8-factor score on every page they research. The "why this score?" panel is visible but **horizon = swing only** and watchlist capped at 5.
2. **Friction:** When they hit the cap or want intraday/positional or want to import broker holdings → upgrade prompt.
3. **Trial:** 14-day Pro trial, no card required (Razorpay subscription with delayed first charge). India is card-shy; UPI autopay via Razorpay is the right rail.
4. **Conversion target:** 4% Free → Pro by month-3 of activation; 12% Pro → Elite by month-6.

## Razorpay integration plan
- Subscriptions API with UPI AutoPay + cards.
- **₹0 first month** trial; Razorpay supports zero-amount auth for UPI mandates.
- Webhook → backend `/api/billing/razorpay/webhook` → flips a `tier` column.
- GST handling: 18% on SaaS; price displayed inclusive in India.
- Annual plans default; monthly plans (₹149) exist but de-emphasised — improves LTV.
- Refund policy: pro-rated within 7 days, no questions.

## Cohort retention assumptions

| Cohort | M1 | M3 | M6 | M12 |
|---|---|---|---|---|
| Free | 100% | 35% | 18% | 10% |
| Pro | 100% | 80% | 65% | 55% |
| Elite | 100% | 88% | 75% | 65% |

Assumptions are aggressive vs Tickertape's reported 25% D30 paid retention; justified by (a) workflow integration making the product sticky and (b) F&O traders with active capital are higher-intent than passive equity buyers. **Re-baseline after first 500 paid users.**

## LTV / CAC sketch

- **Pro LTV** = ₹999 × 0.55 retention × ~3.5y avg lifetime ≈ **₹1,920**.
- **Elite LTV** = ₹2,499 × 0.65 × ~4y ≈ **₹6,500**.
- **Blended LTV** ≈ ₹2,300.
- **CAC target** ≤ ₹600 (LTV:CAC ≥ 3.8). Achievable via:
  - Chrome Web Store organic (CAC ≈ ₹0 for the install, ~₹200 effective per paid via funnel).
  - YouTube creator collabs (₹50K–₹2L per video; track per-creator UTM).
  - Telegram cross-promotion with finance channels.
- **Payback target:** ≤ 6 months.

## Adjacent monetisation (12-month)
- **Strategy marketplace** — creators publish their factor weights as "blueprints"; agentX takes 20%.
- **API access** for small RIAs and family offices — ₹25K/mo starter tier.
- **White-label for brokers** — sell the recommendation engine to a tier-2 broker as an in-app tool.
