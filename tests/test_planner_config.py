"""The scaling ladder is a config change, and its size claims are arithmetic."""

from __future__ import annotations

import pytest

from oolu.planner import (
    PLANNER_PRESETS,
    PlannerConfig,
    human_size,
    parameter_count,
    preset,
)


def test_the_curriculum_presets_land_at_their_named_sizes():
    # The mission names 3B, 8B, 30B rungs; each preset must actually be that
    # size (within a sane band), so "start at 3B, then 8B, then 30B" is a
    # real, checkable ladder — not aspirational labels.
    bands = {
        "s3b": (2.5e9, 3.5e9),
        "s8b": (7.0e9, 9.0e9),
        "s30b": (27.0e9, 33.0e9),
    }
    for name, (low, high) in bands.items():
        count = parameter_count(preset(name))
        assert low <= count <= high, f"{name} = {human_size(count)}"


def test_tiny_is_small_enough_to_be_a_runnable_reference():
    assert parameter_count(preset("tiny")) < 20_000_000


def test_parameter_count_matches_a_hand_computed_decoder_block():
    # A one-layer, tied-embedding config, computed by hand:
    #   embeddings: V*d + P*d
    #   attn: 4*(d*d + d);  mlp: (d*ff+ff)+(ff*d+d);  norms: 2*(2d)
    #   final norm: 2d;  tied head: 0
    d, ff, layers, vocab, seq = 8, 32, 1, 100, 16
    cfg = PlannerConfig(
        vocab_size=vocab, d_model=d, n_layers=layers, n_heads=2,
        d_ff=ff, max_seq_len=seq, tie_embeddings=True,
    )
    embeddings = vocab * d + seq * d
    attn = 4 * (d * d + d)
    mlp = (d * ff + ff) + (ff * d + d)
    norms = 2 * (2 * d)
    expected = embeddings + layers * (attn + mlp + norms) + 2 * d
    assert parameter_count(cfg) == expected


def test_untied_embeddings_add_exactly_one_output_matrix():
    tied = PlannerConfig.sized(d_model=64, n_layers=2, n_heads=4, vocab_size=500)
    untied = PlannerConfig.sized(
        d_model=64, n_layers=2, n_heads=4, vocab_size=500, tie_embeddings=False
    )
    assert parameter_count(untied) - parameter_count(tied) == 500 * 64


def test_d_model_must_divide_into_heads():
    with pytest.raises(ValueError):
        PlannerConfig.sized(d_model=100, n_layers=1, n_heads=7)


def test_config_round_trips_through_json(tmp_path):
    cfg = preset("s8b")
    path = cfg.dump(tmp_path / "cfg.json")
    assert PlannerConfig.load(path) == cfg


def test_preset_rejects_an_unknown_rung():
    with pytest.raises(KeyError):
        preset("s70b")


def test_sized_uses_the_conventional_four_times_width():
    cfg = PlannerConfig.sized(d_model=512, n_layers=1, n_heads=8)
    assert cfg.d_ff == 4 * 512
    assert cfg.head_dim == 64


def test_every_preset_is_constructible_and_positive():
    for name, cfg in PLANNER_PRESETS.items():
        assert parameter_count(cfg) > 0, name
