"""The representative's system prompt: a persona card, real examples, rules.

Style rides in the examples (and, from Phase 1, in the adapter); knowledge
rides in the retrieved exchanges. The prompt asks for a reply, not a
conversation — the model's raw text IS the draft.
"""

from __future__ import annotations

from .models import PersonaCard, RecallHit

HARD_RULES = """\
Hard rules, always:
- Never agree to spend money, schedule anything, or make a promise on
  {name}'s behalf. If the message asks for one of those, draft a holding
  reply in {name}'s voice that defers the decision to them.
- Never invent facts, opinions, or plans {name} has not expressed.
- The draft is addressed to the PEER, never to {name}. Never ask {name}
  questions inside the reply, never request context there, never explain
  yourself, never write as an assistant — the peer would receive it.
- When the reply genuinely cannot be written without something only
  {name} knows (which event is meant, what their plans are, what they
  want said), do NOT guess and do NOT draft around it: answer with
  exactly one line starting with NEED_INFO: followed by your specific
  questions for {name}. Nothing is sent — {name} is asked separately, in
  their own assistant conversation. If a short holding reply in {name}'s
  voice honestly covers it ("let me check and get back to you"), that
  draft is better than NEED_INFO.
- Mirror {name}'s usual length and register — a short message earns a
  short reply.
- Output ONLY the reply text (or the single NEED_INFO: line): no
  preamble, no quotes, no explanations."""


def build_system_prompt(
    card: PersonaCard,
    hits: list[RecallHit],
    *,
    peer: str | None = None,
    units_note: str | None = None,
    info_note: str | None = None,
) -> str:
    """The register rides the prompt: WHO the reply addresses is named, and
    same-peer examples are labeled as such — how {name} talks TO this
    person outranks how they talk in general.

    ``units_note`` is the account's measurement-units directive (the same
    one the chat assistant gets), so a drafted reply expresses measurements
    the way {name} prefers — honoured under, never over, the voice examples.
    """
    name = card.display_name
    lines = [
        f"You are drafting a message reply AS {name} — in their voice, not"
        f" as an assistant. {name} reviews every draft before anything is"
        " sent; your job is to write exactly what they would write.",
    ]
    if peer:
        lines.append(
            f"The reply is TO {peer}. Match the tone, length, and formality"
            f" {name} uses with {peer} specifically — people don't talk to"
            " their boss and their brother the same way."
        )
    if card.about.strip():
        lines.append(f"About {name}: {card.about.strip()}")
    if hits:
        lines.append(
            f"Here is how {name} has actually replied before — match this voice:"
        )
        for hit in hits:
            who = hit.peer if peer and hit.peer == peer else "someone"
            lines.append(f'- When {who} said: "{hit.prompt_text}"')
            lines.append(f'  {name} replied: "{hit.reply_text}"')
    if units_note:
        lines.append(units_note)
    if info_note:
        # The user's own answer to an earlier NEED_INFO — the missing
        # facts, straight from {name}, for THIS reply only.
        lines.append(
            f"{name} has just told you, for this reply: {info_note.strip()}"
            " — use it; do not ask again."
        )
    lines.append(HARD_RULES.format(name=name))
    return "\n".join(lines)


def build_messages(
    card: PersonaCard,
    hits: list[RecallHit],
    inbound_text: str,
    *,
    history: list[dict] | None = None,
    peer: str | None = None,
    units_note: str | None = None,
    info_note: str | None = None,
) -> list[dict]:
    """The OpenAI-style message list a ChatModel takes.

    ``history`` is the recent thread, already role-mapped by the caller:
    the peer's messages as "user", the account's own as "assistant" — the
    model continues the user's side of the conversation.
    """
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(
                card, hits, peer=peer, units_note=units_note, info_note=info_note
            ),
        }
    ]
    for entry in history or []:
        role, content = entry.get("role"), entry.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": inbound_text})
    return messages
