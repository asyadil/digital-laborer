"""Rule-based paraphrasing utilities (no external APIs)."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ParaphraseResult:
    original: str
    paraphrased: str
    replaced_count: int
    preserved_links: List[str]


_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"\b([A-Za-z][A-Za-z']{1,})\b")


class RuleBasedParaphraser:
    def __init__(
        self,
        synonyms: Dict[str, List[str]],
        technical_terms: Optional[Iterable[str]] = None,
        seed: int = 1337,
    ) -> None:
        self.synonyms = {k.lower(): list(v) for k, v in (synonyms or {}).items()}
        self.technical_terms = {t.lower() for t in (technical_terms or [])}
        self._rng = random.Random(seed)

    def paraphrase(self, text: str, intensity: float = 0.5) -> ParaphraseResult:
        if text is None:
            text = ""
        intensity = max(0.0, min(1.0, float(intensity)))

        links = _LINK_RE.findall(text)
        protected, link_map = self._protect_links(text)

        replaced = 0

        def repl(m: re.Match) -> str:
            nonlocal replaced
            word = m.group(1)
            lower = word.lower()

            # Preserve technical terms and Named Entities (simple heuristic: Capitalized word)
            if lower in self.technical_terms:
                return word
            if word[0].isupper() and lower not in self.synonyms:
                return word

            candidates = self.synonyms.get(lower)
            if not candidates:
                return word
            if self._rng.random() > intensity:
                return word

            new_word = self._rng.choice(candidates)
            if word[0].isupper():
                new_word = new_word[:1].upper() + new_word[1:]
            replaced += 1
            return new_word

        paraphrased = _WORD_RE.sub(repl, protected)
        paraphrased = self._restore_links(paraphrased, link_map)

        return ParaphraseResult(
            original=text,
            paraphrased=paraphrased,
            replaced_count=replaced,
            preserved_links=links,
        )

    def _protect_links(self, text: str) -> Tuple[str, Dict[str, str]]:
        link_map: Dict[str, str] = {}

        def repl(m: re.Match) -> str:
            token = f"__LINK_{len(link_map)}__"
            link_map[token] = m.group(0)
            return token

        protected = _LINK_RE.sub(repl, text)
        return protected, link_map

    def _restore_links(self, text: str, link_map: Dict[str, str]) -> str:
        for token, link in link_map.items():
            text = text.replace(token, link)
        return text
