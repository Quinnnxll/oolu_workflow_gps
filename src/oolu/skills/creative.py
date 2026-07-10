"""Creative-app learning: the SOURCE FILE is the lesson, never the replay.

When the user works in a third-party creative application — Photoshop,
SolidWorks, Blender and their kin — what OoLu should learn from is the
SOURCE FILE (.psd, .sldprt, .blend): the artifact that actually encodes
the creative decisions, fetched first and kept for later model training.
Screenshots and mouse/keyboard traces help understand the user's PATH
efficiently — which panels, which order, where the time went — but a
pixel-and-click recording will NEVER execute the work reliably, so it is
captured as ADVISORY context only and is refused as a replayable skill by
construction.
"""

from __future__ import annotations

from pathlib import PurePath

from pydantic import BaseModel, ConfigDict, Field

# The registry of known creative applications and the source-file
# extensions that carry their real state. Matching is by substring on the
# demonstration's application name, case-insensitive.
CREATIVE_APPS: dict[str, frozenset[str]] = {
    "photoshop": frozenset({".psd", ".psb"}),
    "illustrator": frozenset({".ai", ".eps"}),
    "gimp": frozenset({".xcf"}),
    "solidworks": frozenset({".sldprt", ".sldasm", ".slddrw"}),
    "fusion": frozenset({".f3d", ".f3z"}),
    "autocad": frozenset({".dwg", ".dxf"}),
    "blender": frozenset({".blend"}),
    "figma": frozenset({".fig"}),
    "premiere": frozenset({".prproj"}),
    "after effects": frozenset({".aep", ".aepx"}),
}


def creative_app(application: str | None) -> str | None:
    """The matched creative-app key, or None for ordinary applications."""
    name = (application or "").casefold()
    for key in CREATIVE_APPS:
        if key in name:
            return key
    return None


def is_creative_app(application: str | None) -> bool:
    return creative_app(application) is not None


def source_extensions(application: str | None) -> frozenset[str]:
    key = creative_app(application)
    return CREATIVE_APPS[key] if key else frozenset()


class CreativeCapture(BaseModel):
    """What a creative-app session yields, in priority order.

    ``source_files`` are the training payload — fetch these FIRST.
    ``advisory_trace`` (screenshots, input events) explains the user's
    path; it is context, never an executable. ``replayable`` is a
    constant False: no flag anywhere can promote a pixel trace into
    execution.
    """

    model_config = ConfigDict(frozen=True)

    application: str
    source_files: list[str] = Field(default_factory=list)
    other_files: list[str] = Field(default_factory=list)
    advisory_trace: list[str] = Field(default_factory=list)
    replayable: bool = False


def plan_creative_capture(
    application: str,
    *,
    files: list[str] | None = None,
    trace: list[str] | None = None,
) -> CreativeCapture:
    """Sort a session's artifacts into the capture priority: the app's
    source files first (the lesson), everything else after, the
    screen/input trace as advisory context."""
    wanted = source_extensions(application)
    sources, others = [], []
    for path in files or []:
        suffix = PurePath(path).suffix.casefold()
        (sources if suffix in wanted else others).append(path)
    return CreativeCapture(
        application=application,
        source_files=sources,
        other_files=others,
        advisory_trace=list(trace or []),
    )


def refuse_replay_reason(application: str | None) -> str | None:
    """Why a creative-app demonstration must not compile into a replayable
    skill — or None when the application is not creative."""
    key = creative_app(application)
    if key is None:
        return None
    wanted = ", ".join(sorted(CREATIVE_APPS[key]))
    return (
        f"{key} work is learned from its SOURCE FILES ({wanted}) — fetch "
        "those for model training. Screen and input recordings explain "
        "the user's path but will never execute the work reliably, so "
        "they are kept as advisory context, not compiled into a skill."
    )
