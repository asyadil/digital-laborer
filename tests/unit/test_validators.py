import pytest

from src.utils.validators import ValidationError, sanitize_markdown, validate_choices, validate_non_empty_str


def test_validate_non_empty_str():
    assert validate_non_empty_str("hello", "field") == "hello"
    with pytest.raises(ValidationError):
        validate_non_empty_str("   ", "field")


def test_validate_choices():
    assert validate_choices("a", ["a", "b"], "field") == "a"
    with pytest.raises(ValidationError):
        validate_choices("c", ["a", "b"], "field")


def test_sanitize_markdown():
    text = "Hello [world](link)"
    escaped = sanitize_markdown(text)
    assert "\\[" in escaped
    assert "\\(" in escaped
