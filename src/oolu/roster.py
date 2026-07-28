"""The agent roster — who is listed below OoLu, and how a roster turn speaks.

A0 of the agents-expansion plan (docs/agents-expansion-plan.md): OoLu stays
the general assistant at the head of the sidebar; below it the app lists
named agents — News, Poll, Explorer, Travel Plan — each a real conversation
with its own durable thread (``social.AssistantHistoryStore`` tags turns by
agent) and its own registered model seat (``seats.py``), never a costume on
the OoLu chat.

An :class:`AgentCard` is deliberately small: identity, the honest one-line
scope of what the agent can do TODAY, and what a later phase brings. The
card IS the truth an A0 agent may speak from — the turn's system frame is
built from the card, so an agent cannot claim abilities its phase has not
landed. The roster turn itself is lean on purpose: one system frame, the
client-held history, one reply. No tools, no task lane, no growth offers —
those are OoLu's; a roster agent that needs hands earns them in its own
phase, through its own seat.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chat import ModelBudgetExceeded, ModelUnavailable, _split_reasoning


@dataclass(frozen=True)
class AgentCard:
    """One roster agent's standing identity — the sidebar row and the
    boundary of what its conversation may claim."""

    agent_id: str  # the thread tag and the door's name, e.g. "news"
    name: str  # what the sidebar shows
    tagline: str  # the sidebar's one-line sub
    scope: str  # the honest sentence: what this agent does TODAY
    ahead: str  # what later phases bring — named, so honesty has words
    seat: str  # the seats.py purpose its model consultations ride under


# The list below OoLu. OoLu itself is not a roster entry: it heads the
# sidebar with the full assistant surface; these are its named colleagues.
ROSTER: tuple[AgentCard, ...] = (
    AgentCard(
        agent_id="news",
        name="News",
        tagline="Stories from members, to magazine standard",
        scope=(
            "Drop whatever you have straight into this thread — a "
            "written piece, a photo, a clip, a song — and I review it "
            "with you: I may ask one question, then offer to publish it "
            "under the members license, and only your plain yes "
            "publishes. Members' pieces compose into attributed "
            "stories — every piece credited by name and photo. Read "
            "your edition here and set a morning delivery."
        ),
        ahead=(
            "Affiliate advertising with revenue shared back to "
            "contributors arrives in phases A4–A5 of the "
            "agents-expansion plan."
        ),
        seat="news.compose",
    ),
    AgentCard(
        agent_id="poll",
        name="Poll",
        tagline="Two things, compared — for fun, and for real",
        scope=(
            "I offer pairs of member pieces to compare — vote first, "
            "then see the real numbers (once enough members voted). "
            "Say “poll” for a pair, “genres” to pick a stream, or name "
            "a genre. I am also the floor's social scientist: pairs are "
            "designed to test standing hypotheses about how this "
            "community reads, and when a pattern holds — or a genuine "
            "debate opens — I report the field note here. Ask "
            "“findings” any time. With personalization on, your votes "
            "also shape your News edition."
        ),
        ahead=(
            "Affiliate advertising with revenue shared back to "
            "contributors arrives in phases A4–A5 of the "
            "agents-expansion plan."
        ),
        seat="poll.pair",
    ),
    AgentCard(
        agent_id="explorer",
        name="Explorer",
        tagline="Compare, then the best buy",
        scope=(
            "I compare real listings side by side — price, the discount "
            "fact, verified-buyer feedback, the order-book trust score, "
            "member lab results — and crown a best buy per mode, every "
            "factor shown. Follow an interest and the brief arrives here "
            "daily; buying walks the standing approval path."
        ),
        ahead=(
            "Travel planning over your calendar and group arrives in "
            "phase A7 of the agents-expansion plan."
        ),
        seat="explore.brief",
    ),
    AgentCard(
        agent_id="market",
        name="Market",
        tagline="Buy, sell, and be found — under real approvals",
        scope=(
            "The whole marketplace lives in this thread as form blocks: "
            "shop the shelf, open requests for quotes, decide approvals, "
            "drive your orders, and sell — listings carry photos, clips, "
            "and sound. I watch where your position meets the market's "
            "demand and brief you here; say “list” any time for "
            "everything you've created on the platform."
        ),
        ahead=(
            "Financing and insurance partner products ride the same "
            "typed offers; cross-market federation already answers "
            "shop searches."
        ),
        seat="market.desk",
    ),
    AgentCard(
        agent_id="travel",
        name="Travel Plan",
        tagline="Trips under real constraints",
        scope=(
            "I plan trips under real constraints: your calendar, the "
            "group's consented free-busy (busy/free only, never event "
            "contents), and the budget. Feasible packages rank with "
            "every factor shown; broken constraints are named, never "
            "buried. Booking walks the standing approval path, and a "
            "confirmed trip lands on your calendar."
        ),
        ahead=(
            "Multi-leg itineraries with inter-leg travel time are the "
            "named next step; the roster's phases A0–A7 are complete."
        ),
        seat="travel.plan",
    ),
)

_BY_ID = {card.agent_id: card for card in ROSTER}


def agent_card(agent_id: str) -> AgentCard | None:
    """The card for an id, or None — the caller decides how to refuse."""
    return _BY_ID.get(str(agent_id or "").strip())


def roster_items() -> list[dict]:
    """The roster door's payload: what the sidebar renders, in order."""
    return [
        {
            "agent_id": card.agent_id,
            "name": card.name,
            "tagline": card.tagline,
            "scope": card.scope,
            "ahead": card.ahead,
            "seat": card.seat,
        }
        for card in ROSTER
    ]


