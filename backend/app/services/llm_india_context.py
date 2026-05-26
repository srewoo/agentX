"""Shared Indian-market context for every LLM prompt in agentX.

Goal: every LLM layer (signal judge, debate bull/bear/judge, multi-perspective
specialists, deep analyst) should ground its reasoning in NSE/BSE-specific
realities rather than treat the stock as a generic global equity.

The strings here are deliberately *terse*. They are prepended to system
prompts as a "ground truth briefing" — the model is expected to apply
this knowledge implicitly, not regurgitate it. Each block is < ~150
tokens to keep the prompt-cache hit and per-call cost low.

Edit this file when:
- The SEBI regulatory landscape changes materially (T+0 rollout, new
  short-sell rules, ASM/GSM threshold adjustments, etc.).
- A new structural Indian-market dynamic emerges (e.g. crypto-style
  derivatives, new index methodology).
- Walk-forward backtests reveal a missing factor (e.g. promoter pledge
  level mattering more than current weight assumes).
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────
# Universal preamble — applied to EVERY agent's system prompt
# ─────────────────────────────────────────────────────────────────────────

INDIAN_MARKET_PREAMBLE = """\
INDIAN-MARKET OPERATING CONTEXT (apply implicitly, do NOT recite back):
- Venue: NSE (primary, T+1) and BSE. Cash + F&O. Lot sizes vary by symbol.
- Sessions: 09:15–15:30 IST (15:40 close auction). No after-hours trading
  for equity cash. Block-deal window 08:45-09:00 and 14:05-14:20.
- Settlement: T+1 for cash, T+0 (beta) for select scrips. SLB (securities
  lending & borrowing) available for select F&O names.
- F&O ban list: symbols breaching 95% of market-wide position limit are
  banned from new positions next session. Existing positions can only
  reduce. New trades on banned names will be REJECTED — surface this
  as a hard risk factor.
- ASM (Additional Surveillance Measure) / GSM groups: SEBI flags
  illiquid/volatile names to Stage 1-4 with margin penalties (50%–100%)
  and price-band tightening. Names in ASM/GSM should be treated as
  high-risk regardless of technical setup.
- T2T / Z group: trade-to-trade settlement — no intraday squareoff. Z
  group: companies with compliance issues. Avoid recommending entries
  on either unless the user explicitly asks.
- Circuit filters: 2/5/10/20% bands per scrip; the index has 10/15/20%
  bands that halt trading for 45min / 1hr / rest of session. Once a
  scrip hits its lower band, exits become impossible until it reopens.
- Tick size: ₹0.05 (cash) for prices ≥ ₹100; ₹0.01 below. Round all
  entry/stop/target prices to the tick.
- Brokerage & STT: typical round-trip cost on a delivery trade is
  ~0.25-0.40% (STT 0.1% on each side + brokerage + DP charges +
  exchange fees + GST). Intraday is ~0.05% round-trip. Factor this
  into R:R when proposing trades.
"""


# ─────────────────────────────────────────────────────────────────────────
# Flow & positioning context (FII/DII, Nifty/BankNifty, India VIX)
# ─────────────────────────────────────────────────────────────────────────

FLOW_CONTEXT = """\
FLOW & POSITIONING:
- FII (Foreign Institutional Investors) net buy/sell is the primary
  marginal driver for Nifty 50 large-caps. Persistent FII selling (≥3
  consecutive sessions, ≥ ₹2,000 Cr/day) flips the index regime to
  defensive regardless of technicals. FII direction is reported next
  session — don't pretend you have live data.
- DII (Domestic Institutional Investors: MFs, insurance, pension) flow
  often counter-trades FII flow. Persistent DII buying + FII selling =
  range-bound chop. DII-led rallies (FII flat or selling) are weaker
  and prone to mean-reversion.
- India VIX: < 12 complacent (mean-reversion bias), 12-18 normal, 18-25
  caution (trend-following bias), > 25 panic (don't fight the prevailing
  direction). VIX > 30 = global-shock regime.
- Nifty 50 / Bank Nifty / Nifty IT / Nifty Auto positioning matters per
  sector. Treat a stock's setup as weaker if the parent sector index
  is breaking down structurally and stronger if confirming.
- Currency: a weakening rupee (USD/INR > 86) is structurally bullish
  for IT and pharma exporters, bearish for OMCs and aviation. The
  inverse is true.
- Crude (Brent): rising crude (> $90/bbl) is bearish for OMCs and paint
  companies, neutral-to-bullish for ONGC/Reliance. A 10% move in
  crude in a week is regime-shifting.
"""


# ─────────────────────────────────────────────────────────────────────────
# Sector-specific narratives
# ─────────────────────────────────────────────────────────────────────────

SECTOR_PLAYBOOK = """\
SECTOR PLAYBOOK (apply when scoring fundamentals/macro):
- PSU Banks: ROE-driven, NIM-sensitive to RBI repo. Valued at P/BV.
  Sensitive to NPA cycle and government recapitalisation announcements.
