import yaml

from src.content.generator import ContentGenerator
from src.content.templates import TemplateManager
from src.utils.config_loader import AppConfig


def test_generate_reddit_comment(tmp_path):
    templates_yaml = {
        "templates": [
            {
                "id": "reddit_1",
                "platform": "reddit",
                "min_words": 10,
                "max_words": 60,
                "text": "Hello r/{{subreddit}}\nThis is a simple routine and I will try to measure results.",
            }
        ]
    }
    p = tmp_path / "templates.yaml"
    p.write_text(yaml.dump(templates_yaml), encoding="utf-8")

    mgr = TemplateManager.from_yaml_file(str(p))
    cfg = AppConfig(telegram={"bot_token": "x", "user_chat_id": "1"})
    gen = ContentGenerator(cfg, templates=mgr, synonyms={"simple": ["basic"], "try": ["attempt"], "measure": ["track"]})

    out = gen.generate_reddit_comment("beermoney")
    assert out["platform"] == "reddit"
    assert isinstance(out["content"], str)
    assert 0.0 <= out["quality"]["score"] <= 1.0
