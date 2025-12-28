"""Template management with variables and simple conditionals."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class Template:
    template_id: str
    platform: str
    name: str
    text: str
    min_words: int
    max_words: int


class TemplateError(RuntimeError):
    pass


_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\}\}")
_IF_RE = re.compile(r"\{%\s*if\s+([a-zA-Z_][a-zA-Z0-9_\.]*)\s*%\}")
_ENDIF_RE = re.compile(r"\{%\s*endif\s*%\}")
_INLINE_IF_RE = re.compile(
    r"\{%\s*if\s+([a-zA-Z_][a-zA-Z0-9_\.]*)\s*%\}(.*?)\{%\s*endif\s*%\}",
    re.DOTALL,
 )


def _get_by_path(data: Dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


class TemplateManager:
    """Loads templates from YAML and renders them deterministically."""

    def __init__(self, templates: Optional[List[Template]] = None) -> None:
        self._templates: List[Template] = templates or []

    @classmethod
    def from_yaml_file(cls, path: str) -> "TemplateManager":
        if not os.path.exists(path):
            raise FileNotFoundError(f"Templates file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict) or "templates" not in raw:
            raise TemplateError("Invalid templates YAML: missing 'templates' list")
        items = raw.get("templates")
        if not isinstance(items, list):
            raise TemplateError("Invalid templates YAML: 'templates' must be a list")

        templates: List[Template] = []
        for idx, t in enumerate(items):
            if not isinstance(t, dict):
                raise TemplateError(f"Template at index {idx} must be an object")
            template_id = str(t.get("id") or f"tpl_{idx}")
            platform = str(t.get("platform") or "generic")
            name = str(t.get("name") or template_id)
            text = str(t.get("text") or "")
            if not text.strip():
                raise TemplateError(f"Template {template_id} has empty text")
            min_words = int(t.get("min_words") or 0)
            max_words = int(t.get("max_words") or 10000)
            templates.append(
                Template(
                    template_id=template_id,
                    platform=platform,
                    name=name,
                    text=text,
                    min_words=min_words,
                    max_words=max_words,
                )
            )
        return cls(templates=templates)

    def list_templates(self, platform: Optional[str] = None) -> List[Template]:
        if platform is None:
            return list(self._templates)
        return [t for t in self._templates if t.platform == platform]

    def pick_template(self, platform: str, seed: int) -> Template:
        candidates = self.list_templates(platform=platform) or self.list_templates(platform="generic")
        if not candidates:
            raise TemplateError("No templates available")
        return candidates[seed % len(candidates)]

    def render(self, template_text: str, context: Dict[str, Any]) -> str:
        """Render variables and {% if key %}...{% endif %} blocks."""
        try:
            rendered = self._render_conditionals(template_text, context)
            rendered = _VAR_RE.sub(lambda m: self._render_var(m.group(1), context), rendered)
            return rendered
        except Exception as exc:
            raise TemplateError(f"Template rendering failed: {exc}") from exc

    def _render_var(self, var_path: str, context: Dict[str, Any]) -> str:
        value = _get_by_path(context, var_path)
        if value is None:
            return ""
        return str(value)

    def _render_conditionals(self, text: str, context: Dict[str, Any]) -> str:
        # First, resolve inline conditionals that open/close on the same line or span.
        # This keeps the template language simple but covers common usage.
        while True:
            match = _INLINE_IF_RE.search(text)
            if not match:
                break
            key = match.group(1)
            inner = match.group(2)
            val = _get_by_path(context, key)
            replacement = inner if bool(val) else ""
            text = text[: match.start()] + replacement + text[match.end() :]

        out_lines: List[str] = []
        lines = text.splitlines(True)
        include_stack: List[bool] = [True]

        for line in lines:
            if_match = _IF_RE.search(line)
            endif_match = _ENDIF_RE.search(line)

            if if_match:
                key = if_match.group(1)
                val = _get_by_path(context, key)
                include_stack.append(bool(val) and include_stack[-1])
                continue
            if endif_match:
                if len(include_stack) == 1:
                    raise TemplateError("endif without matching if")
                include_stack.pop()
                continue
            if include_stack[-1]:
                out_lines.append(line)

        if len(include_stack) != 1:
            raise TemplateError("Unclosed if block")
        return "".join(out_lines)
