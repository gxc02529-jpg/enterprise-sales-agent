import json

import pytest

from sales_agent.prompts.registry import FilePromptRegistry


def test_prompt_registry_selects_locale_and_active_version(tmp_path) -> None:
    path = tmp_path / "prompts.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "prompts": [
                    {
                        "key": "demo",
                        "version": "1",
                        "locale": "zh-CN",
                        "status": "inactive",
                        "content": "旧版 {{name}}",
                        "variables": ["name"],
                    },
                    {
                        "key": "demo",
                        "version": "2",
                        "locale": "zh-CN",
                        "status": "active",
                        "content": "新版 {{name}}",
                        "variables": ["name"],
                    },
                    {
                        "key": "demo",
                        "version": "2",
                        "locale": "en-US",
                        "status": "active",
                        "content": "Hello {{name}}",
                        "variables": ["name"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry = FilePromptRegistry(path)
    rendered = registry.render("demo", "en-US", name="Sales")
    assert rendered.content == "Hello Sales"
    assert rendered.version == "2"
    fallback = registry.render("demo", "fr-FR", name="销售")
    assert fallback.content == "新版 销售"


def test_prompt_registry_rejects_multiple_active_versions(tmp_path) -> None:
    path = tmp_path / "prompts.json"
    path.write_text(
        json.dumps(
            {
                "prompts": [
                    {
                        "key": "demo",
                        "version": str(version),
                        "locale": "zh-CN",
                        "status": "active",
                        "content": "内容",
                    }
                    for version in (1, 2)
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="multiple active"):
        FilePromptRegistry(path)
