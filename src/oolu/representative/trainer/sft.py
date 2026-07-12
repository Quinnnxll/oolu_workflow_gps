"""The SFT seam: a config, a Trainer port, and the subprocess default.

Training runs OUTSIDE this process — a pinned script (run_sft.py, or
whatever command the operator configures) reads a JSON config, trains a
QLoRA adapter from base, and leaves two things in the output directory:
the PEFT adapter files and a ``metrics.json`` with the holdout perplexity.
The worker never imports torch; a GPU box and a test fake satisfy the same
contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class TrainingError(RuntimeError):
    """The training run failed or produced no usable adapter."""


@dataclass(frozen=True)
class SftConfig:
    """Everything one training run needs, JSON-serializable."""

    base_model: str
    train_path: str
    holdout_path: str
    output_dir: str
    rank: int = 16
    epochs: int = 2
    learning_rate: float = 2e-4
    max_seq_len: int = 1024

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "SftConfig":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def derive_config(
    *,
    base_model: str,
    example_count: int,
    train_path: str | Path,
    holdout_path: str | Path,
    output_dir: str | Path,
) -> SftConfig:
    """Hyperparameters follow corpus size: small corpora get lower rank and
    an extra epoch; large ones get more capacity and less repetition."""
    if example_count < 2_000:
        rank, epochs = 16, 3
    elif example_count < 10_000:
        rank, epochs = 32, 2
    else:
        rank, epochs = 32, 1
    return SftConfig(
        base_model=base_model,
        train_path=str(train_path),
        holdout_path=str(holdout_path),
        output_dir=str(output_dir),
        rank=rank,
        epochs=epochs,
    )


@dataclass(frozen=True)
class TrainedAdapter:
    adapter_dir: Path
    holdout_ppl: float | None


@runtime_checkable
class Trainer(Protocol):
    """Port for whatever turns a config into adapter files."""

    def train(self, config: SftConfig) -> TrainedAdapter: ...


# The marker file every PEFT save leaves behind — its presence is the
# cheapest honest signal that training actually produced an adapter.
ADAPTER_MARKER = "adapter_config.json"
METRICS_FILE = "metrics.json"


def run_training_command(
    command_template: list[str], config, *, timeout_s: float
) -> TrainedAdapter:
    """Run one training subprocess against the shared contract: the config
    (anything with ``dump`` and ``output_dir``) goes in as JSON, adapter
    files plus optional metrics come out of output_dir — or TrainingError
    says exactly what didn't."""
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = config.dump(output_dir / "train-config.json")
    command = [part.replace("{config}", str(config_path)) for part in command_template]
    try:
        run = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise TrainingError(f"training timed out after {timeout_s:.0f}s") from exc
    if run.returncode != 0:
        tail = (run.stderr or run.stdout or "").strip()[-2000:]
        raise TrainingError(f"training exited {run.returncode}: {tail or 'no output'}")
    if not (output_dir / ADAPTER_MARKER).exists():
        raise TrainingError(
            f"training finished but left no {ADAPTER_MARKER} in {output_dir}"
        )
    holdout_ppl = None
    metrics_path = output_dir / METRICS_FILE
    if metrics_path.exists():
        try:
            value = json.loads(metrics_path.read_text(encoding="utf-8")).get(
                "holdout_ppl"
            )
            holdout_ppl = float(value) if value is not None else None
        except (ValueError, TypeError) as exc:
            raise TrainingError(f"unreadable {METRICS_FILE}: {exc}") from exc
    return TrainedAdapter(adapter_dir=output_dir, holdout_ppl=holdout_ppl)


class SubprocessTrainer:
    """Runs the configured training command with ``{config}`` substituted.

    Default command is the pinned in-repo script; a self-hosted operator can
    point at anything that honors the same config/output contract."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        timeout_s: float = 4 * 3600.0,
    ):
        self._command = command or [
            sys.executable,
            "-m",
            "oolu.representative.trainer.run_sft",
            "--config",
            "{config}",
        ]
        self._timeout_s = timeout_s

    def train(self, config: SftConfig) -> TrainedAdapter:
        return run_training_command(self._command, config, timeout_s=self._timeout_s)
