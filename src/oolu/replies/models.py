"""Shared models for deterministic reply routing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageEnvelope(BaseModel):
    """Small channel-neutral representation of an inbound text message."""

    model_config = ConfigDict(frozen=True)

    channel: str
    conversation_id: str
    sender_id: str
    text: str
    direction: Literal["inbound", "outbound"] = "inbound"
    message_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class ReplyRule(BaseModel):
    """An intentionally narrow rule; narrow matching prevents surprising sends."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    phrases: list[str] = Field(min_length=1)
    reply: str = Field(min_length=1)
    match: Literal["exact", "contains"] = "exact"
    channels: list[str] = Field(default_factory=list)
    requires: dict[str, str] = Field(default_factory=dict)


class ReplyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str | None = None
    source: Literal["learned", "rule", "fallback", "none"] = "none"
    rule_id: str | None = None


class ReplyConfig(BaseModel):
    """Local JSON configuration: trusted context plus ordered reply rules."""

    model_config = ConfigDict(frozen=True)

    context: dict[str, str] = Field(default_factory=dict)
    rules: list[ReplyRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "ReplyConfig":
        return cls.model_validate(
            json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        )
