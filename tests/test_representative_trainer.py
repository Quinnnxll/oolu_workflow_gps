"""Representative mode, Phase 1: the adapter pipeline, queue to live voice.

Exit gate (docs/representative-plan.md): the worker takes a scope from
queue -> scrubbed JSONL -> adapter artifact -> registry row -> serving
registration with no manual step; a corpus under the cold-start floor
skips (Phase-0 behavior continues); a failed run fails the registry row
AND the task without wedging the user; the perplexity gate shelves a
regressed refresh; and a live adapter becomes the voice the engine drafts
with — falling back to the shared model when the adapter server dies.
All pure Python: a fake trainer, a fake transport, real SQLite queues.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.durable.connection import DurableConnection
from oolu.durable.queue import DurableTaskQueue, TaskStatus
from oolu.representative import (
    RepresentativeEngine,
    RepresentativeStore,
    StoreAdapterServer,
    VllmAdapterServer,
    adapter_name,
    build_sft_dataset,
)
from oolu.representative.trainer import (
    TRAIN_TASK_KIND,
    SubprocessTrainer,
    TrainerWorker,
    TrainingError,
    refresh_reason,
    sweep,
)
from oolu.representative.trainer.jobs import REFRESH_AGE_S, REFRESH_NEW_EXCHANGES
from oolu.representative.trainer.sft import ADAPTER_MARKER, TrainedAdapter

NOW = 1_700_000_000.0


def _store(clock=None) -> RepresentativeStore:
    return RepresentativeStore(":memory:", clock=clock or (lambda: NOW))


def _seed(store, scope, n, *, prefix="q"):
    store.configure(scope, mode="draft")
    for i in range(n):
        store.remember_exchange(
            scope,
            key=f"{prefix}{i}",
            prompt=f"{prefix} question number {i} about the deploy?",
            reply=f"answer {i}: push to main and the action does the rest",
        )


# --------------------------------------------------------------------------- #
# The registry: one live voice per scope, ever.                                #
# --------------------------------------------------------------------------- #
def test_the_registry_lifecycle_keeps_one_active_voice():
    store = _store()
    v1 = store.begin_adapter("s1", base_model="qwen", message_count=500)
    assert v1 == 1 and store.active_adapter("s1") is None
    store.finish_adapter("s1", v1, artifact_ref="sha256:aa", holdout_ppl=4.0)
    store.activate_adapter("s1", v1)
    assert int(store.active_adapter("s1")["version"]) == 1

    v2 = store.begin_adapter("s1", base_model="qwen", message_count=800)
    store.finish_adapter("s1", v2, artifact_ref="sha256:bb", holdout_ppl=3.5)
    store.activate_adapter("s1", v2)
    active = store.active_adapter("s1")
    assert int(active["version"]) == 2 and active["artifact_ref"] == "sha256:bb"
    statuses = {int(r["version"]): r["status"] for r in store.adapter_history("s1")}
    assert statuses == {1: "retired", 2: "active"}

    # A failed pending version never becomes anything else.
    v3 = store.begin_adapter("s1", base_model="qwen", message_count=900)
    store.fail_adapter("s1", v3)
    store.activate_adapter("s1", v3)  # refuses: only trained versions activate
    assert int(store.active_adapter("s1")["version"]) == 2

    # Erasure takes the registry with everything else.
    store.configure("s1", mode="draft")
    assert store.erase("s1") >= 4
    assert store.active_adapter("s1") is None and store.adapter_history("s1") == []


# --------------------------------------------------------------------------- #
# The corpus: scrubbed, deduped, weighted, holdout split.                      #
# --------------------------------------------------------------------------- #
def test_the_dataset_is_scrubbed_deduped_and_weighted():
    store = _store()
    # Assembled at runtime so the tree-wide secret scanner never sees a
    # key-shaped literal; the scrubber still sees a real-shaped one.
    fake_key = "sk-" + "abcdefghij1234567890"
    store.remember_exchange(
        "s1", key="a", prompt="where do I send it?",
        reply=f"mail quinn@mphepo.io and cc the key {fake_key}",
    )
    store.remember_exchange("s1", key="b", prompt="review my PR?", reply="on it 👍")
    store.remember_exchange("s1", key="c", prompt="review my PR?", reply="on it 👍")
    for i in range(7):
        store.remember_exchange(
            "s1", key=f"d{i}", prompt=f"question {i} about deploys?",
            reply=f"longer answer number {i} with real substance in it",
        )
    train, holdout, stats = build_sft_dataset(store, "s1", holdout_fraction=0.2)

    everything = train + holdout
    assert stats.deduped == 1 and stats.scrubbed == 1
    assert len(everything) == 9 and stats.holdout == 1
    flat = json.dumps(everything)
    assert "quinn@mphepo.io" not in flat and fake_key not in flat
    assert "<EMAIL>" in flat and "<API_KEY>" in flat
    weights = {
        example["messages"][1]["content"]: example["weight"] for example in everything
    }
    assert weights["on it 👍"] == pytest.approx(0.3)
    assert all(
        weight == 1.0 for reply, weight in weights.items() if reply != "on it 👍"
    )
    # Oldest first; the holdout is the newest slice.
    assert everything[0]["messages"][0]["content"] == "where do I send it?"
    assert holdout[0]["messages"][0]["content"] == "question 6 about deploys?"


# --------------------------------------------------------------------------- #
# The refresh policy and the sweep.                                            #
# --------------------------------------------------------------------------- #
def test_refresh_reasons_floor_first_then_fresh_messages_then_age():
    store = _store()
    _seed(store, "s1", 4)
    assert refresh_reason(store, "s1", now=NOW, floor=5) is None
    _seed(store, "s1", 1, prefix="extra")
    assert "first adapter" in refresh_reason(store, "s1", now=NOW, floor=5)

    version = store.begin_adapter("s1", base_model="qwen", message_count=5)
    store.finish_adapter("s1", version, artifact_ref="sha256:aa", holdout_ppl=4.0)
    store.activate_adapter("s1", version)
    assert refresh_reason(store, "s1", now=NOW, floor=5) is None
    _seed(store, "s1", REFRESH_NEW_EXCHANGES, prefix="new")
    assert "new exchanges" in refresh_reason(store, "s1", now=NOW, floor=5)
    fresh_store = _store()
    _seed(fresh_store, "s2", 5)
    v = fresh_store.begin_adapter("s2", base_model="qwen", message_count=5)
    fresh_store.finish_adapter("s2", v, artifact_ref="sha256:bb")
    fresh_store.activate_adapter("s2", v)
    assert refresh_reason(fresh_store, "s2", now=NOW, floor=5) is None
    assert "days old" in refresh_reason(
        fresh_store, "s2", now=NOW + REFRESH_AGE_S + 1, floor=5
    )


def test_the_sweep_never_queues_the_same_corpus_twice(tmp_path):
    store = _store()
    queue = DurableTaskQueue(DurableConnection(tmp_path / "queue.db"))
    _seed(store, "s1", 6)
    store.configure("s2", mode="off")  # off scopes never sweep

    first = sweep(store, queue, now=NOW, floor=5)
    again = sweep(store, queue, now=NOW, floor=5)
    assert [t.task_id for t in first] == [t.task_id for t in again]
    assert first[0].kind == TRAIN_TASK_KIND and first[0].payload["scope"] == "s1"
    # New words move the idempotency key: a fresh corpus queues fresh work.
    _seed(store, "s1", 1, prefix="more")
    assert sweep(store, queue, now=NOW, floor=5)[0].task_id != first[0].task_id


# --------------------------------------------------------------------------- #
# The subprocess trainer honors the contract, and enforces it.                 #
# --------------------------------------------------------------------------- #
_FAKE_TRAIN = (
    "import json, sys, pathlib;"
    "cfg = json.loads(pathlib.Path(sys.argv[1]).read_text());"
    "out = pathlib.Path(cfg['output_dir']);"
    "(out / 'adapter_config.json').write_text('{}');"
    "(out / 'adapter_model.safetensors').write_text('fake-weights');"
    "(out / 'metrics.json').write_text(json.dumps({'holdout_ppl': 4.25}))"
)


def test_the_subprocess_trainer_runs_the_contract(tmp_path):
    from oolu.representative.trainer.sft import derive_config

    config = derive_config(
        base_model="qwen", example_count=100,
        train_path=tmp_path / "t.jsonl", holdout_path=tmp_path / "h.jsonl",
        output_dir=tmp_path / "adapter",
    )
    assert config.rank == 16 and config.epochs == 3  # small corpus shape
    trained = SubprocessTrainer(
        [sys.executable, "-c", _FAKE_TRAIN, "{config}"]
    ).train(config)
    assert trained.holdout_ppl == pytest.approx(4.25)
    assert (trained.adapter_dir / ADAPTER_MARKER).exists()

    with pytest.raises(TrainingError, match="exited 3"):
        SubprocessTrainer(
            [sys.executable, "-c", "import sys; sys.exit(3)", "{config}"]
        ).train(config)
    with pytest.raises(TrainingError, match="no adapter_config.json"):
        SubprocessTrainer([sys.executable, "-c", "pass", "{config}"]).train(
            derive_config(
                base_model="qwen", example_count=100,
                train_path=tmp_path / "t.jsonl", holdout_path=tmp_path / "h.jsonl",
                output_dir=tmp_path / "empty",
            )
        )


# --------------------------------------------------------------------------- #
# The worker: queue to live voice, no manual step.                             #
# --------------------------------------------------------------------------- #
class _FakeTrainer:
    def __init__(self, ppl=4.0, fail=False):
        self._ppl, self._fail = ppl, fail

    def train(self, config):
        if self._fail:
            raise TrainingError("gpu caught fire")
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / ADAPTER_MARKER).write_text("{}", encoding="utf-8")
        (out / "adapter_model.safetensors").write_text("w", encoding="utf-8")
        return TrainedAdapter(adapter_dir=out, holdout_ppl=self._ppl)


class _FakeServing:
    def __init__(self):
        self.loaded: list[tuple[str, str]] = []

    def load(self, name, adapter_dir):
        self.loaded.append((name, str(adapter_dir)))


def _worker(tmp_path, store, queue, *, trainer=None, serving=None, floor=5):
    return TrainerWorker(
        store,
        queue,
        trainer or _FakeTrainer(),
        FilesystemArtifactStore(tmp_path / "artifacts"),
        base_model="qwen/base",
        work_root=tmp_path / "work",
        adapters_root=tmp_path / "live",
        serving=serving,
        floor=floor,
    )


def test_the_worker_takes_a_scope_from_queue_to_live_adapter(tmp_path):
    store = _store()
    queue = DurableTaskQueue(DurableConnection(tmp_path / "queue.db"))
    serving = _FakeServing()
    _seed(store, "s1", 10)
    [task] = sweep(store, queue, now=NOW, floor=5)

    worker = _worker(tmp_path, store, queue, serving=serving)
    result = worker.run_once()
    assert result["activated"] is True and result["version"] == 1
    assert result["adapter"] == adapter_name("s1", 1)
    assert queue.get(task.task_id).status is TaskStatus.DONE

    # Registry: active, with the artifact reference and the ppl on the row.
    active = store.active_adapter("s1")
    assert active["status"] == "active"
    assert active["artifact_ref"].startswith("sha256:")
    assert active["holdout_ppl"] == pytest.approx(4.0)

    # The artifact really is the adapter, durably.
    blob = FilesystemArtifactStore(tmp_path / "artifacts").get(
        active["artifact_ref"]
    )
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        assert ADAPTER_MARKER in archive.getnames()

    # Serving got a stable copy and the load call.
    [(name, path)] = serving.loaded
    assert name == result["adapter"] and Path(path, ADAPTER_MARKER).exists()
    assert worker.run_once() is None  # the queue is drained


def test_cold_start_skips_and_failure_fails_both_row_and_task(tmp_path):
    store = _store()
    queue = DurableTaskQueue(DurableConnection(tmp_path / "queue.db"))
    _seed(store, "cold", 3)
    queue.enqueue(TRAIN_TASK_KIND, {"scope": "cold"})
    result = _worker(tmp_path, store, queue).run_once()
    assert result["skipped"] == "cold_start"
    assert store.adapter_history("cold") == []  # no row ever opened

    _seed(store, "hot", 10)
    task = queue.enqueue(TRAIN_TASK_KIND, {"scope": "hot"}, max_attempts=1)
    result = _worker(
        tmp_path, store, queue, trainer=_FakeTrainer(fail=True)
    ).run_once()
    assert "gpu caught fire" in result["error"]
    assert queue.get(task.task_id).status is TaskStatus.DEAD
    [row] = store.adapter_history("hot")
    assert row["status"] == "failed"
    # The user is not wedged: Phase-0 drafting still has no active adapter.
    assert store.active_adapter("hot") is None


def test_a_regressed_refresh_is_shelved_by_the_perplexity_gate(tmp_path):
    store = _store()
    queue = DurableTaskQueue(DurableConnection(tmp_path / "queue.db"))
    _seed(store, "s1", 10)
    queue.enqueue(TRAIN_TASK_KIND, {"scope": "s1"}, idempotency_key="one")
    assert _worker(tmp_path, store, queue).run_once()["activated"] is True

    queue.enqueue(TRAIN_TASK_KIND, {"scope": "s1"}, idempotency_key="two")
    result = _worker(
        tmp_path, store, queue, trainer=_FakeTrainer(ppl=9.9)
    ).run_once()
    assert result["activated"] is False
    assert int(store.active_adapter("s1")["version"]) == 1  # v1 stays live
    statuses = {int(r["version"]): r["status"] for r in store.adapter_history("s1")}
    assert statuses[2] == "trained"  # shelved, not lost


# --------------------------------------------------------------------------- #
# Serving: the live adapter is the voice the engine drafts with.               #
# --------------------------------------------------------------------------- #
class _FakeVllm:
    """A transport standing in for the whole vLLM server."""

    def __init__(self, text="totally my voice"):
        self.calls: list[tuple[str, dict]] = []
        self._text = text
        self.dead = False

    def __call__(self, url, payload):
        if self.dead:
            raise urllib.error.URLError("connection refused")
        self.calls.append((url, payload))
        return {"choices": [{"message": {"content": self._text}}]}


class _Parrot:
    def reply(self, messages):
        return "shared-model words"


def _activated_store():
    store = _store()
    store.configure("s1", mode="draft")
    version = store.begin_adapter("s1", base_model="qwen", message_count=500)
    store.finish_adapter("s1", version, artifact_ref="sha256:aa", holdout_ppl=4.0)
    store.activate_adapter("s1", version)
    return store


def test_the_live_adapter_speaks_and_a_dead_server_degrades():
    store = _activated_store()
    transport = _FakeVllm()
    server = VllmAdapterServer(store, api_base="http://vllm:8000/v1", transport=transport)
    name = adapter_name("s1", 1)
    assert server.model_for("s1") == name
    assert StoreAdapterServer(store).model_for("nobody") is None

    server.load(name, "/adapters/" + name)
    assert transport.calls[0] == (
        "http://vllm:8000/v1/load_lora_adapter",
        {"lora_name": name, "lora_path": "/adapters/" + name},
    )

    engine = RepresentativeEngine(store, model=_Parrot(), adapters=server)
    draft = engine.draft(
        "s1", conversation_id="bob", inbound_text="you around?", display_name="alice"
    )
    assert draft.generated_text == "totally my voice"
    assert draft.adapter_version == name
    url, payload = transport.calls[-1]
    assert url.endswith("/chat/completions") and payload["model"] == name

    # The server dies: the shared model answers, honestly labeled "base".
    transport.dead = True
    fallback = engine.draft(
        "s1", conversation_id="bob", inbound_text="still there?", display_name="alice"
    )
    assert fallback.generated_text == "shared-model words"
    assert fallback.adapter_version == "base"

    # And the engine's status names the live voice.
    assert engine.status("s1")["adapter"] == name
