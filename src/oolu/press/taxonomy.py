"""The genre taxonomy — one typed, versioned vocabulary for everything.

The listing-tag lesson (``nodeplace/economics.py``'s ``class:``/``market:``
keys) applied to content: contributions carry genre keys from ONE
registry, and the same keys later classify poll genres (A3) and campaign
targeting (A4) — so "fairly matched" is a join on a shared vocabulary,
never a fuzzy guess across two word lists.

The taxonomy is versioned: a contribution records the version it was
keyed under, so a future re-cut of the genres never silently reclassifies
standing records — they say which law they were filed under.
"""

from __future__ import annotations

from dataclasses import dataclass

TAXONOMY_VERSION = 1


@dataclass(frozen=True)
class Genre:
    key: str  # the stable join key, e.g. "technology"
    label: str  # what the picker shows
    description: str  # what belongs here — the contributor's guide


# Version 1: broad enough for a general member magazine, with the two
# commerce-adjacent genres (products, results) that seed Explorer's
# evidence in A6. Append-only within a version; a re-cut bumps the
# version.
_GENRES: tuple[Genre, ...] = (
    Genre("local", "Around me", "What is happening where you live and work."),
    Genre("world", "The world", "Events beyond your neighborhood."),
    Genre("technology", "Technology", "Tools, software, machines, and how they change work."),
    Genre("science", "Science", "Findings, experiments, and how we know things."),
    Genre("business", "Business", "Markets, companies, money, and making a living."),
    Genre("culture", "Culture", "Art, books, music, film, and the life around them."),
    Genre("sport", "Sport", "Games, matches, training, and the people who play."),
    Genre("food", "Food", "Cooking, eating, growing, and where food comes from."),
    Genre("travel", "Travel", "Places, journeys, and what they are really like."),
    Genre("health", "Health", "Bodies, minds, care, and staying well."),
    Genre("products", "Products", "Real experiences with things you bought and used."),
    Genre("results", "Lab & tests", "Measured results: benchmarks, tests, and the numbers behind them."),
)

GENRES: dict[str, Genre] = {genre.key: genre for genre in _GENRES}


def taxonomy_items() -> list[dict]:
    """The genres door's payload: the picker renders exactly this."""
    return [
        {
            "key": genre.key,
            "label": genre.label,
            "description": genre.description,
        }
        for genre in _GENRES
    ]
