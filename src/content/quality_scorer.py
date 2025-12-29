"""Deterministic content quality scoring (fast, offline)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class QualityAssessment:
    score: float
    breakdown: Dict[str, float]
    suggestions: List[str]
    debug: Optional[Dict[str, Any]] = None


_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORD_SPLIT_RE = re.compile(r"\b\w+\b", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_SPAM_PHRASES = [
    "guaranteed",
    "100%",
    "risk free",
    "click here",
    "act now",
    "limited time",
    "free money",
    "easy money",
    "earn fast",
    "too good to be true",
    "instant cash",
    "double your",
    "no effort",
    "miracle",
    "secret trick",
    "get rich",
    "overnight",
]


class QualityScorer:
    def __init__(
        self,
        min_length: int = 200,
        max_length: int = 800,
        max_links: int = 2,
    ) -> None:
        self.min_length = min_length
        self.max_length = max_length
        self.max_links = max_links

    def assess(self, content: str) -> QualityAssessment:
        if content is None:
            content = ""

        words = _WORD_SPLIT_RE.findall(content)
        word_count = len(words)
        links = _LINK_RE.findall(content)

        length_score = self._length_score(word_count)
        spam_score, spam_suggestions = self._spam_score(content, links)
        flow_score, flow_suggestions = self._flow_score(content)
        link_score, link_suggestions = self._link_placement_score(content, links)
        readability_score, read_suggestions = self._readability_score(content)

        breakdown = {
            "length": length_score,
            "spam": spam_score,
            "flow": flow_score,
            "link_placement": link_score,
            "readability": readability_score,
        }

        # Weights per spec: length 0.2, spam 0.3, flow 0.2, link placement 0.2, readability 0.1
        total = (
            0.2 * length_score
            + 0.3 * spam_score
            + 0.2 * flow_score
            + 0.2 * link_score
            + 0.1 * readability_score
        )

        suggestions = spam_suggestions + flow_suggestions + link_suggestions + read_suggestions
        total = max(0.0, min(1.0, float(total)))

        return QualityAssessment(score=total, breakdown=breakdown, suggestions=suggestions)

    def _length_score(self, word_count: int) -> float:
        if word_count <= 0:
            return 0.0
        if word_count < self.min_length:
            return max(0.0, word_count / max(1.0, float(self.min_length)))
        if word_count > self.max_length:
            overflow = word_count - self.max_length
            penalty = min(1.0, overflow / max(1.0, float(self.max_length)))
            return max(0.0, 1.0 - penalty)
        return 1.0

    def _spam_score(self, content: str, links: List[str]) -> tuple[float, List[str]]:
        suggestions: List[str] = []
        lowered = content.lower()

        phrase_hits = sum(1 for p in _SPAM_PHRASES if p in lowered)
        link_penalty = max(0, len(links) - self.max_links)
        repeated_word_penalty = self._repetition_penalty(lowered)
        caps_penalty = 0.1 if self._excessive_caps(content) else 0.0
        bang_penalty = 0.1 if content.count("!") > 3 else 0.0

        score = 1.0
        if phrase_hits:
            score -= min(0.6, phrase_hits * 0.15)
            suggestions.append("Reduce promotional/spam phrases")
        if link_penalty:
            score -= min(0.5, link_penalty * 0.2)
            suggestions.append("Reduce number of links")
        if repeated_word_penalty > 0:
            score -= min(0.4, repeated_word_penalty)
            suggestions.append("Avoid repeating the same words/phrases")
        if caps_penalty:
            score -= caps_penalty
            suggestions.append("Use fewer ALL-CAPS words")
        if bang_penalty:
            score -= bang_penalty
            suggestions.append("Reduce exclamation marks")
        if any(token in lowered for token in ["http://", "https://"]) and len(links) == 0:
            suggestions.append("Avoid obfuscated links; use clean URLs sparingly")
        return max(0.0, score), suggestions

    def _repetition_penalty(self, lowered: str) -> float:
        words = _WORD_SPLIT_RE.findall(lowered)
        if len(words) < 40:
            return 0.0
        counts: Dict[str, int] = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        top = max(counts.values() or [0])
        ratio = top / max(1, len(words))
        return max(0.0, (ratio - 0.08) * 5.0)  # penalty starts if >8% same token

    def _flow_score(self, content: str) -> tuple[float, List[str]]:
        suggestions: List[str] = []
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(content.strip()) if s.strip()]
        if not sentences:
            return 0.0, ["Add clearer sentence structure"]

        lengths = [len(_WORD_SPLIT_RE.findall(s)) for s in sentences]
        avg = sum(lengths) / max(1, len(lengths))
        variance = sum((l - avg) ** 2 for l in lengths) / max(1, len(lengths))
        std = math.sqrt(variance)

        score = 1.0
        if avg < 8:
            score -= 0.2
            suggestions.append("Use slightly longer sentences")
        if avg > 30:
            score -= 0.3
            suggestions.append("Break up long sentences")
        if std < 2 and len(sentences) > 3:
            score -= 0.1
            suggestions.append("Vary sentence length for more natural flow")

        return max(0.0, score), suggestions

    def _link_placement_score(self, content: str, links: List[str]) -> tuple[float, List[str]]:
        suggestions: List[str] = []
        if not links:
            return 0.7, ["Consider adding a helpful link if relevant"]

        score = 1.0
        tokens = _WORD_SPLIT_RE.findall(content)
        first_40 = " ".join(tokens[:40]).lower()
        if any(link.lower() in first_40 for link in links):
            score -= 0.2
            suggestions.append("Avoid placing links at the very start")

        # Penalize link dumping at end
        last_40 = " ".join(tokens[-40:]).lower()
        if sum(1 for link in links if link.lower() in last_40) == len(links) and len(links) >= 2:
            score -= 0.2
            suggestions.append("Distribute links naturally within content")

        # Reward mid-body placement when single link
        if len(links) == 1 and score == 1.0:
            score = 1.0

        return max(0.0, score), suggestions

    def _excessive_caps(self, content: str) -> bool:
        letters = [c for c in content if c.isalpha()]
        if not letters:
            return False
        caps = sum(1 for c in letters if c.isupper())
        return (caps / len(letters)) > 0.4

    def _readability_score(self, content: str) -> tuple[float, List[str]]:
        suggestions: List[str] = []
        words = _WORD_SPLIT_RE.findall(content)
        if not words:
            return 0.0, ["Add more content"]
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(content) if s.strip()]
        sentence_count = max(1, len(sentences))
        avg_words = len(words) / sentence_count

        # Simple readability proxy: prefer avg sentence length 12-22
        if avg_words <= 8:
            return 0.7, ["Add more detail per sentence"]
        if avg_words >= 28:
            return 0.6, ["Shorten sentences to improve readability"]

        # Smooth score peak near 18
        score = 1.0 - min(0.4, abs(avg_words - 18) / 30)
        return max(0.0, score), suggestions
