"""Content generation engine (offline, deterministic)."""

from .generator import ContentGenerator
from .paraphraser import RuleBasedParaphraser
from .quality_scorer import QualityScorer
from .templates import TemplateManager

__all__ = ["ContentGenerator", "RuleBasedParaphraser", "QualityScorer", "TemplateManager"]
