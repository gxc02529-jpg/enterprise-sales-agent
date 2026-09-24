from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field, model_validator

from sales_agent.config import Settings


class PromptTemplate(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=2, max_length=32)
    status: str = Field(pattern="^(active|inactive)$")
    content: str = Field(min_length=1)
    variables: list[str] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def validate_placeholders(self) -> PromptTemplate:
        for variable in self.variables:
            if f"{{{{{variable}}}}}" not in self.content:
                raise ValueError(f"prompt variable is not used: {variable}")
        return self


class PromptCatalog(BaseModel):
    schema_version: int = 1
    prompts: list[PromptTemplate]


@dataclass(frozen=True)
class RenderedPrompt:
    key: str
    version: str
    locale: str
    content: str


class PromptRegistry(ABC):
    @abstractmethod
    def render(self, key: str, locale: str, **variables: Any) -> RenderedPrompt: ...

    @abstractmethod
    def list_templates(self) -> list[PromptTemplate]: ...


class FilePromptRegistry(PromptRegistry):
    """Versioned, locale-aware prompt catalog reloaded when its config file changes.

    The catalog is deliberately outside Python code. Deployments can promote or roll
    back a prompt by changing which version has ``status=active`` and atomically
    replacing the catalog file through their normal configuration/GitOps pipeline.
    """

    def __init__(self, path: Path, *, default_locale: str = "zh-CN") -> None:
        self.path = path
        self.default_locale = default_locale
        self._lock = RLock()
        self._mtime_ns = -1
        self._templates: list[PromptTemplate] = []
        self._active: dict[tuple[str, str], PromptTemplate] = {}
        self._reload(force=True)

    def _reload(self, *, force: bool = False) -> None:
        stat = self.path.stat()
        if not force and stat.st_mtime_ns == self._mtime_ns:
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        catalog = PromptCatalog.model_validate(payload)
        active: dict[tuple[str, str], PromptTemplate] = {}
        seen_versions: set[tuple[str, str, str]] = set()
        for template in catalog.prompts:
            version_key = (template.key, template.locale, template.version)
            if version_key in seen_versions:
                raise ValueError(f"duplicate prompt version: {version_key}")
            seen_versions.add(version_key)
            if template.status == "active":
                active_key = (template.key, template.locale)
                if active_key in active:
                    raise ValueError(f"multiple active prompt versions: {active_key}")
                active[active_key] = template
        if not active:
            raise ValueError("prompt catalog must contain at least one active template")
        self._templates = catalog.prompts
        self._active = active
        self._mtime_ns = stat.st_mtime_ns

    def _ensure_fresh(self) -> None:
        with self._lock:
            self._reload()

    def render(self, key: str, locale: str, **variables: Any) -> RenderedPrompt:
        self._ensure_fresh()
        requested_locale = locale or self.default_locale
        template = self._active.get((key, requested_locale))
        if template is None and "-" in requested_locale:
            template = self._active.get((key, requested_locale.split("-", 1)[0]))
        if template is None:
            template = self._active.get((key, self.default_locale))
        if template is None:
            raise KeyError(f"no active prompt for key={key!r}, locale={requested_locale!r}")
        missing = set(template.variables) - variables.keys()
        unexpected = variables.keys() - set(template.variables)
        if missing or unexpected:
            raise ValueError(
                f"prompt variables mismatch; missing={sorted(missing)}, "
                f"unexpected={sorted(unexpected)}"
            )
        content = template.content
        for name in template.variables:
            content = content.replace(f"{{{{{name}}}}}", str(variables[name]))
        return RenderedPrompt(
            key=template.key,
            version=template.version,
            locale=template.locale,
            content=content,
        )

    def list_templates(self) -> list[PromptTemplate]:
        self._ensure_fresh()
        return [template.model_copy() for template in self._templates]


def build_prompt_registry(settings: Settings) -> PromptRegistry:
    return FilePromptRegistry(
        settings.prompt_catalog_path,
        default_locale=settings.prompt_default_locale,
    )
