"""Offline deterministic content generator (no paid LLM APIs)."""
from __future__ import annotations

import hashlib
from collections import deque
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    def __init__(
        self,
        config: Any,
        templates: TemplateManager,
        synonyms: Dict[str, List[str]],
        referral_links: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.templates = templates
        self.paraphraser = RuleBasedParaphraser(synonyms=synonyms, seed=1337)
        self.scorer = QualityScorer(
            min_length=int(getattr(config.content, "min_length", 200)),
            max_length=int(getattr(config.content, "max_length", 800)),
            min_sections=3,
        )
        self.referral_links = referral_links or []
        self.default_locale = str(getattr(getattr(config, "content", None), "default_locale", "en") or "en").lower()
        self._recent_fingerprints: deque[str] = deque(maxlen=200)
        self._fingerprint_set: set[str] = set()

    def generate_reddit_comment(
        self, subreddit: str, context: Optional[Dict[str, Any]] = None, locale: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._generate(
            platform="reddit",
            context={
                "subreddit": subreddit,
                "tone": "practical",
                "locale": locale,
                "referral_link": (context or {}).get("referral_link")
                if context
                else self._default_referral_link("reddit", locale=locale),
                **(context or {}),
            },
            min_words=200,
            max_words=500,
        )

    def generate_youtube_comment(
        self, video_title: str, video_description: str, locale: Optional[str] = None
    ) -> Dict[str, Any]:
        topic = self._extract_topic(video_title + " " + (video_description or ""))
        return self._generate(
            platform="youtube",
            context={
                "video_title": video_title,
                "topic": topic,
                "video_description": video_description,
                "cta": "If you want the walkthrough + link, check the first reply — happy to share what worked for me.",
                "locale": locale,
                "referral_link": (context or {}).get("referral_link")
                if context
                else self._default_referral_link("youtube", locale=locale),
            },
            min_words=150,
            max_words=400,
        )

    def generate_quora_answer(
        self, question: str, existing_answers_summary: str, locale: Optional[str] = None
    ) -> Dict[str, Any]:
        topic = self._extract_topic(question)
        return self._generate(
            platform="quora",
            context={
                "question": question,
                "topic": topic,
                "existing_answers_summary": existing_answers_summary,
                "structure": [
                    "**Intro**: brief hook answering the question plainly.",
                    "**Body**: 3-5 sections with steps, examples, pitfalls.",
                    "**Conclusion**: recap + next step + referral placement.",
                ],
                "locale": locale,
                "referral_links": self._referral_links_for("quora", limit=2, locale=locale),
                "cta": "If you want a starter bundle with tools and the referral link I used, ask and I’ll share.",
            },
            min_words=800,
            max_words=2000,
        )

    def generate_long_form_article(
        self,
        topic: str,
        platform: str = "generic",
        audience: str = "beginner",
        tone: str = "practical",
        min_words: int = 1200,
        max_words: int = 2200,
    ) -> Dict[str, Any]:
        """Generate long-form content with structured sections and CTA."""
        outline = [
            "## Hook & Context",
            "## What to know first",
            "## Step-by-step",
            "## Common pitfalls",
            "## Metrics & proof",
            "## Next steps / CTA",
        ]
        ctx = {
            "topic": topic,
            "audience": audience,
            "tone": tone,
            "outline": outline,
            "cta": "If you want the full toolkit + templates I used, reply and I’ll share privately.",
            "referral_link": self._default_referral_link(platform) or self._default_referral_link("generic"),
        }
        sections = self._build_long_form_sections(ctx)
        raw = "\n\n".join(sections)
        # Slight paraphrase for variation
        try:
            raw = self.paraphraser.paraphrase(raw, intensity=0.25).paraphrased
        except Exception:
            pass
        # Enforce length & sanitize
        normalized = self._normalize_whitespace(raw)
        trimmed = self._enforce_word_range(normalized, min_words=min_words, max_words=max_words)
        qa = self.scorer.assess(trimmed)
        return {
            "platform": platform,
            "content": self._sanitize_output(trimmed),
            "quality": {"score": qa.score, "breakdown": qa.breakdown, "suggestions": qa.suggestions},
            "template_id": "long_form_structured",
            "errors": [],
            "warnings": [],
        }

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
        ctx = dict(context)
        ctx["platform"] = platform
        locale = self._resolve_locale(context)
        ctx["locale"] = locale

        try:
            seed = self._stable_seed(platform, context)
            tpl = self.templates.pick_template(platform=platform, seed=seed, locale=locale)
            raw = self.templates.render(tpl.text, context=ctx)
        except (TemplateError, Exception) as exc:
            tpl = None
            raw = ""
            errors.append(f"template_error: {exc}")

        if not raw.strip():
            raw = self._fallback_text(platform, context)
            warnings.append("Used fallback template")

        # Ensure there is at least one helpful link placeholder if config provides it.
        default_link = self._default_referral_link(platform, locale=locale) if platform else None
        ctx["_default_referral_link"] = default_link
        if not default_link and not context.get("referral_link") and not context.get("referral_links"):
            warnings.append(f"missing_referral_link:{platform}:{locale or self.default_locale}")
            warnings.append("alert_missing_referral_link")
        raw = self._inject_links(raw, ctx, default_link=default_link)

        # Paraphrase lightly for variation
        try:
            paraphrased = self.paraphraser.paraphrase(raw, intensity=0.35).paraphrased
        except Exception as exc:
            paraphrased = raw
            warnings.append(f"paraphrase_failed: {exc}")

        structured = self._apply_platform_structure(platform, paraphrased, ctx)
        normalized = self._normalize_whitespace(structured)
        trimmed = self._enforce_word_range(normalized, min_words=min_words, max_words=max_words)

        qa = self.scorer.assess(trimmed)
        spam_hits = self._spam_indicators(trimmed)
        if spam_hits:
            warnings.append(f"spam_indicators: {', '.join(spam_hits)}")

        novelty_hits = self._novelty_flags(trimmed)
        if novelty_hits:
            warnings.extend(novelty_hits)

        sanitized = self._sanitize_output(trimmed)

        fp = self._fingerprint_content(sanitized)
        if fp in self._fingerprint_set:
            warnings.append("duplicate_content_recent")
        else:
            if len(self._recent_fingerprints) >= self._recent_fingerprints.maxlen:
                oldest = self._recent_fingerprints.popleft()
                self._fingerprint_set.discard(oldest)
            self._recent_fingerprints.append(fp)
            self._fingerprint_set.add(fp)

        return {
            "platform": platform,
            "content": sanitized,
            "quality": {"score": qa.score, "breakdown": qa.breakdown, "suggestions": qa.suggestions},
            "template_id": tpl.template_id if tpl else None,
            "errors": errors,
            "warnings": warnings,
        }

    def _fallback_text(self, platform: str, context: Dict[str, Any]) -> str:
        if platform == "reddit":
            sub = context.get("subreddit") or ""
            return (
                f"In r/{sub}, the posts that last are practical and human. I keep it simple: one actionable step, one personal detail, and one optional resource. "
                f"Track what you do for 10-14 days, note the response, and adjust tone before adding any link."
            )
        if platform == "youtube":
            title = context.get("video_title") or "this video"
            return (
                f"I liked {title} because it’s honest about the effort. What worked for me was one small routine for 14 days and a single polite CTA at the end. "
                f"Happy to share the exact line + link if useful."
            )
        if platform == "quora":
            q = context.get("question") or "the question"
            return (
                f"Here's a structured way to answer {q}: start with constraints (time, risk, skills), give 3-5 steps with an example metric, close with a recap and an offer to share resources."
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
            count = 0
            out = []
            for token in re.split(r"(\s+)", text):
                if not token:
                    continue
                if re.match(r"\s+", token):
                    if count >= max_words:
                        continue
                    out.append(token)
                    continue
                if count >= max_words:
                    break
                out.append(token)
                count += 1
            return "".join(out).strip()
        return text

    def _spam_indicators(self, text: str) -> List[str]:
        lower = text.lower()
        hits: List[str] = []
        for phrase in ["guaranteed", "100%", "act now", "risk free", "click here", "limited time"]:
            if phrase in lower:
                hits.append(phrase)
        if lower.count("http") > 2:
            hits.append("too_many_links")
        return hits

    def _novelty_flags(self, text: str) -> List[str]:
        """Lightweight novelty/dedup heuristics."""
        flags: List[str] = []
        lower = text.lower()
        if lower.count("http") > 3:
            flags.append("too_many_links_novelty")
        if len(set(lower.split())) < max(10, len(lower.split()) * 0.3):
            flags.append("low_word_variety")
        return flags

    def _fingerprint_content(self, text: str) -> str:
        """Stable fingerprint to detect recent duplicates."""
        normalized = self._normalize_whitespace(text).lower()
        normalized = re.sub(r"\s+", " ", normalized)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _inject_links(self, text: str, context: Dict[str, Any], default_link: Optional[str] = None) -> str:
        links = context.get("referral_links") or []
        if not isinstance(links, list):
            links = []

        if context.get("referral_link"):
            links = links + [context["referral_link"]]

        # Remove duplicates and empty
        seen = set()
        unique_links = []
        for link in links:
            if not link:
                continue
            norm = link.strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            unique_links.append(norm)

        if unique_links:
            sanitized_links = [self._sanitize_link(link) for link in unique_links]
            links_text = "\n".join(f"- {link}" for link in sanitized_links)
            return text + "\n\nHelpful resources:\n" + links_text

        # Fallback to default platform link if available
        platform = context.get("platform")
        locale = context.get("locale")
        default_link = default_link or (self._default_referral_link(platform, locale=locale) if platform else None)
        if default_link:
            return text + f"\n\nHelpful resource: {self._sanitize_link(default_link)}"
        return text

    def _apply_platform_structure(self, platform: str, text: str, context: Dict[str, Any]) -> str:
        """Apply platform-specific formatting before scoring/length enforcement."""
        if platform != "quora":
            return text

        question = context.get("question") or ""
        structure = context.get("structure") or []
        referral_links = context.get("referral_links") or []

        # Build intro, body, conclusion slots
        intro = structure[0] if structure else "**Intro**"
        body = structure[1] if len(structure) > 1 else "**Body**"
        conclusion = structure[2] if len(structure) > 2 else "**Conclusion**"

        intro_block = f"{intro}\n- Question: {question}\n- Constraint: time/skill/risk briefly set\n"
        body_block = f"{body}\n- Step 1: context-specific action\n- Step 2: example with small metric\n- Step 3: common pitfall + fix\n"

        # Insert referral links naturally in body if provided
        if referral_links:
            safe_links = [self._sanitize_link(link) for link in referral_links if isinstance(link, str) and link.startswith("http")]
            if safe_links:
                mid_link = safe_links[0]
                body_block += f"- Resource: {mid_link}\n"
                if len(safe_links) > 1:
                    body_block += f"- Bonus: {safe_links[1]}\n"

        conclusion_block = f"{conclusion}\n- Recap main actionable\n- Next step for reader\n- Offer to share more resources on request\n{context.get('cta','')}\n"

        return "\n".join([intro_block.strip(), body_block.strip(), conclusion_block.strip()])

    def _build_long_form_sections(self, context: Dict[str, Any]) -> List[str]:
        topic = context.get("topic", "")
        audience = context.get("audience", "reader")
        tone = context.get("tone", "practical")
        outline: List[str] = context.get("outline") or []
        referral = context.get("referral_link")

        def para(seed: int, core: str) -> str:
            rng = random.Random(seed)
            additions = [
                "Keep a simple log and measure progress weekly.",
                "Use one metric to decide if the step works.",
                "Avoid over-optimizing before you get signal.",
                "Share a small win to build trust before any link.",
            ]
            return core + " " + rng.choice(additions)

        sections: List[str] = []
        for idx, heading in enumerate(outline):
            base = f"{heading}\n"
            if "Hook" in heading:
                base += para(idx, f"{topic} matters for {audience}. Set expectations and choose a scope you can ship in 7-10 days.")
            elif "What to know" in heading:
                base += para(idx, f"Clarify constraints (time, budget, risk). Tone: {tone}. State assumptions plainly.")
            elif "Step-by-step" in heading:
                base += (
                    "- Step 1: Define the target outcome and one metric\n"
                    "- Step 2: Run a small test with a single channel\n"
                    "- Step 3: Inspect results, adjust copy, and iterate\n"
                )
            elif "pitfalls" in heading:
                base += (
                    "- Chasing too many channels at once\n"
                    "- Adding links before trust is built\n"
                    "- Ignoring signals (bounce, replies, downvotes)\n"
                )
            elif "Metrics" in heading:
                base += "Track 3 metrics: reach, engagement, conversion. Include a 7-day and 30-day snapshot."
                if referral:
                    base += f"\nMention resource lightly (e.g., {self._sanitize_link(referral)})."
            elif "Next steps" in heading:
                base += para(idx, context.get("cta", "Invite readers to ask for the playbook or template."))
            sections.append(base.strip())
        return sections

    def _sanitize_link(self, link: str) -> str:
        return re.sub(r"[\s<>\"']", "", link).strip()

    def _sanitize_output(self, text: str) -> str:
        """Basic sanitation to avoid injection/formatting issues (Markdown-safe)."""
        # Remove control characters
        text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
        # Escape basic Markdown special chars
        for ch in ["`", "*", "_", "[", "]", "(", ")", "~", "#", "+", "-", "|"]:
            text = text.replace(ch, f"\\{ch}")
        return text.strip()

    def _default_referral_link(self, platform: Optional[str], locale: Optional[str] = None) -> Optional[str]:
        """Pick first active referral link for the platform (and locale if provided)."""
        if not platform:
            return None
        chosen, _ = self._pick_referral(platform, locale=locale, limit=1)
        return chosen[0] if chosen else None

    def _referral_links_for(self, platform: Optional[str], limit: int = 2, locale: Optional[str] = None) -> List[str]:
        chosen, _ = self._pick_referral(platform, locale=locale, limit=limit)
        return chosen

    def _pick_referral(
        self, platform: Optional[str], locale: Optional[str], limit: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        if not platform:
            return [], []
        selected_links: List[str] = []
        selected_meta: List[Dict[str, Any]] = []
        locale_norm = (locale or self.default_locale or "en").lower()
        for item in self.referral_links:
            if not isinstance(item, dict):
                continue
            if not item.get("active", True):
                continue
            name = str(item.get("platform_name", "")).lower()
            if platform.lower() not in name and name not in ("generic", "all"):
                continue
            item_locale = str(item.get("locale") or item.get("lang") or "").lower() or "en"
            if item_locale not in (locale_norm, "all", "any", "generic"):
                continue
            url = item.get("url")
            if isinstance(url, str) and url.startswith("http"):
                selected_links.append(url)
                selected_meta.append(item)
            if len(selected_links) >= limit:
                break
        return selected_links, selected_meta

    def _resolve_locale(self, context: Dict[str, Any]) -> str:
        ctx_locale = context.get("locale")
        if isinstance(ctx_locale, str) and ctx_locale.strip():
            return ctx_locale.strip().lower()
        return self.default_locale
