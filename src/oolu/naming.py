"""Concise, keyword-oriented names for auto-created nodes and skills.

A node's name is a label, not a transcript (docs/node-generation.md §4:
"human-facing, imperative-object style"). When the system names something
itself — a learned skill, a listing whose contributor gave no title, a
desk entry with nothing better than a skill id — the name must be the
task's load-bearing keywords, never the whole sentence the user typed.
The sentence stays where it belongs: in the description.

Explicit names always win; this module only shapes the FALLBACKS.
"""

from __future__ import annotations

import re

# Words that carry no identity: articles, pronouns, politeness, glue.
STOPWORDS = frozenset(
    """a an and are as at be but by can could do does for from get go has
    have hi how i in into is it its just let like make me my need now of
    on onto or our out please so some than that the their them then there
    they this to up us want was we what when which will with would you
    your""".split()
)

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

MAX_KEYWORDS = 4


def keywords(text: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """The first ``limit`` distinct non-stopword tokens, in spoken order —
    order preserved because 'report pdf' and 'pdf report' name different
    intents."""
    seen: set[str] = set()
    picked: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        picked.append(token)
        if len(picked) >= limit:
            break
    return picked


def concise_name(text: str, limit: int = MAX_KEYWORDS) -> str:
    """A display name from a task sentence: 'convert the quarterly report
    to pdf and email it' -> 'Convert Quarterly Report Pdf'. Falls back to
    a trimmed slice of the original when nothing survives the stopword
    filter (e.g. 'do it for me')."""
    words = keywords(text, limit)
    if not words:
        fallback = (text or "").strip()
        return fallback if len(fallback) <= 32 else fallback[:32] + "…"
    return " ".join(word.capitalize() for word in words)


def keyword_slug(text: str, limit: int = MAX_KEYWORDS, sep: str = ".") -> str:
    """A machine identity from the same keywords: 'convert.quarterly.
    report.pdf'. Empty when nothing survives — callers keep their own
    fallback."""
    return sep.join(keywords(text, limit))
