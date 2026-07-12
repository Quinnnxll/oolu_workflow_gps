"""The preference pass (Phase 2): the user's edits become the reward.

Every draft the user rewrote before sending is a labeled preference —
chosen (their words) over rejected (the model's). Once enough pairs exist,
a short DPO run stacks on the fresh SFT adapter: same subprocess contract
as SFT, same output-dir handshake, no torch in this process.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .sft import TrainedAdapter, run_training_command


@dataclass(frozen=True)
class DpoConfig:
    """Everything one preference run needs, JSON-serializable.

    ``adapter_dir`` is the SFT adapter the pass starts from — DPO tunes the
    voice's judgment, it never replaces the voice."""

    base_model: str
    adapter_dir: str
    pairs_path: str
    output_dir: str
    epochs: int = 1
    learning_rate: float = 5e-6
    beta: float = 0.1
    max_seq_len: int = 1024

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "DpoConfig":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


@runtime_checkable
class PreferenceTrainer(Protocol):
    """Port for whatever turns edit pairs into a better adapter."""

    def tune(self, config: DpoConfig) -> TrainedAdapter: ...


class SubprocessPreferenceTrainer:
    """The DPO twin of SubprocessTrainer — same contract, pinned script."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        timeout_s: float = 2 * 3600.0,
    ):
        self._command = command or [
            sys.executable,
            "-m",
            "oolu.representative.trainer.run_dpo",
            "--config",
            "{config}",
        ]
        self._timeout_s = timeout_s

    def tune(self, config: DpoConfig) -> TrainedAdapter:
        return run_training_command(self._command, config, timeout_s=self._timeout_s)
