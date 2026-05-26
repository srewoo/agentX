"""Pydantic schemas for every LLM response in agentX.

Centralising these here makes parse failures impossible by construction
(Pydantic raises ValidationError on bad shape) and lets callers chain on
typed objects instead of free dicts. Every LLM-facing service should
import its schema from this module — never re-define a duplicate.

Validation philosophy:
- ``Field(max_length=…)`` everywhere a string lands in the UI, so a
  hallucinated 2KB response can't blow up downstream layouts.
- Enum-like ``pattern`` constraints on categorical fields so callers can
  switch on the value safely.
- ``model_validator``/``field_validator`` for cross-field invariants.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────
# Layer-2 LLM judge over deterministic signals
# ─────────────────────────────────────────────────────────────────────────

class JudgeVerdict(BaseModel):
    """One verdict for one Layer-1 candidate signal."""
    id: str = Field(min_length=1, max_length=64)
    verdict: str = Field(pattern="^(keep|drop|downgrade)$")
    reason: str = Field(max_length=240)

    @field_validator("reason")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class JudgeResponse(BaseModel):
    """Envelope returned by the judge LLM. Strict shape — no extras."""
    verdicts: list[JudgeVerdict] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# LLM analyst — per-signal narrative
# ─────────────────────────────────────────────────────────────────────────

class SignalEnrichment(BaseModel):
    """2-3 sentence analyst summary + key factor + risk factor."""
    summary: str = Field(default="", max_length=500)
    key_factor: str = Field(default="", max_length=200)
    risk: str = Field(default="", max_length=200)

    def as_display_string(self) -> str:
        """Render the three fields as one display sentence for the UI."""
        parts: list[str] = []
        for piece in (
            self.summary.strip(),
            f"Key factor: {self.key_factor.strip()}" if self.key_factor.strip() else "",
            f"Risk: {self.risk.strip()}" if self.risk.strip() else "",
        ):
            if piece and piece != "N/A":
                parts.append(piece)
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Bull / Bear / Judge debate loop (#7)
# ─────────────────────────────────────────────────────────────────────────

class DebateArgument(BaseModel):
    """One side's case in the bull-vs-bear debate over a signal."""
    side: str = Field(pattern="^(bull|bear)$")
    thesis: str = Field(max_length=400)
    key_evidence: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)


class DebateVerdict(BaseModel):
    """Third-party judge's call after reading both sides."""
    winner: str = Field(pattern="^(bull|bear|inconclusive)$")
    synthesis: str = Field(max_length=400)
    calibrated_confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=400)


class DebateResult(BaseModel):
    """Aggregate output of one signal's debate."""
    signal_id: str = Field(min_length=1, max_length=64)
    bull: Optional[DebateArgument] = None
    bear: Optional[DebateArgument] = None
    verdict: DebateVerdict


# ─────────────────────────────────────────────────────────────────────────
# Recommendation-level LLM judge (separate from signal judge)
# ─────────────────────────────────────────────────────────────────────────

class RecommendationJudgeOutput(BaseModel):
    """Used by recommendation_llm_judge — keep/downgrade/avoid + reason."""
    stance: str = Field(pattern="^(BUY|SELL|HOLD|CAUTIOUS_BUY|CAUTIOUS_SELL|AVOID)$")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=400)


# ─────────────────────────────────────────────────────────────────────────
# Multi-perspective LLM analyst (#14)
# ─────────────────────────────────────────────────────────────────────────

class PerspectiveAnalysis(BaseModel):
    """One specialist's read on a signal.

    The four perspectives (Technical / Fundamental / Sentiment / Macro)
    each emit this same shape. Score is signed: positive = supports the
    rule direction, negative = contradicts it. Confidence reflects the
    *strength of evidence*, independent of direction.
    """
    perspective: str = Field(pattern="^(technical|fundamental|sentiment|macro)$")
    # -1 = strongly contradicts rule direction; +1 = strongly supports
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=300)
    key_drivers: list[str] = Field(default_factory=list, max_length=4)
    red_flags: list[str] = Field(default_factory=list, max_length=4)


class MultiPerspectiveAnalysis(BaseModel):
    """Synthesised cross-perspective view of one signal."""
    signal_id: str = Field(min_length=1, max_length=64)
    perspectives: list[PerspectiveAnalysis]
    # Weighted aggregate ∈ [-1, +1]
    aggregate_score: float = Field(ge=-1.0, le=1.0)
    consensus: str = Field(pattern="^(strong_confirm|confirm|mixed|contradict|strong_contradict)$")
    # Two-sentence judge-style synthesis suitable for the card tooltip.
    synthesis: str = Field(max_length=400)

    def as_display_summary(self) -> str:
        """Compact one-paragraph rendering for the signal-card expansion."""
        parts = [self.synthesis]
        for p in self.perspectives:
            sign = "+" if p.score >= 0 else ""
            parts.append(f"[{p.perspective.upper()} {sign}{p.score:.2f}] {p.summary}")
        return "\n".join(parts)
