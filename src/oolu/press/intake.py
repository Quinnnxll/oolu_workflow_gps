"""The News desk's intake — raw material becomes a piece through talk.

The growth-trigger law (``chat.py``'s n8n lesson), applied to
publishing: nobody fills a form. A member drops whatever they have into
the News thread — an article-shaped text, a photo, a clip, a song — and
the DESK detects it, the way OoLu spots a buildable chore. The desk
then reviews in conversation: when something is missing it asks ONE
question (what happened, where, when — the reporter's basics), folds
the answer into the draft, and finally makes a standing publish offer
that names exactly what will stand: the title, the shelf, the license
terms, the attachments. The member's plain "yes" on the very next
message IS the consent — the same one-message consent shape the growth
offers use, and the offer RENDERED the license terms, so the consent is
attached to the words it answers. Anything that is not a yes or a no
withdraws the offer: consent detached from the question it answered is
not consent.

Whether the published piece is WORTH composing is not the desk's mood:
the rubric decides (``standards.py``), exactly as it does for every
piece on the shelf — the desk's honest job ends at a clean publication.
What the desk does judge, loudly and before any offer:

- **Leaks.** Text that would need scrubbing is refused with each leak
  named — the author fixes the words; the desk never rewrites them.
- **Thinness.** A piece too short to inform earns one gathering
  question, not a refusal. After the member answers once, the desk
  offers what stands — one question per piece, never an interrogation.
- **Retelling.** A near-duplicate is offered honestly: the offer names
  the credit the neighbor check will record.

Deterministic by design: detection, titles, genres, and every desk
sentence are plain functions of the material — the same words in, the
same review out, model or no model. The seat's model keeps the ordinary
News conversation; the intake is the desk's own hand.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict

from ..nodeplace.plagiarism import similarity
from .contributions import (
    LICENSES,
    MAX_TITLE_CHARS,
    SIMILARITY_FLAG,
    leak_report,
)
from .taxonomy import GENRES

# Text alone at least this long reads like material; shorter is
# conversation. Attached media is material at any length — a photo IS
# the piece, the words can follow.
MATERIAL_FLOOR_CHARS = 160
# Under this the desk asks its one gathering question before offering.
GATHER_FLOOR_CHARS = 280
# One gathering question per piece — a desk, not an interrogation.
MAX_GATHER_ROUNDS = 1

INTAKE_LICENSE = next(iter(LICENSES))


class IntakeDraft(BaseModel):
    """The piece under review — one per member, durable, replaceable."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    principal: str
    stage: str  # "gathering" (a question stands) | "offered" (yes/no stands)
    title: str
    body: str
    genres: tuple[str, ...]
    file_ids: tuple[str, ...] = ()
    asked: int = 0  # gathering rounds spent
    updated_at: datetime | None = None


_SCHEMA = """CREATE TABLE IF NOT EXISTS press_intake (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    stage TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    genres TEXT NOT NULL,
    file_ids TEXT NOT NULL,
    asked INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, principal)
)"""


