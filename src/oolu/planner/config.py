"""The scaling ladder: a decoder-only transformer sized by config, not code.

route-finding-proof.md §5 reserved a socket for a small learned model; this
module describes the model that grows to fill it as the node/route database
grows. The point is not to train a 30B model today — it is that the SAME
architecture scales from a runnable ``tiny`` reference to ``s3b``/``s8b``/
``s30b`` by changing four numbers, so the training curriculum ("start at 3B,
then 8B, then 30B, as more users, more nodes, and more routes accumulate") is
a config change, never a rewrite.

``PlannerConfig`` is a plain, JSON-serializable dataclass — no torch, no
import cost. ``parameter_count`` reports the exact number of trainable
parameters a standard pre-norm decoder-only transformer with these
hyperparameters would have, so a preset's claim ("~3B") is checkable in a
unit test with arithmetic alone. ``torch_model.py`` reads the same config to
build the real module behind the ``workflow-plan`` extra.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# The vocabulary capacity the big presets are allocated — room for the
# node/route database to grow into. The live vocabulary (planner/vocab.py)
# starts far smaller and grows with the marketplace; this is the ceiling a
# checkpoint was trained against, not today's node count.
DEFAULT_VOCAB_CAPACITY = 131_072

# How many nodes a single mission plan may span. Plans are short compared to
# language — a 512-token plan is an enormous workflow — so this is generous.
DEFAULT_MAX_PLAN_LEN = 1024


@dataclass(frozen=True)
class PlannerConfig:
    """Hyperparameters of a decoder-only planning transformer.

    ``d_model`` must be divisible by ``n_heads``. ``d_ff`` defaults to the
    conventional 4× width when constructed through :meth:`sized`.
    """

    vocab_size: int
    d_model: int
    n_layers: int
    n_heads: int
    d_ff: int
    max_seq_len: int = DEFAULT_MAX_PLAN_LEN
    tie_embeddings: bool = True

    def __post_init__(self) -> None:
        if self.d_model % self.n_heads != 0:
            raise ValueError(
                f"d_model ({self.d_model}) must be divisible by "
                f"n_heads ({self.n_heads})"
            )
        for name in ("vocab_size", "d_model", "n_layers", "n_heads", "d_ff"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @property
    def head_dim(self) -> int:
        return self.d_model // self.n_heads

    @classmethod
    def sized(
        cls,
        *,
        d_model: int,
        n_layers: int,
        n_heads: int,
        vocab_size: int = DEFAULT_VOCAB_CAPACITY,
        ff_multiplier: int = 4,
        max_seq_len: int = DEFAULT_MAX_PLAN_LEN,
        tie_embeddings: bool = True,
    ) -> "PlannerConfig":
        """Build a config with the conventional ``d_ff = 4 * d_model``."""
        return cls(
            vocab_size=vocab_size,
            d_model=d_model,
            n_layers=n_layers,
            n_heads=n_heads,
            d_ff=ff_multiplier * d_model,
            max_seq_len=max_seq_len,
            tie_embeddings=tie_embeddings,
        )

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "PlannerConfig":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def parameter_count(cfg: PlannerConfig) -> int:
    """Exact trainable-parameter count of a pre-norm decoder-only transformer.

    Counts, per the standard architecture: token + learned positional
    embeddings; per layer four attention projections (Q/K/V/O, with bias),
    a two-matrix MLP (with bias), and two LayerNorms (weight + bias); a final
    LayerNorm; and an output head that is free when embeddings are tied.
    """
    d, ff = cfg.d_model, cfg.d_ff
    embeddings = cfg.vocab_size * d + cfg.max_seq_len * d
    attn = 4 * (d * d + d)
    mlp = (d * ff + ff) + (ff * d + d)
    norms = 2 * (2 * d)
    per_layer = attn + mlp + norms
    final_norm = 2 * d
    head = 0 if cfg.tie_embeddings else cfg.vocab_size * d
    return embeddings + cfg.n_layers * per_layer + final_norm + head


def human_size(n: int) -> str:
    """A compact ``2.9B`` / ``7.8B`` / ``30.5B`` label for a parameter count."""
    for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f}{unit}"
    return str(n)


# The curriculum. ``tiny`` is dependency-light and small enough to instantiate
# in a test or on a laptop (small vocab, four layers); the rest are the
# training rungs named in the mission — each a four-number change away from
# its neighbour, each verified against its nominal size in test_planner_config.
PLANNER_PRESETS: dict[str, PlannerConfig] = {
    "tiny": PlannerConfig.sized(
        d_model=256, n_layers=4, n_heads=4, vocab_size=8_192, max_seq_len=256
    ),
    "s3b": PlannerConfig.sized(d_model=2560, n_layers=32, n_heads=20),
    "s8b": PlannerConfig.sized(d_model=4096, n_layers=36, n_heads=32),
    "s30b": PlannerConfig.sized(d_model=7168, n_layers=48, n_heads=56),
}


def preset(name: str) -> PlannerConfig:
    """Look up a curriculum rung by name (``tiny``/``s3b``/``s8b``/``s30b``)."""
    try:
        return PLANNER_PRESETS[name]
    except KeyError:
        rungs = ", ".join(PLANNER_PRESETS)
        raise KeyError(f"unknown planner preset {name!r}; choose one of: {rungs}")
