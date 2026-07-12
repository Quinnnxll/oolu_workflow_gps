"""Shared models for representative mode — the user's own voice, drafted.

A representative never speaks first and (in Phase 0) never speaks last:
it drafts a reply in the user's voice, and the user decides. Every model
here is frozen; state changes go through the store.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# "auto" can be switched on any time, but autonomy is EARNED per message:
# an auto-send needs the gate (grounded, no commitment) AND a proven
# accept-rate record — until then "auto" behaves exactly like "draft".
RepresentativeMode = Literal["off", "draft", "auto"]
MODES: tuple[str, ...] = ("off", "draft", "auto")

# "auto_sent" is the representative speaking for the user; the four human
# statuses are the user speaking about the representative — only those
# count toward the accept-rate that earns autonomy.
DraftStatus = Literal["pending", "sent", "edited", "discarded", "auto_sent"]

# A short standing self-description, not a biography.
MAX_ABOUT_CHARS = 2_000


class PersonaCard(BaseModel):
    """Who the representative writes as: a name plus standing facts."""

    model_config = ConfigDict(frozen=True)

    display_name: str
    about: str = ""


class RecallHit(BaseModel):
    """One remembered exchange, scored against the message being answered."""

    model_config = ConfigDict(frozen=True)

    prompt_text: str
    reply_text: str
    score: float


class GateVerdict(BaseModel):
    """The confidence gate's reading of one generated reply.

    In Phase 0 the verdict only annotates drafts — nothing auto-sends.
    ``auto_ok`` is recorded anyway so Phase 2 can be tuned against real
    history instead of guesses.
    """

    model_config = ConfigDict(frozen=True)

    score: float = 0.0
    commitment: bool = False
    auto_ok: bool = False
    reasons: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    """One generated reply awaiting (or past) the user's decision.

    ``scope`` is the owning account (tenant:principal); ``conversation_id``
    is where the reply would go (a friend's username, a channel thread).
    The (generated_text, final_text, status) triple is the flywheel: sent
    unedited is a positive example, edited is a preference pair, discarded
    is gate feedback.
    """

    model_config = ConfigDict(frozen=True)

    draft_id: str
    scope: str
    conversation_id: str
    inbound_text: str
    generated_text: str
    status: DraftStatus = "pending"
    final_text: str | None = None
    gate: GateVerdict = Field(default_factory=GateVerdict)
    adapter_version: str = "base"
    created_at: float
    decided_at: float | None = None
