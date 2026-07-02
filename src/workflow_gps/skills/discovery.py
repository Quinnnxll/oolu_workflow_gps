from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...] = ()


# Curated task tools only — deliberately no general shells/interpreters, so
# discovery never auto-allow-lists a path to arbitrary code execution.
DEFAULT_TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec("ffmpeg", "media", ("video", "audio", "convert", "trim", "transcode")),
    ToolSpec(
        "magick",
        "image",
        ("image", "resize", "convert", "crop", "thumbnail"),
        ("convert",),
    ),
    ToolSpec("pandoc", "document", ("document", "convert", "markdown", "docx", "pdf")),
    ToolSpec(
        "soffice",
        "document",
        ("office", "docx", "xlsx", "pptx", "convert", "pdf"),
        ("libreoffice",),
    ),
    ToolSpec("jq", "data", ("json", "filter", "transform")),
    ToolSpec("sqlite3", "data", ("sql", "database", "query", "sqlite")),
    ToolSpec("curl", "network", ("http", "download", "api", "request")),
    ToolSpec("wget", "network", ("download", "http", "fetch")),
    ToolSpec("git", "vcs", ("git", "repository", "clone", "commit")),
    ToolSpec("zip", "archive", ("zip", "compress", "archive")),
    ToolSpec("unzip", "archive", ("unzip", "extract", "archive")),
    ToolSpec("tar", "archive", ("tar", "archive", "extract", "compress")),
    ToolSpec("gzip", "archive", ("gzip", "compress")),
    ToolSpec("pdftotext", "pdf", ("pdf", "text", "extract")),
    ToolSpec("qpdf", "pdf", ("pdf", "merge", "split", "encrypt")),
    ToolSpec("rg", "search", ("search", "grep", "find", "text"), ("ripgrep",)),
)


class DiscoveredTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    category: str
    tags: list[str] = Field(default_factory=list)


def discover_tools(
    catalog: tuple[ToolSpec, ...] = DEFAULT_TOOL_CATALOG,
    *,
    path: str | None = None,
) -> list[DiscoveredTool]:
    search_path = path or os.environ.get("PATH")
    found: list[DiscoveredTool] = []
    for spec in catalog:
        for candidate in (spec.name, *spec.aliases):
            resolved = shutil.which(candidate, path=search_path)
            if resolved:
                found.append(
                    DiscoveredTool(
                        name=spec.name,
                        path=str(Path(resolved).resolve()),
                        category=spec.category,
                        tags=list(spec.tags),
                    )
                )
                break
    return found


def resolve_file(name: str, *roots: str | Path) -> Path | None:
    expanded = [Path(root).expanduser() for root in roots]
    for root in expanded:
        direct = root / name
        if direct.exists():
            return direct.resolve()
    for root in expanded:
        if root.is_dir():
            for match in sorted(root.rglob(name)):
                return match.resolve()
    return None
