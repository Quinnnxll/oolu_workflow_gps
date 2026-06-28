"""The small adapter contract LINE and other channels can implement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import MessageEnvelope


class ChannelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PollBatch:
    messages: tuple[MessageEnvelope, ...]
    next_offset: int | None


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str

    def poll(self, *, offset: int | None = None, timeout_s: int = 25) -> PollBatch: ...

    def send(self, message: MessageEnvelope, text: str) -> None: ...
