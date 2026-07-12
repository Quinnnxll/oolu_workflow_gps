"""The adapter pipeline (Phase 1): jobs, the worker, and the SFT runner.

The heavy lifting (torch/transformers/peft/trl) lives behind the
``oolu[representative-train]`` extra and a subprocess boundary — the base
install drafts and serves without ever importing it.
"""

from .dpo import DpoConfig, PreferenceTrainer, SubprocessPreferenceTrainer
from .jobs import TRAIN_TASK_KIND, refresh_reason, sweep
from .sft import SftConfig, SubprocessTrainer, TrainedAdapter, Trainer, TrainingError
from .worker import TrainerWorker

__all__ = [
    "DpoConfig",
    "PreferenceTrainer",
    "SftConfig",
    "SubprocessPreferenceTrainer",
    "SubprocessTrainer",
    "TRAIN_TASK_KIND",
    "TrainedAdapter",
    "Trainer",
    "TrainerWorker",
    "TrainingError",
    "refresh_reason",
    "sweep",
]