class IntakeStore:
    """One standing draft per member — the GrowthOfferStore discipline:
    a new draft replaces the old (the newest material is the one on the
    table), and ``pop`` reads-and-deletes atomically so a consent is
    spent exactly once."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def put(self, draft: IntakeDraft) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_intake
                     (tenant_id, principal, stage, title, body, genres,
                      file_ids, asked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, principal) DO UPDATE SET
                     stage = excluded.stage,
                     title = excluded.title,
                     body = excluded.body,
                     genres = excluded.genres,
                     file_ids = excluded.file_ids,
                     asked = excluded.asked,
                     updated_at = excluded.updated_at""",
                (
                    draft.tenant_id,
                    draft.principal,
                    draft.stage,
                    draft.title,
                    draft.body,
                    json.dumps(list(draft.genres)),
                    json.dumps(list(draft.file_ids)),
                    draft.asked,
                    self._clock().isoformat(),
                ),
            )

    def get(self, *, tenant: str, principal: str) -> IntakeDraft | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM press_intake"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        return self._draft(row) if row is not None else None

    def pop(self, *, tenant: str, principal: str) -> IntakeDraft | None:
        """Take the draft off the table — read and delete in ONE
        transaction, so an answer is spent exactly once."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM press_intake"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "DELETE FROM press_intake"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
            return self._draft(row)

    @staticmethod
    def _draft(row) -> IntakeDraft:
        return IntakeDraft(
            tenant_id=row["tenant_id"],
            principal=row["principal"],
            stage=row["stage"],
            title=row["title"],
            body=row["body"],
            genres=tuple(json.loads(row["genres"])),
            file_ids=tuple(json.loads(row["file_ids"])),
            asked=int(row["asked"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


# --------------------------------------------------------------------------- #
# Detection: the desk spots material the way OoLu spots a chore.               #
# --------------------------------------------------------------------------- #
def looks_like_material(message: str, *, has_media: bool = False) -> bool:
    """Does this message read like a piece, not a chat line?

    Attached media is material at any length. Text alone must carry
    enough words to stand as one (the floor), and a message that ends
    on a question mark is a question for the agent, however long — the
    conversation stays a conversation.
    """
    if has_media:
        return True
    text = str(message or "").strip()
    if len(text) < MATERIAL_FLOOR_CHARS:
        return False
    return not text.endswith("?")


_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def derive_title(text: str) -> str:
    """The piece's own first words as its title: the first line when it
    fits, else the first sentence, else a clean word-boundary cut."""
    first = next(
        (line.strip() for line in str(text or "").splitlines() if line.strip()),
        "",
    )
    if len(first) <= MAX_TITLE_CHARS:
        return first
    sentence = _SENTENCE_END.split(first, maxsplit=1)[0].strip()
    if len(sentence) <= MAX_TITLE_CHARS:
        return sentence
    cut = sentence[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


# What each genre sounds like in a member's raw words — a small honest
# keyword floor, never a model's guess. "local" is the default: a
# member's raw report is usually about where they live.
_GENRE_HINTS: dict[str, tuple[str, ...]] = {
    "world": ("country", "government", "election", "nation", "border"),
    "technology": ("software", "computer", "machine", "robot", "code", "device"),
    "science": ("experiment", "study", "research", "storm", "climate", "species"),
    "business": ("market", "company", "price", "trade", "shop", "sale"),
    "culture": ("music", "song", "album", "film", "book", "gallery", "festival"),
    "sport": ("match", "game", "team", "race", "league", "training"),
    "food": ("food", "cook", "recipe", "restaurant", "harvest", "meal"),
    "travel": ("trip", "journey", "flight", "beach", "mountain", "visited"),
    "health": ("health", "clinic", "doctor", "sleep", "exercise", "recovery"),
    "products": ("bought", "purchase", "review", "product", "unboxed"),
    "results": ("benchmark", "measured", "results", "numbers", "dataset"),
}


def infer_genres(text: str, *, media_types: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The shelf the material sounds like, from its own words — at most
    two keys, "local" when nothing else speaks. A sound attachment hints
    culture: a song is culture before it is anything else."""
    lowered = f" {str(text or '').lower()} "
    hits = [
        key
        for key, hints in _GENRE_HINTS.items()
        if key in GENRES and any(f" {hint}" in lowered for hint in hints)
    ]
    if any(m.startswith("audio/") for m in media_types) and "culture" not in hits:
        hits.insert(0, "culture")
    return tuple(hits[:2]) if hits else ("local",)


# --------------------------------------------------------------------------- #
# The desk's words: every sentence a plain function of the material.           #
# --------------------------------------------------------------------------- #
GATHER_ASK = (
    "This looks like a piece for the magazine. Before I set it up: "
    "tell me a bit more — what happened, where, and when? A line or two "
    "is plenty. (Or say “no” and it stays our chat.)"
)

GATHER_ASK_WORDS = (
    "I have the attachment — a piece still needs words. Tell me what "
    "we're looking at: what happened, where, and when? (Or say “no” "
    "and it stays our chat.)"
)

LEAK_REFUSAL = (
    "I can't set this up as it stands — publishing it would leak {leaks}. "
    "Remove that from the text and send it again; I never rewrite your "
    "words."
)

DROPPED = "Okay — it stays our chat. Send it again any time."

WITHDRAWN_NOTE = (
    "(I've set the draft aside — send the material again when you want "
    "it published.)"
)


