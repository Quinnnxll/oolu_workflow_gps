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


# Two goals at or above this similarity are the SAME work said twice —
# close enough that minting a second node would split one history in two.
# Conservative on purpose: it gates an OFFER to reuse (a question, never a
# silent reroute), and the build door's refusal names the near-match so a
# genuinely different goal can be said more distinctly.
NEAR_GOAL_SIMILARITY = 0.6


def _content_words(text: str) -> list[str]:
    """The sentence's load-bearing tokens, in spoken order — the same
    stopword filter names are built from, so 'please normalize the invoice
    csvs' and 'normalize invoice csvs' read as one goal."""
    return [
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if token not in STOPWORDS
    ]


def _trigrams(text: str) -> set[str]:
    if len(text) < 3:
        return {text} if text else set()
    return {text[i : i + 3] for i in range(len(text) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def goal_similarity(a: str, b: str) -> float:
    """How close two task sentences are AS GOALS, 0..1.

    Two signals over the stopword-filtered content words, and the max of
    the two wins: token overlap catches reordered wordings, character
    trigrams catch morphology — 'csvs' vs 'csv files' shares almost no
    tokens but nearly every trigram. Compare against
    :data:`NEAR_GOAL_SIMILARITY` to call a twin a twin."""
    words_a, words_b = _content_words(a), _content_words(b)
    tokens = _jaccard(set(words_a), set(words_b))
    chars = _jaccard(_trigrams(" ".join(words_a)), _trigrams(" ".join(words_b)))
    return max(tokens, chars)
