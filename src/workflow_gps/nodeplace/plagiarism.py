from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_duplicate_hash(content_hash: str, known_hashes: set[str]) -> bool:
    return content_hash in known_hashes


def is_plagiarism(
    artifact: str, corpus: dict[str, str], *, threshold: float = 0.9
) -> str | None:
    for version_id, existing in corpus.items():
        if similarity(artifact, existing) >= threshold:
            return version_id
    return None
