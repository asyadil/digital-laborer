"""Offline deterministic content generator (no paid LLM APIs)."""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.content.paraphraser import RuleBasedParaphraser
from src.content.quality_scorer import QualityScorer
from src.content.templates import TemplateManager, TemplateError


@dataclass(frozen=True)
class GenerationResult:
    platform: str
    content: str
    quality_score: float
    quality_breakdown: Dict[str, float]
    suggestions: List[str]
    template_id: Optional[str]
    errors: List[str]
    warnings: List[str]


_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class ContentGenerator:
    def __init__(self, config: Any, templates: TemplateManager, synonyms: Dict[str, List[str]]) -> None:
        self.config = config
        self.templates = templates
        self.paraphraser = RuleBasedParaphraser(synonyms=synonyms, seed=1337)
        self.scorer = QualityScorer(
            min_length=int(getattr(config.content, "min_length", 200)),
            max_length=int(getattr(config.content, "max_length", 800)),
        )

    def generate_reddit_comment(self, subreddit: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._generate(
            platform="reddit",
            context={"subreddit": subreddit, **(context or {})},
            min_words=200,
            max_words=500,
        )

    def generate_youtube_comment(self, video_title: str, video_description: str) -> Dict[str, Any]:
        topic = self._extract_topic(video_title + " " + (video_description or ""))
        return self._generate(
            platform="youtube",
            context={"video_title": video_title, "topic": topic, "video_description": video_description},
            min_words=150,
            max_words=400,
        )

    def generate_quora_answer(self, question: str, existing_answers_summary: str) -> Dict[str, Any]:
        topic = self._extract_topic(question)
        return self._generate(
            platform="quora",
            context={
                "question": question,
                "topic": topic,
                "existing_answers_summary": existing_answers_summary,
            },
            min_words=800,
            max_words=2000,
        )

    def paraphrase_content(self, text: str, intensity: float = 0.5) -> Dict[str, Any]:
        res = self.paraphraser.paraphrase(text, intensity=intensity)
        return {
            "original": res.original,
            "paraphrased": res.paraphrased,
            "replaced_count": res.replaced_count,
            "preserved_links": res.preserved_links,
        }

    def assess_quality(self, content: str) -> Dict[str, Any]:
        qa = self.scorer.assess(content)
        return {"score": qa.score, "breakdown": qa.breakdown, "suggestions": qa.suggestions}

    def _generate(self, platform: str, context: Dict[str, Any], min_words: int, max_words: int) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []

        try:
            seed = self._stable_seed(platform, context)
            tpl = self.templates.pick_template(platform=platform, seed=seed)
            raw = self.templates.render(tpl.text, context=context)
        except (TemplateError, Exception) as exc:
            tpl = None
            raw = ""
            errors.append(f"template_error: {exc}")

        if not raw.strip():
            raw = self._fallback_text(platform, context)
            warnings.append("Used fallback template")

        # Ensure there is at least one helpful link placeholder if config provides it.
        raw = self._inject_links(raw, context)

        # Paraphrase lightly for variation
        try:
            paraphrased = self.paraphraser.paraphrase(raw, intensity=0.35).paraphrased
        except Exception as exc:
            paraphrased = raw
            warnings.append(f"paraphrase_failed: {exc}")

        normalized = self._normalize_whitespace(paraphrased)
        trimmed = self._enforce_word_range(normalized, min_words=min_words, max_words=max_words)

        qa = self.scorer.assess(trimmed)

        return {
            "platform": platform,
            "content": trimmed,
            "quality": {"score": qa.score, "breakdown": qa.breakdown, "suggestions": qa.suggestions},
            "template_id": tpl.template_id if tpl else None,
            "errors": errors,
            "warnings": warnings,
        }

    def _fallback_text(self, platform: str, context: Dict[str, Any]) -> str:
        if platform == "reddit":
            sub = context.get("subreddit") or ""
            return (
                f"I\'ve tried a bunch of approaches over the years, and what worked best for me was treating it like an experiment: "
                f"track what you do, keep the risk low, and focus on consistency. In r/{sub}, people usually respond well to practical steps: "
                f"start small, measure results, and iterate. If you\'re exploring low-effort ways to get started, I\'d recommend picking one method, "
                f"doing it for two weeks, and writing down what actually moved the needle."
            )
        if platform == "youtube":
            title = context.get("video_title") or "this video"
            return (
                f"Really enjoyed {title}. I went through a similar phase where I tried too many things at once and got nowhere. "
                f"What helped was choosing one simple routine, sticking to it daily, and keeping expectations realistic. If anyone\'s new to this, "
                f"start with something you can actually maintain for 15 minutes a day and build from there."
            )
        if platform == "quora":
            q = context.get("question") or "the question"
            return (
                f"Here\'s a structured way to think about {q}: start by defining your constraints (time, risk tolerance, skills), then pick a method "
                f"that matches them. In the beginning, the most important factor is consistency. Once you have a baseline routine, you can optimize. "
                f"I\'ll break this down into a practical plan below, including common mistakes and how to avoid them."
            )
        return ""

    def _stable_seed(self, platform: str, context: Dict[str, Any]) -> int:
        key = platform + ":" + repr(sorted(context.items()))
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return int(h[:8], 16)

    def _extract_topic(self, text: str) -> str:
        if not text:
            return ""
        lowered = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        tokens = [t for t in lowered.split() if len(t) >= 4]
        if not tokens:
            return ""
        # deterministic: pick top by frequency
        freq: Dict[str, int] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        return sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    def _normalize_whitespace(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _enforce_word_range(self, text: str, min_words: int, max_words: int) -> str:
        words = re.findall(r"\b\w+\b", text)
        if len(words) < min_words:
            # deterministically pad with neutral expansion
            pad_sentence = (
                " In my experience, the key is to keep it simple, avoid shortcuts, and focus on repeatable steps you can measure over time."
            )
            while len(re.findall(r"\b\w+\b", text)) < min_words:
                text += pad_sentence
            return text.strip()
        if len(words) > max_words:
            # truncate at word boundary
            kept = words[:max_words]
            # rebuild by walking original text and stopping after max_words
            count = 0
            out = []
            for token in re.split(r"(\s+)", text):
                if not token:
                    continue
                if re.fullmatch(r"\s+", token):
                    out.append(token)
                    continue
                # count words in token
                w = re.findall(r"\b\w+\b", token)
                if w:
                    if count + len(w) > max_words:
                        break
                    count += len(w)
                out.append(token)
            return "".join(out).strip()
        return text

    def _inject_links(self, text: str, context: Dict[str, Any]) -> str:
        # Keep deterministic and safe: do not add if already has links.
        if _LINK_RE.search(text):
            return text
        # If context provides referral_link, insert once.
        link = context.get("referral_link")
        if isinstance(link, str) and link.startswith("http"):
            return text + f"\n\nHelpful resource: {link}"
        return text
