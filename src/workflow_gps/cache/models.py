"""Value objects stored by the script cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScriptCacheEntry:
    key: str
    script: str
    dependencies: tuple[str, ...]
    tier: str
    model: str
    success_count: int
    failure_count: int
    created_at: datetime
    updated_at: datetime
