import yaml

from src.content.templates import TemplateManager


def test_template_render_vars_and_if(tmp_path):
    data = {
        "templates": [
            {
                "id": "t1",
                "platform": "reddit",
                "text": "Hello {{name}}\n{% if show %}Shown{% endif %}\n",
            }
        ]
    }
    p = tmp_path / "templates.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")

    mgr = TemplateManager.from_yaml_file(str(p))
    tpl = mgr.pick_template("reddit", seed=1)
    out = mgr.render(tpl.text, {"name": "Alice", "show": True})
    assert "Hello Alice" in out
    assert "Shown" in out

    out2 = mgr.render(tpl.text, {"name": "Bob", "show": False})
    assert "Hello Bob" in out2
    assert "Shown" not in out2