def offer_text(
    draft: IntakeDraft, *, credit_title: str | None = None
) -> str:
    """The standing publish offer — everything that will stand, in the
    server's own words: title, shelf, attachments, the LICENSE TERMS
    (the yes that answers this offer is the consent, so the offer must
    render what is being consented to), and the retelling credit when
    the neighbor check will record one."""
    license = LICENSES[INTAKE_LICENSE]
    genre_labels = ", ".join(
        GENRES[key].label if key in GENRES else key for key in draft.genres
    )
    lines = [
        f"Ready to publish: “{draft.title}” — filed under {genre_labels}."
    ]
    if draft.file_ids:
        n = len(draft.file_ids)
        lines.append(f"With {n} attachment{'s' if n != 1 else ''}.")
    if credit_title:
        lines.append(
            f"It retells an earlier piece (“{credit_title}”) — the "
            "original will be credited."
        )
    lines.append(f"License — {license.name}: {license.terms}")
    lines.append(
        "Say “yes” to publish under these terms; say “no” and it stays "
        "our chat."
    )
    return "\n".join(lines)


def nearest_live(
    text: str, live_texts: list[tuple[str, str]]
) -> tuple[str | None, float]:
    """The neighbor check's preview: (contribution_id, similarity) of
    the closest live piece at or above the flag, else (None, 0.0) — so
    the offer can name the credit before the gate records it."""
    best_id, best = None, 0.0
    for other_id, other_text in live_texts:
        score = similarity(text, other_text)
        if score > best:
            best_id, best = other_id, score
    if best < SIMILARITY_FLAG:
        return None, 0.0
    return best_id, best


def review(
    draft: IntakeDraft,
    *,
    live_texts: list[tuple[str, str]],
    title_of: Callable[[str], str | None] = lambda _cid: None,
) -> tuple[IntakeDraft, str]:
    """One review pass: ``(draft as it now stands, the desk's words)``.

    The draft comes back staged — "gathering" when the desk asked its
    question, "offered" when the publish offer is on the table, and
    "dropped" when the text would leak: the member resends FIXED words
    (a fresh draft), never words folded onto the leaky ones.
    """
    leaks = leak_report(f"{draft.title}\n{draft.body}")
    if leaks:
        staged = draft.model_copy(update={"stage": "dropped"})
        return staged, LEAK_REFUSAL.format(leaks=", ".join(leaks))
    if not draft.body.strip():
        staged = draft.model_copy(
            update={"stage": "gathering", "asked": draft.asked + 1}
        )
        return staged, GATHER_ASK_WORDS
    if (
        len(draft.body) < GATHER_FLOOR_CHARS
        and draft.asked < MAX_GATHER_ROUNDS
    ):
        staged = draft.model_copy(
            update={"stage": "gathering", "asked": draft.asked + 1}
        )
        return staged, GATHER_ASK
    credit_id, _ = nearest_live(
        f"{draft.title}\n{draft.body}", live_texts
    )
    credit_title = title_of(credit_id) if credit_id else None
    staged = draft.model_copy(update={"stage": "offered"})
    return staged, offer_text(staged, credit_title=credit_title)


def fold_answer(draft: IntakeDraft, answer: str) -> IntakeDraft:
    """The member's gathering answer joins the piece: appended to the
    body (their words, verbatim), the title derived once real words
    exist, the genres re-read over the grown text."""
    body = f"{draft.body}\n\n{answer.strip()}".strip()
    title = draft.title or derive_title(body)
    return draft.model_copy(
        update={
            "body": body,
            "title": title,
            "genres": infer_genres(f"{title}\n{body}"),
        }
    )


def draft_from_material(
    *,
    tenant: str,
    principal: str,
    message: str,
    file_ids: tuple[str, ...] = (),
    media_types: tuple[str, ...] = (),
) -> IntakeDraft:
    """A fresh draft from raw material: the message's own words become
    title and body; the shelf is read from those words (and the sound
    of the attachments)."""
    text = str(message or "").strip()
    title = derive_title(text)
    # The title is the first line; the body keeps EVERYTHING (a
    # one-line piece is its own title and body — the gate needs both).
    return IntakeDraft(
        tenant_id=tenant,
        principal=principal,
        stage="gathering",
        title=title,
        body=text,
        genres=infer_genres(text, media_types=media_types),
        file_ids=tuple(file_ids),
    )