# --------------------------------------------------------------------------- #
# The roster turn: the card speaks, the model fills in the words.              #
# --------------------------------------------------------------------------- #
_SYSTEM_FRAME = """\
You are {name}, one of OoLu's named agents, in your own conversation
thread. You are not OoLu: you have no file access, no tasks, no runs, no
tools — this thread is words only.

What you can do today: {scope}
What comes later: {ahead}

Hard rules, no exceptions:
- Never claim to have collected, composed, published, ranked, booked, or
  measured anything — none of that machinery exists for you yet. When
  asked for it, say plainly what you can do today and what phase brings
  the rest.
- Never invent member content, statistics, poll results, prices,
  discounts, reviews, trust scores, or schedules. A number you were not
  given is a number you do not have.
- You may discuss, explain, and help the user think through the subject
  — that is your whole job today. Be genuinely useful within it.
- Answer in the user's language, briefly and warmly."""


def agent_system_prompt(card: AgentCard) -> str:
    """The one system frame a roster turn stands in — built from the card,
    so the agent's claims cannot outrun its phase."""
    return _SYSTEM_FRAME.format(name=card.name, scope=card.scope, ahead=card.ahead)


def _card_answer(card: AgentCard) -> str:
    """The model-less (or model-down) reply: the card, spoken plainly —
    a dead model must never mean a dead conversation."""
    return f"{card.scope} {card.ahead}"


def agent_turn(
    card: AgentCard,
    message: str,
    *,
    history: list[dict] | None = None,
    model=None,  # chat.ChatModel | None — the seat's router
    context: str | None = None,  # an extra system note (e.g. the clock)
) -> tuple[str, str | None, str]:
    """One roster turn: ``(say, reasoning, source)``.

    ``source`` is "model" when the seated model answered, "card" when the
    reply is the card's own honest scope (no model, or the model is down).
    """
    if model is None:
        return _card_answer(card), None, "card"
    messages: list[dict] = [
        {"role": "system", "content": agent_system_prompt(card)}
    ]
    if context:
        messages.append({"role": "system", "content": context})
    for entry in history or []:
        role = entry.get("role")
        content = entry.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    try:
        raw = model.reply(messages)
    except ModelBudgetExceeded as exc:
        # The cap speaks out loud — a refusal in words, never a silent skip.
        return str(exc), None, "model"
    except ModelUnavailable:
        return _card_answer(card), None, "card"
    say, reasoning = _split_reasoning(raw)
    return (say.strip() or _card_answer(card)), reasoning, "model"
