"""The trainer worker: queue -> corpus -> adapter -> registry -> serving.

One lease-driven loop on a DEDICATED durable queue (the shared host queue
has no kind filter — a trainer must never steal a workflow task). Each
task takes a scope from JSONL to a live adapter with no manual step:

    lease -> build dataset (scrubbed, deduped, holdout split)
          -> QLoRA from base (the Trainer port; subprocess in production)
          -> tar the adapter into the artifact store (the durable record)
          -> registry row: trained, then active (previous version retires)
          -> install + load on the inference server, when one is wired
          -> complete the task with the receipt

Failures fail the registry row AND the task (the queue retries with
backoff); a corpus still under the cold-start floor completes as a skip —
Phase-0 behavior simply continues for that user.
"""

from __future__ import annotations

import argparse
import io
import shutil
import tarfile
import time
from pathlib import Path

from ..dataset import COLD_START_FLOOR, build_sft_dataset, to_jsonl
from ..serving import adapter_name, scope_digest
from ..store import RepresentativeStore
from .jobs import TRAIN_TASK_KIND, sweep
from .sft import Trainer, derive_config

# Training is minutes-long; the lease must outlive it or a second worker
# would double-train the same scope.
DEFAULT_LEASE_S = 4 * 3600.0

# A refresh may regress: keep the new version shelved (trained, not active)
# when its holdout perplexity is more than this factor worse than the
# currently active version's.
PPL_REGRESSION_LIMIT = 1.10


def _pack(adapter_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(adapter_dir.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(adapter_dir)))
    return buffer.getvalue()


class TrainerWorker:
    def __init__(
        self,
        store: RepresentativeStore,
        queue,
        trainer: Trainer,
        artifacts,
        *,
        base_model: str,
        work_root: str | Path,
        adapters_root: str | Path | None = None,
        serving=None,
        owner: str = "representative-trainer",
        lease_seconds: float = DEFAULT_LEASE_S,
        floor: int = COLD_START_FLOOR,
    ):
        self._store = store
        self._queue = queue
        self._trainer = trainer
        self._artifacts = artifacts
        self._base_model = base_model
        self._work_root = Path(work_root)
        self._adapters_root = Path(adapters_root) if adapters_root else None
        self._serving = serving
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._floor = floor

    def run_once(self) -> dict | None:
        """Lease and finish one task; None when the queue is quiet."""
        task = self._queue.lease(self._owner, lease_seconds=self._lease_seconds)
        if task is None:
            return None
        if task.kind != TRAIN_TASK_KIND:
            # A dedicated queue makes this unreachable; a shared one must
            # not have its foreign tasks burned by us.
            self._queue.fail(
                task.task_id,
                self._owner,
                error=f"not a {TRAIN_TASK_KIND} task",
            )
            return None
        scope = str(task.payload.get("scope") or "")
        version_out: list[int] = []
        try:
            result = self._train_scope(scope, version_out)
        except Exception as exc:  # the queue owns retries; the row owns truth
            if version_out:
                self._store.fail_adapter(scope, version_out[0])
            self._queue.fail(task.task_id, self._owner, error=str(exc)[:2000])
            return {"scope": scope, "error": str(exc)}
        self._queue.complete(task.task_id, self._owner, result=result)
        return result

    def _train_scope(self, scope: str, version_out: list[int]) -> dict:
        train, holdout, stats = build_sft_dataset(self._store, scope)
        if stats.examples + stats.holdout < self._floor:
            return {
                "scope": scope,
                "skipped": "cold_start",
                "examples": stats.examples + stats.holdout,
                "floor": self._floor,
            }
        count = self._store.exchange_count(scope)
        version = self._store.begin_adapter(
            scope, base_model=self._base_model, message_count=count
        )
        version_out.append(version)
        name = adapter_name(scope, version)

        work_dir = self._work_root / scope_digest(scope) / f"v{version}"
        work_dir.mkdir(parents=True, exist_ok=True)
        train_path = work_dir / "train.jsonl"
        holdout_path = work_dir / "holdout.jsonl"
        train_path.write_text(to_jsonl(train), encoding="utf-8")
        holdout_path.write_text(to_jsonl(holdout), encoding="utf-8")

        previous = self._store.active_adapter(scope)
        trained = self._trainer.train(
            derive_config(
                base_model=self._base_model,
                example_count=stats.examples,
                train_path=train_path,
                holdout_path=holdout_path,
                output_dir=work_dir / "adapter",
            )
        )
        artifact_ref = self._artifacts.put(
            name, _pack(trained.adapter_dir), media_type="application/gzip"
        )
        self._store.finish_adapter(
            scope, version, artifact_ref=artifact_ref, holdout_ppl=trained.holdout_ppl
        )

        activated = self._may_activate(previous, trained.holdout_ppl)
        if activated:
            self._install(name, trained.adapter_dir)
            self._store.activate_adapter(scope, version)
        return {
            "scope": scope,
            "version": version,
            "adapter": name,
            "activated": activated,
            "artifact_ref": artifact_ref,
            "holdout_ppl": trained.holdout_ppl,
            "examples": stats.examples,
            "deduped": stats.deduped,
            "scrubbed": stats.scrubbed,
        }

    @staticmethod
    def _may_activate(previous, holdout_ppl: float | None) -> bool:
        """The perplexity gate: a refresh that models the user's held-out
        messages clearly WORSE than the live version stays shelved."""
        if previous is None or holdout_ppl is None:
            return True
        previous_ppl = previous["holdout_ppl"]
        if previous_ppl is None:
            return True
        return holdout_ppl <= float(previous_ppl) * PPL_REGRESSION_LIMIT

    def _install(self, name: str, adapter_dir: Path) -> None:
        """Give the inference server a stable copy and load it."""
        live_dir = adapter_dir
        if self._adapters_root is not None:
            live_dir = self._adapters_root / name
            if live_dir.exists():
                shutil.rmtree(live_dir)
            live_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(adapter_dir, live_dir)
        if self._serving is not None:
            self._serving.load(name, live_dir)

    def run_forever(self, *, poll_s: float = 30.0) -> None:  # pragma: no cover
        """Sweep due scopes onto the queue, then drain it — repeat."""
        while True:
            sweep(self._store, self._queue, floor=self._floor)
            while self.run_once() is not None:
                pass
            time.sleep(poll_s)


def main() -> None:  # pragma: no cover - the ops entry point
    from ...durable.artifacts import FilesystemArtifactStore
    from ...durable.connection import DurableConnection
    from ...durable.queue import DurableTaskQueue
    from ..serving import VllmAdapterServer
    from .sft import SubprocessTrainer

    parser = argparse.ArgumentParser(
        description="OoLu representative trainer worker (Phase 1)"
    )
    parser.add_argument("--data", default="~/.oolu", help="the host's data dir")
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--vllm", default=None, help="vLLM api_base for live loads")
    parser.add_argument("--poll", type=float, default=30.0)
    args = parser.parse_args()

    data = Path(args.data).expanduser()
    store = RepresentativeStore(data / "representative.db")
    queue = DurableTaskQueue(DurableConnection(data / "representative-queue.db"))
    serving = (
        VllmAdapterServer(store, api_base=args.vllm) if args.vllm else None
    )
    TrainerWorker(
        store,
        queue,
        SubprocessTrainer(),
        FilesystemArtifactStore(data / "adapters" / "artifacts"),
        base_model=args.base_model,
        work_root=data / "adapters" / "work",
        adapters_root=data / "adapters" / "live",
        serving=serving,
    ).run_forever(poll_s=args.poll)


if __name__ == "__main__":  # pragma: no cover
    main()
