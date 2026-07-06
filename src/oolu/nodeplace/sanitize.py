from __future__ import annotations

import hashlib

from ..knowledge.scrubbing import is_safe_to_store, scrub
from ..skills.models import ReusableSkill


def sanitize_skill(skill: ReusableSkill) -> tuple[str, str]:
    raw = skill.model_dump_json()
    sanitized = scrub(raw)
    if not is_safe_to_store(sanitized):
        raise ValueError("sanitized skill still contains secret material")
    content_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
    return sanitized, content_hash
