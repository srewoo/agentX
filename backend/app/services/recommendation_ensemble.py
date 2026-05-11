from __future__ import annotations

"""Model ensemble for recommendation conviction.

The ensemble combines deterministic technical score, factor agreement,
fundamental valuation quality, R:R, data quality, portfolio constraints, and
an optional LLM judge adjustment into one auditable decision.
"""

from typing import Any

from app.models.recommendation import Action, SignalContribution


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _component(name: str, score: float, weight: float, note: str) -> dict[str, Any]:
    return {
        "name": name,
        "score": int(round(_clip(score, 0.0, 100.0))),
        "weight": round(weight, 3),
        "note": note,
    }


def _learned_edge_component(contributions: list[SignalContribution], direction: int) -> tuple[float, str]:
    try:
        from app.services.recommendation_tracker import factor_edge_multiplier
    except Exception:
        return 50.0, "No learned factor-edge cache available."

    aligned: list[float] = []
    for c in contributions:
        if abs(c.score) < 0.2 or c.score * direction <= 0:
            continue
        aligned.append(float(factor_edge_multiplier(c.name)))
    if not aligned:
        return 50.0, "No strong aligned factors with learned edge."
    avg_mult = sum(aligned) / len(aligned)
    score = _clip(50.0 + (avg_mult - 1.0) * 100.0, 0.0, 100.0)
    return score, f"Average learned factor multiplier {avg_mult:.2f}."


def build_recommendation_ensemble(
    *,
    action: Action,
    weighted_score: float,
    calibrated_conviction: int,
    factor_agreement: float,
    risk_reward: float,
    regime: str,
    contributions: list[SignalContribution],
    fundamental_valuation: dict[str, Any] | None,
    portfolio_context: dict[str, Any] | None,
    data_quality: str,
    llm_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direction = 1 if weighted_score >= 0 else -1
    technical_score = abs(weighted_score) * 100.0
    agreement_score = factor_agreement * 100.0
    rr_score = _clip((risk_reward / 2.0) * 100.0, 0.0, 100.0)
    fv = fundamental_valuation or {}
    fundamental_score = float(fv.get("score") or 50)
    learned_score, learned_note = _learned_edge_component(contributions, direction)
    data_score = 85.0 if data_quality == "eod_verified" else 65.0 if data_quality == "delayed_intraday" else 50.0

    components = [
        _component("technical_score", technical_score, 0.28, f"Weighted model score {weighted_score:+.3f}."),
        _component("calibrated_signal", calibrated_conviction, 0.18, "Backtest/regime-calibrated conviction."),
        _component("factor_agreement", agreement_score, 0.14, f"{factor_agreement:.0%} directional factor agreement."),
        _component("fundamental_valuation", fundamental_score, 0.16, f"Fundamental grade {fv.get('grade', 'N/A')}."),
        _component("learned_edge", learned_score, 0.10, learned_note),
        _component("risk_reward", rr_score, 0.08, f"Risk/reward {risk_reward:.2f}."),
        _component("data_quality", data_score, 0.06, data_quality.replace("_", " ")),
    ]
    base = sum(c["score"] * c["weight"] for c in components) / sum(c["weight"] for c in components)

    notes: list[str] = []
    blockers: list[str] = []
    if regime == "risk_off":
        base *= 0.94
        notes.append("Risk-off regime discount applied.")
    if fv.get("red_flags"):
        base -= min(10, 3 * len(fv["red_flags"]))
        notes.append("Fundamental red flags reduced conviction.")
    if portfolio_context:
        decision = portfolio_context.get("decision")
        if decision == "block_add":
            blockers.append("Portfolio concentration blocks adding this position.")
        elif decision in {"trim", "reduce"}:
            base -= 8
            notes.append("Portfolio context recommends reducing exposure.")

    llm_adjustment = 0
    if llm_judge:
        try:
            llm_adjustment = int(llm_judge.get("confidence_adjustment") or 0)
        except Exception:
            llm_adjustment = 0
        llm_adjustment = int(_clip(llm_adjustment, -15, 15))
        verdict = str(llm_judge.get("verdict") or "").upper()
        if verdict == "BLOCK":
            blockers.append("LLM judge found evidence gap/risk severe enough to block.")
        elif verdict == "DOWNGRADE":
            llm_adjustment = min(llm_adjustment, -5)
        elif verdict == "UPGRADE":
            llm_adjustment = max(llm_adjustment, 5)
        notes.append(f"LLM judge {verdict or 'N/A'} adjustment {llm_adjustment:+d}.")

    final = int(round(_clip(base + llm_adjustment, 0.0, 100.0)))
    decision_action: Action = action
    if blockers and decision_action == "BUY":
        decision_action = "HOLD"
    if decision_action in {"BUY", "SELL"} and final < 48:
        decision_action = "HOLD"
        notes.append("Directional call demoted because ensemble conviction is below 48.")
    if action == "AVOID":
        decision_action = "AVOID"
        final = 0

    return {
        "version": "ensemble_v1",
        "base_conviction": int(round(_clip(base, 0.0, 100.0))),
        "final_conviction": final,
        "suggested_action": decision_action,
        "llm_adjustment": llm_adjustment,
        "components": components,
        "blockers": blockers,
        "notes": notes,
    }
