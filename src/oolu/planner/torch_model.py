"""The real transformer the ladder scales — a reference, behind the extra.

This is the module a training run at 3B/8B/30B actually instantiates. It is
a standard pre-norm decoder-only transformer whose ONLY unusual property is
its vocabulary: the tokens are node/route ids from :class:`NodeVocabulary`,
not word-pieces. The architecture is deliberately conventional so the
scaling behaviour is the well-understood kind — the novelty is entirely in
what a token means, which is a data decision (the vocabulary), not an
architecture one.

It lives behind the ``workflow-plan`` extra and imports torch lazily, in the
same spirit as ``representative/trainer/run_sft.py``: the base install, the
pure-Python baseline, and every test run without torch present. Nothing in
CI instantiates a billion-parameter module; ``parameter_count`` in
``config.py`` is what the tests check, and it agrees with what this module
would build (``num_parameters`` asserts the two never drift).

    pip install 'oolu[workflow-plan]'
"""

from __future__ import annotations

from .config import PlannerConfig, parameter_count

try:  # The heavy stack lives behind the extra; fail with the fix, not a trace.
    import torch
    from torch import nn
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "the node-token planner transformer needs the workflow-plan extra:"
        " pip install 'oolu[workflow-plan]'"
        f" (missing: {exc.name})"
    ) from exc


class _Block(nn.Module):  # pragma: no cover - needs torch + a GPU to be useful
    """One pre-norm transformer block: causal self-attention + MLP."""

    def __init__(self, cfg: PlannerConfig):
        super().__init__()
        self.norm1 = nn.LayerNorm(cfg.d_model)
        self.attn = nn.MultiheadAttention(
            cfg.d_model, cfg.n_heads, batch_first=True
        )
        self.norm2 = nn.LayerNorm(cfg.d_model)
        self.mlp = nn.Sequential(
            nn.Linear(cfg.d_model, cfg.d_ff),
            nn.GELU(),
            nn.Linear(cfg.d_ff, cfg.d_model),
        )

    def forward(self, x, attn_mask):
        h = self.norm1(x)
        attended, _ = self.attn(h, h, h, attn_mask=attn_mask, need_weights=False)
        x = x + attended
        return x + self.mlp(self.norm2(x))


class NodePlannerTransformer(nn.Module):  # pragma: no cover - reference module
    """Decoder-only transformer over node/route tokens.

    Reads a :class:`PlannerConfig`; the same config drives ``parameter_count``,
    so the ladder's size claims and this module agree by construction.
    """

    def __init__(self, cfg: PlannerConfig):
        super().__init__()
        self.cfg = cfg
        self.token_embedding = nn.Embedding(cfg.vocab_size, cfg.d_model)
        self.position_embedding = nn.Embedding(cfg.max_seq_len, cfg.d_model)
        self.blocks = nn.ModuleList(_Block(cfg) for _ in range(cfg.n_layers))
        self.final_norm = nn.LayerNorm(cfg.d_model)
        self.head = nn.Linear(cfg.d_model, cfg.vocab_size, bias=False)
        if cfg.tie_embeddings:
            self.head.weight = self.token_embedding.weight

    def forward(self, token_ids):
        _, length = token_ids.shape
        positions = torch.arange(length, device=token_ids.device)
        x = self.token_embedding(token_ids) + self.position_embedding(positions)
        mask = torch.triu(
            torch.full((length, length), float("-inf"), device=token_ids.device),
            diagonal=1,
        )
        for block in self.blocks:
            x = block(x, mask)
        return self.head(self.final_norm(x))

    def num_parameters(self) -> int:
        """The trainable-parameter count torch reports for this module."""
        return sum(p.numel() for p in self.parameters() if p.requires_grad)


def num_parameters(cfg: PlannerConfig) -> int:  # pragma: no cover - needs torch
    """Build the module and report torch's parameter count — a cross-check
    that ``config.parameter_count`` (the pure-arithmetic estimate CI relies
    on) matches what the real module allocates."""
    built = NodePlannerTransformer(cfg).num_parameters()
    estimated = parameter_count(cfg)
    if built != estimated:
        raise AssertionError(
            f"parameter_count drift: arithmetic={estimated:,} module={built:,}"
        )
    return built
