"""The node/route vocabulary — where a token stops being a word.

An LLM's vocabulary is words and word-pieces; this model's vocabulary is
**nodes and routes**. Each node key (``route:{name}`` — the same key the
trace store grades outcomes by) is one token. A composed route re-enters the
library as a single ``NodeContract`` (route-finding-proof.md §3), so the very
same key space already covers routes: one vocabulary tokenizes both, exactly
as the mission asks. Planning a mission then reads like generating a sentence
— except the "words" are executable, typed nodes, and the "sentence" is a
plan the type system can verify.

Two design choices keep this honest and scalable:

- GOALS ARE NOT NODES. A goal is free user text; it conditions the plan but
  never appears in it. Goals hash into a bounded band of ``GOAL`` tokens, so
  the vocabulary's growth is driven by the node/route database (the thing we
  want to scale with), not by the unbounded space of sentences users type.
- THE VOCABULARY GROWS, THE IDS ARE STABLE. New nodes append at the next id
  and never renumber; a trained checkpoint keeps meaning as the marketplace
  grows, and an unknown node at inference maps to ``UNK`` rather than
  corrupting the sequence. This is the same discipline the trace store keeps
  ("rename a node and it starts life over"): identity is the key.

The result is portable JSON — no torch, no framework — so the same vocabulary
file pins tokenization for a pure-Python baseline today and a 30B checkpoint
trained elsewhere tomorrow.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# Reserved ids every vocabulary begins with. Order is frozen: a checkpoint
# and a vocabulary file agree on these before either has seen a single node.
PAD = "<pad>"  # padding for batched sequences
BOS = "<bos>"  # beginning of a plan
EOS = "<eos>"  # the plan is complete
UNK = "<unk>"  # a node the vocabulary has never seen
SPECIAL_TOKENS: tuple[str, ...] = (PAD, BOS, EOS, UNK)

# Goals condition the plan through this many hashed buckets. A band, not a
# per-goal token: structurally similar missions ("process invoice csv files"
# vs "process report pdf files") that hash near each other share gradient,
# and the band never grows no matter how many distinct sentences arrive.
DEFAULT_GOAL_BUCKETS = 4_096

_GOAL_PREFIX = "<goal:"


def goal_token(goal: str, *, buckets: int = DEFAULT_GOAL_BUCKETS) -> str:
    """The conditioning token for a goal string — deterministic, bounded.

    Uses crc32 (never Python's salted ``hash``) so the same goal maps to the
    same token across processes, machines, and training runs.
    """
    bucket = zlib.crc32((goal or "").strip().lower().encode("utf-8")) % buckets
    return f"{_GOAL_PREFIX}{bucket}>"


@dataclass
class NodeVocabulary:
    """A growable, stable bijection between node/route keys and token ids.

    Ids ``0..len(SPECIAL_TOKENS)-1`` are the reserved tokens; the next band
    is the fixed goal vocabulary; node/route keys occupy everything above and
    grow by appending. ``add`` is idempotent and append-only — an id, once
    handed out, is never reused for a different token.
    """

    goal_buckets: int = DEFAULT_GOAL_BUCKETS
    _token_to_id: dict[str, int] = field(default_factory=dict)
    _id_to_token: list[str] = field(default_factory=list)
    frozen: bool = False

    def __post_init__(self) -> None:
        if not self._id_to_token:
            for token in SPECIAL_TOKENS:
                self._append(token)
            for bucket in range(self.goal_buckets):
                self._append(f"{_GOAL_PREFIX}{bucket}>")

    # ------------------------------------------------------------------ #
    # Construction and growth.                                           #
    # ------------------------------------------------------------------ #
    def _append(self, token: str) -> int:
        token_id = len(self._id_to_token)
        self._token_to_id[token] = token_id
        self._id_to_token.append(token)
        return token_id

    def add(self, node_key: str) -> int:
        """Register a node/route key, returning its (stable) id.

        Idempotent: an already-known key returns its existing id. Raises if
        the vocabulary is frozen and the key is new — a frozen vocabulary
        pins a checkpoint's tokenization and must not silently grow under it.
        """
        existing = self._token_to_id.get(node_key)
        if existing is not None:
            return existing
        if self.frozen:
            raise ValueError(
                f"vocabulary is frozen; cannot add new node key {node_key!r}"
            )
        return self._append(node_key)

    def extend(self, node_keys: Iterable[str]) -> None:
        for key in node_keys:
            self.add(key)

    def freeze(self) -> "NodeVocabulary":
        """Pin the vocabulary: further new keys raise instead of appending."""
        self.frozen = True
        return self

    # ------------------------------------------------------------------ #
    # Encoding.                                                           #
    # ------------------------------------------------------------------ #
    @property
    def pad_id(self) -> int:
        return self._token_to_id[PAD]

    @property
    def bos_id(self) -> int:
        return self._token_to_id[BOS]

    @property
    def eos_id(self) -> int:
        return self._token_to_id[EOS]

    @property
    def unk_id(self) -> int:
        return self._token_to_id[UNK]

    def goal_id(self, goal: str) -> int:
        """The id of a goal's conditioning token (always in vocabulary)."""
        return self._token_to_id[goal_token(goal, buckets=self.goal_buckets)]

    def id_of(self, node_key: str, *, add: bool = False) -> int:
        """The id for a node key. Unknown keys map to ``UNK`` unless ``add``.

        ``add=True`` grows the vocabulary (training time); the default keeps
        it fixed and degrades unknown nodes to ``UNK`` (inference time), so an
        unseen marketplace node can never corrupt a sequence's other tokens.
        """
        if add:
            return self.add(node_key)
        return self._token_to_id.get(node_key, self.unk_id)

    def token_of(self, token_id: int) -> str:
        return self._id_to_token[token_id]

    def is_node(self, token_id: int) -> bool:
        """True when the id is a node/route token (not special, not a goal)."""
        return token_id >= len(SPECIAL_TOKENS) + self.goal_buckets

    def __len__(self) -> int:
        return len(self._id_to_token)

    @property
    def node_count(self) -> int:
        """How many node/route keys are registered (excludes reserved bands)."""
        return len(self._id_to_token) - len(SPECIAL_TOKENS) - self.goal_buckets

    # ------------------------------------------------------------------ #
    # Persistence — portable JSON, framework-free.                       #
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """A self-describing snapshot; the reserved/goal bands are implied by
        ``goal_buckets`` and re-derived on load, so only node keys are stored."""
        offset = len(SPECIAL_TOKENS) + self.goal_buckets
        return {
            "goal_buckets": self.goal_buckets,
            "frozen": self.frozen,
            "nodes": self._id_to_token[offset:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeVocabulary":
        vocab = cls(goal_buckets=int(data.get("goal_buckets", DEFAULT_GOAL_BUCKETS)))
        for key in data.get("nodes", []):
            vocab._append(key)
        vocab.frozen = bool(data.get("frozen", False))
        return vocab

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "NodeVocabulary":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ #
    # Builders — grow a vocabulary from the sources that hold the nodes. #
    # ------------------------------------------------------------------ #
    @classmethod
    def from_node_keys(
        cls, keys: Sequence[str], *, goal_buckets: int = DEFAULT_GOAL_BUCKETS
    ) -> "NodeVocabulary":
        vocab = cls(goal_buckets=goal_buckets)
        vocab.extend(keys)
        return vocab
