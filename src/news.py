from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsSignal:
    score: int
    risk_level: str
    headlines: list[str]
    explanation: str


POSITIVE_TERMS = {
    "growth",
    "stable",
    "cut",
    "surge",
    "rally",
    "profit",
    "beats",
    "inflow",
    "easing",
    "recovery",
}

NEGATIVE_TERMS = {
    "war",
    "conflict",
    "sanction",
    "inflation",
    "hike",
    "crash",
    "fall",
    "default",
    "volatile",
    "tension",
    "attack",
    "recession",
}

HIGH_IMPACT_TERMS = {
    "rbi",
    "budget",
    "election",
    "war",
    "attack",
    "sanction",
    "rate hike",
    "rate cut",
    "geopolitical",
}


class NewsAnalyzer:
    def analyze(self, headlines: list[str]) -> NewsSignal:
        if not headlines:
            return NewsSignal(0, "Low", [], "No news headlines supplied")

        score = 0
        high_impact_hits = 0
        for headline in headlines:
            text = headline.lower()
            score += sum(8 for term in POSITIVE_TERMS if term in text)
            score -= sum(10 for term in NEGATIVE_TERMS if term in text)
            high_impact_hits += sum(1 for term in HIGH_IMPACT_TERMS if term in text)

        score = max(-100, min(100, score))
        risk_level = "High" if high_impact_hits >= 2 or score <= -30 else "Medium" if high_impact_hits else "Low"
        explanation = f"Scored {len(headlines)} headlines with {high_impact_hits} high-impact term hits"
        return NewsSignal(score=score, risk_level=risk_level, headlines=headlines, explanation=explanation)
