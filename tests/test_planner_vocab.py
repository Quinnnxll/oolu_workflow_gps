"""The vocabulary: a token is a node/route, ids are stable, growth is bounded."""

from __future__ import annotations

import pytest

from oolu.planner import NodeVocabulary, goal_token
from oolu.planner.vocab import BOS, EOS, PAD, SPECIAL_TOKENS, UNK


def test_reserved_tokens_come_first_in_a_frozen_order():
    vocab = NodeVocabulary(goal_buckets=8)
    assert vocab.pad_id == 0
    assert [vocab.token_of(i) for i in range(4)] == [PAD, BOS, EOS, UNK]
    # The goal band follows immediately, then node keys.
    assert vocab.node_count == 0
    assert len(vocab) == len(SPECIAL_TOKENS) + 8


def test_goal_tokens_are_deterministic_and_bounded():
    # Same sentence -> same token across calls (crc32, not salted hash);
    # distinct sentences never grow the vocabulary — the band is fixed.
    a = goal_token("Process the Invoice CSV files", buckets=16)
    b = goal_token("process the invoice csv files", buckets=16)
    assert a == b  # case/whitespace-normalized
    vocab = NodeVocabulary(goal_buckets=16)
    before = len(vocab)
    for i in range(1000):
        vocab.goal_id(f"a totally distinct mission number {i}")
    assert len(vocab) == before  # goals never append


def test_add_is_idempotent_and_ids_are_stable_and_append_only():
    vocab = NodeVocabulary(goal_buckets=4)
    first = vocab.add("route:ingest")
    assert vocab.add("route:ingest") == first  # idempotent
    second = vocab.add("route:validate")
    assert second == first + 1  # append-only
    # A reload preserves the exact ids.
    reloaded = NodeVocabulary.from_dict(vocab.to_dict())
    assert reloaded.id_of("route:ingest") == first
    assert reloaded.id_of("route:validate") == second


def test_unknown_nodes_map_to_unk_not_a_new_id_by_default():
    vocab = NodeVocabulary(goal_buckets=4)
    vocab.add("route:known")
    assert vocab.id_of("route:never-seen") == vocab.unk_id
    assert vocab.token_of(vocab.id_of("route:never-seen")) == UNK
    # ...unless explicitly growing (training time).
    grown = vocab.id_of("route:new", add=True)
    assert grown != vocab.unk_id


def test_freeze_pins_the_vocabulary_against_silent_growth():
    vocab = NodeVocabulary(goal_buckets=4)
    vocab.add("route:a")
    vocab.freeze()
    with pytest.raises(ValueError):
        vocab.add("route:b")
    # Frozen, an unknown key still degrades to UNK rather than raising.
    assert vocab.id_of("route:b") == vocab.unk_id


def test_is_node_distinguishes_nodes_from_special_and_goal_tokens():
    vocab = NodeVocabulary(goal_buckets=8)
    node_id = vocab.add("route:thing")
    assert vocab.is_node(node_id)
    assert not vocab.is_node(vocab.bos_id)
    assert not vocab.is_node(vocab.goal_id("some goal"))


def test_node_count_excludes_the_reserved_and_goal_bands():
    vocab = NodeVocabulary.from_node_keys(
        ["route:a", "route:b", "route:c"], goal_buckets=32
    )
    assert vocab.node_count == 3
    assert len(vocab) == len(SPECIAL_TOKENS) + 32 + 3


def test_persistence_round_trips_only_the_node_keys(tmp_path):
    vocab = NodeVocabulary.from_node_keys(["route:x", "route:y"], goal_buckets=64)
    path = vocab.dump(tmp_path / "vocab.json")
    loaded = NodeVocabulary.load(path)
    assert loaded.goal_buckets == 64
    assert loaded.node_count == 2
    assert loaded.id_of("route:x") == vocab.id_of("route:x")
    assert loaded.id_of("route:y") == vocab.id_of("route:y")
    assert vocab.token_of(1) == BOS  # sanity: band order survives reload
