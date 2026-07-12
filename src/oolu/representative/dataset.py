"""The training corpus: the user's approved exchanges, made safe and shaped.

Every example is (someone's message -> what the user actually replied),
straight from the exchange memory Phase 0 accumulates. Four rules:

* Scrub first. Every prompt and reply passes the knowledge layer's secret/
  PII scrubber before it can reach a training file — placeholders train
  fine; keys and addresses must not.
* Dedupe on normalized text — a stock answer repeated fifty times would
  otherwise own the adapter.
* Keep the one-liners, downweighted. "sounds good 👍" carries the user's
  style; it just must not collapse the model into monosyllables.
* The newest slice is holdout, never trained on — the perplexity gate that
  decides whether a version may go active reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..knowledge.scrubbing import scrub
from ..replies.engine import normalize_message
from .store import RepresentativeStore

# Below this many usable examples an adapter overfits into parody: the
# worker skips training and Phase-0 behavior simply continues.
COLD_START_FLOOR = 500

# Replies this short (in normalized tokens) are style, not substance.
_ONE_LINER_TOKENS = 3
_ONE_LINER_WEIGHT = 0.3

# The most recent slice of the corpus, reserved for the perplexity gate.
HOLDOUT_FRACTION = 0.1


@dataclass(frozen=True)
class DatasetStats:
    examples: int
    holdout: int
    deduped: int
    scrubbed: int  # examples the scrubber had to touch (kept, cleaned)


def build_sft_dataset(
    store: RepresentativeStore,
    scope: str,
    *,
    max_examples: int = 20_000,
    holdout_fraction: float = HOLDOUT_FRACTION,
) -> tuple[list[dict], list[dict], DatasetStats]:
    """Return (train, holdout, stats), oldest exchange first.

    Each example is chat-format with a per-sample weight:
    ``{"messages": [{"role": "user", ...}, {"role": "assistant", ...}],
    "weight": 1.0}``. Holdout is the most RECENT slice — the model is
    always judged on what the user said last, trained on what came before.
    """
    seen: set[tuple[str, str]] = set()
    examples: list[dict] = []
    deduped = scrubbed = 0
    # The store returns newest first; the corpus reads oldest first.
    for row in reversed(store.exchanges(scope, limit=max_examples)):
        prompt, reply = str(row["prompt_text"]), str(row["reply_text"])
        clean_prompt, clean_reply = scrub(prompt), scrub(reply)
        scrubbed += (clean_prompt, clean_reply) != (prompt, reply)
        reply_tokens = normalize_message(clean_reply).split()
        if not reply_tokens or not normalize_message(clean_prompt):
            continue
        key = (normalize_message(clean_prompt), " ".join(reply_tokens))
        if key in seen:
            deduped += 1
            continue
        seen.add(key)
        examples.append(
            {
                "messages": [
                    {"role": "user", "content": clean_prompt},
                    {"role": "assistant", "content": clean_reply},
                ],
                "weight": (
                    _ONE_LINER_WEIGHT
                    if len(reply_tokens) <= _ONE_LINER_TOKENS
                    else 1.0
                ),
            }
        )
    holdout_size = int(len(examples) * holdout_fraction)
    split = len(examples) - holdout_size
    train, holdout = examples[:split], examples[split:]
    return (
        train,
        holdout,
        DatasetStats(
            examples=len(train),
            holdout=len(holdout),
            deduped=deduped,
            scrubbed=scrubbed,
        ),
    )


def to_jsonl(examples: list[dict]) -> str:
    return "".join(json.dumps(example, ensure_ascii=False) + "\n" for example in examples)