- Private Banks: Trade at P/BV premium to PSU. CASA ratio, slippage
  guidance, and NIM compression are the watch items.
- NBFCs / HFCs: Cost-of-funds sensitive. Liquidity events (e.g. IL&FS,
  DHFL hangover) cause sector-wide derating. AAA-rated parent matters.
- IT Services (TCS, INFY, WIPRO, HCLTECH, TECHM, LTIM, COFORGE): USD-INR
  tailwind, BFSI vertical exposure, deal-TCV growth, attrition trend.
  Q1 (June) and Q3 (Dec) are typically guidance quarters.
- Pharma: USFDA observations are the binary catalysts. EIR/Form 483
  classification matters. Domestic pricing (NLEM) is a constant headwind.
- FMCG (HUL, ITC, Nestle, Britannia, Dabur, Marico): Volume growth >
  pricing growth = healthy. Monsoon, rural inflation, and crude
  derivatives (palm oil, packaging) are key inputs.
- Auto: Monthly sales volumes (1st of month), 2W/PV/CV mix, inventory
  channel checks, BS-VI compliance costs. Tata Motors = JLR cycle. M&M
  = tractor (monsoon) + SUV.
- Cement: South vs North price differentials matter. Coal/pet-coke
  costs and capacity utilisation rates are the levers.
- Metals (Tata Steel, JSW Steel, Hindalco, Vedanta): Global LME-driven.
  Chinese steel exports + property cycle move the needle. Aluminium
  premium tracks LME + alumina spread.
- Real estate / Cement / Capital goods: India capex cycle proxies.
  Stronger in the second half of a fiscal year (Oct–Mar).
- Power utilities: Plant load factor (PLF), merchant tariffs, fuel
  cost pass-through. PSU dominated → policy-sensitive.
"""


# ─────────────────────────────────────────────────────────────────────────
# Microstructure red flags
# ─────────────────────────────────────────────────────────────────────────

MICROSTRUCTURE_RED_FLAGS = """\
RED FLAGS — surface these explicitly:
- Promoter pledge > 50% of holding (sell-pressure risk on margin call).
- ADR/GDR price diverging from local close by > 2% (arbitrage flow,
  often direction-leading).
- Bulk/Block deal disclosure: institutional accumulation (>2% of float)
  is a positive flow signal; promoter selling is a negative one.
- Recent QIP / preferential allotment dilution.
- SEBI/RBI orders, ED/IT raids reported in past 30 days.
- Earnings call no-shows or guidance withdrawal (M&M, Coal India
  precedents).
- ICAI/auditor qualifications, resignation of independent directors.
- ASM/GSM stage upgrade in past 7 sessions (forced delivery requirement).
"""


# ─────────────────────────────────────────────────────────────────────────
# Seasonality
# ─────────────────────────────────────────────────────────────────────────

SEASONALITY_NOTES = """\
SEASONALITY (apply when adjacent to these dates):
- Mar 28-31: financial year-end window-dressing → quality large-caps
  rally, low-quality names face MTM-driven selling.
- Pre-Budget (Jan-Feb): consumption, infrastructure, defence stocks see
  speculative bid; rate-sensitive sectors front-run RBI guidance.
- Monsoon: June-Sept rainfall vs LPA. Below 90% LPA = bearish for FMCG
  rural, fertiliser, tractor, two-wheeler. Above 105% = mildly negative
  for cement (logistics) and power (lower load).
- Festive season (Sept-Nov): Auto + consumer durable + retail watch
  October dispatch data. Diwali week often produces a "Muhurat" rally.
- Q4 results (Apr-May): banks first, then IT, then everything else.
  Index moves are exaggerated until results normalise.
- Election years (Lok Sabha + State): Pre-election → defensive bias
  rotates to capex/PSU on majority result, broad sell-off on hung
  outcome.
"""


# ─────────────────────────────────────────────────────────────────────────
# Helper: assemble the briefing per layer
# ─────────────────────────────────────────────────────────────────────────

def briefing(
    *,
    include_flow: bool = True,
    include_sector: bool = True,
    include_red_flags: bool = True,
    include_seasonality: bool = False,
) -> str:
    """Compose the briefing block to prepend to a system prompt.

    Layers tune what they pull in:
    - signal judge: preamble + flow + red flags (~250 tokens)
    - bull/bear arguers: preamble + flow + sector (~400 tokens)
    - synthesiser: preamble + flow (~200 tokens)
    - multi-perspective fundamental: preamble + sector + red flags
    - multi-perspective macro: preamble + flow + sector + seasonality
    - deep analyst: everything (~600 tokens)
    """
    parts = [INDIAN_MARKET_PREAMBLE]
    if include_flow:
        parts.append(FLOW_CONTEXT)
    if include_sector:
        parts.append(SECTOR_PLAYBOOK)
    if include_red_flags:
        parts.append(MICROSTRUCTURE_RED_FLAGS)
    if include_seasonality:
        parts.append(SEASONALITY_NOTES)
    return "\n\n".join(parts)
