# SEBI Compliance — Legal-Product Note

> **Disclaimer on the disclaimer:** This is a product note, not legal advice. Every number flagged TODO must be confirmed with a SEBI-registered Research Analyst or compliance counsel before launch. Regulations under the SEBI (Research Analysts) Regulations, 2014, have been amended several times — last material update we are aware of is **December 2024**. Verify against the current SEBI circular set before any external doc is drafted.

## TL;DR

- **Publishing BUY/SELL/TARGET on listed Indian securities to the public requires SEBI Research Analyst (RA) registration.** Full stop. "Educational content" disclaimers do **not** shield you. SEBI has acted against multiple unregistered influencers in 2023–2025.
- Two viable paths: **(A) self-register as RA** or **(B) partner with a registered RA** who reviews and signs off on every recommendation we publish.
- Until one of these is done, **frame the product as "analytics + signals"**: factor breakdowns, OI heatmaps, FII/DII flow, portfolio analytics. **No "Buy RELIANCE at 2,800, target 2,950, SL 2,750" published to non-paying users without RA cover.**

## Path A — Self-register as Research Analyst

### Requirements (verify each before relying on them)
- **Net worth:** ₹1,00,000 minimum for individual RA, ₹25,00,000 for body corporate. **TODO — verify; this floor was reportedly raised in 2023 and may have been raised again.**
- **Qualification:** Graduate + NISM Series XV-A (Research Analyst) certification. Some categories also require NISM Series XV-B for fixed-income.
- **Experience:** 5 years of relevant experience for individuals **or** a post-graduate degree in finance/economics/accounting/business management. **TODO — verify the degree-substitutes-for-experience clause is still in force.**
- **Registration fee:** ₹5,000 application + ₹10,000 registration (individual). Body corporate is higher. **TODO — verify.**
- **Compliance officer:** Required for body-corporate RA.
- **Compliance audit:** Annual compliance audit by a CA/CS, submitted to SEBI. Half-yearly reports.
- **Practical total cost (year 1):** ₹75K–₹1.5L depending on body corporate vs individual, audit fees, and compliance tooling. **TODO — get quotes from 2–3 RA compliance consultants (e.g., 1 Finance Compliance, IndiaFilings, custom CA).**
- **Timeline:** ~3–6 months end-to-end (NISM exam + filing + SEBI processing).

### Ongoing obligations (the operational tax)
1. **Disclosure on every recommendation:** RA's name, registration number, conflicts (does the RA hold the stock?), date/time of publication, source data.
2. **Audit trail:** Retain every recommendation, the data behind it, and the prevailing market price for **5 years**. We have most of this already (`signal_tracker.py` — though only 22% test-covered, fix before launch).
3. **No insider information.** Internal Chinese walls if we ever build a transactions side.
4. **Advertising restrictions.** No "guaranteed returns", "100% accurate", performance claims must be disclosed with full context.
5. **Suitability framework** is not technically required for RAs (that's Investment Advisers / IA), but moving toward a "tell the user this is generic, not personalised" framing is best practice.
6. **Cooling-off:** If the RA holds a stock, there are restrictions on issuing recommendations on it within 30 days (verify exact window).

## Path B — Partner with registered RA

- Identify 2–3 RA firms willing to review and co-sign daily/weekly recommendations.
- Commercial: typically a **revenue share (10–25%)** or **fixed retainer (₹50K–₹2L/mo)** — verify with quotes.
- Operationally: every published recommendation routes through the RA's compliance review (can be lightweight — daily batch + audit log).
- **Faster to launch (4–6 weeks vs 3–6 months).**
- **Risk:** RA partner becomes a single point of failure. Mitigate with two parallel partners or treat as a bridge to self-registration.

## Recommended path

**Start with Path B** (partner) for launch in Q+1, **run Path A in parallel** (founder or technical co-founder takes NISM XV-A), self-register by Q+2. Cut the partner once self-registered. This minimises time-to-market while building the long-term moat.

## Product framing — what we ship today, pre-RA cover

| Feature | Today (no RA cover) | After RA cover |
|---|---|---|
| 8-factor score | Yes — labelled "analytics signal", not "advice" | Yes — labelled "research view" |
| Entry/SL/Targets | **Show only on the user's own backtest / paper trades.** Do not publish to the public feed. | Publish, with RA disclosure block |
| Telegram daily digest | OI heatmap, FII/DII flow, events — **no specific BUY/SELL targets** | Add specific calls with RA sign-off |
| "Why this BUY?" panel | Rename to "Factor view"; the verb is *analyse*, not *recommend* | Restore "Recommendation" framing |
| F&O OI heatmap | Yes — pure data presentation, low risk | Yes |
| Portfolio analytics | Yes — user's own holdings, no recommendation surface | Yes |
| Backtester | Yes — historical analytics, low risk | Yes |
| Paper trading | Yes — low risk | Yes |

## Disclaimer kit (every public surface)

```
agentX provides analytics, charting, and signal-engineering tools.
agentX is [registered with SEBI as Research Analyst — Reg. No. INHxxxxxxxxx /
operated under research review by <RA partner>, SEBI Reg. No. INHxxxxxxxxx].
Investments in securities markets are subject to market risks; read all
related documents carefully before investing. Past performance is not
indicative of future returns. Information shown is for educational and
research purposes; users must consult a SEBI-registered investment adviser
before acting. agentX does not guarantee returns.
```

(Use the bracketed clause matching whichever path is live.)

## Audit trail — what we already have, what's missing

**Have:**
- Every recommendation goes through `signal_tracker.py` (verify retention policy is ≥5y).
- Factor weights and contributions are captured in `Recommendation.signals` (per `recommendation.py`).
- LLM usage logged with cost (`llm_client.py`).

**Missing / TODO:**
- Append-only storage with cryptographic hash chain for non-repudiation.
- Auto-generated half-yearly compliance report (scriptable).
- Conflict-of-interest register (does any team member hold a stock we recommended?).
- User suitability disclaimer at signup (one-line + checkbox).

## Open legal items (block external launch until closed)
1. RA partner identified and contracted, OR self-registration filed and accepted.
2. Counsel review of disclaimer kit and Pro/Elite tier T&Cs.
3. DPDP Act 2023 compliance review — we hold portfolio + holdings PII.
4. GST registration confirmed for Razorpay subscriptions.
5. Verify all numbers in this doc flagged TODO.
