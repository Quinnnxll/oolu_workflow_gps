"""Model seats — one discipline for every model consultation.

The platform consults models from many places: the conversation, the
node-function author, the script synthesizer and repairer, the planning
intaker and router, the post-retry rebuilder, the representative. Each
call site grew its own prompt (fine — the words belong to the work) but
also its own, implicit answers to four questions that should never be
implicit:

1. **What files may this call touch?** (file access)
2. **What tools does it hold?** (hands)
3. **What is it responsible for producing?** (the charge)
4. **Whose consent covers it, who meters it, who audits it?** (governance)

A **seat** answers those four questions once, in one registry — and the
answers hold whatever model sits down. Swap Anthropic for OpenAI for a
local model mid-conversation and the seat does not move: same drawer
scope, same hands, same charge, same consent switch, same purpose the
meter and the audit log book it under. The model is a visitor; the seat
is the office.

What is enforced in code today:

- ``DeskFiles`` is the one file hand a seated call writes through: it is
  bound to one node's drawer and refuses any path outside the seat's
  declared scopes — an author seat can write ``src/``, and nothing else,
  no matter what the model asks for.
- A seat with a ``consent_key`` refuses to open without the caller's
  attestation that the consent door was passed (the switch itself lives
  in settings; each door checks it and attests here, so the requirement
  is visible at the seat, not scattered).
- Every write through a seat is recorded and belongs in the audit log
  (the gateway appends a ``model.seat`` event naming the purpose, the
  node, and the files written).

The purposes are the SAME strings the model router meters under and the
usage books aggregate by — one vocabulary end to end. See
``docs/model-seats.md`` for the full architecture and the migration map
for call sites not yet seated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


class SeatViolation(PermissionError):
    """A seated call reached outside its seat — refused, never normalized."""


@dataclass(frozen=True)
class Seat:
    """One model call site's standing terms, model-independent."""

    purpose: str  # the meter/audit/usage key, e.g. "node.build"
    charge: str  # the one-sentence responsibility
    reads: tuple[str, ...] = ()  # drawer path prefixes it may read
    writes: tuple[str, ...] = ()  # drawer path prefixes it may write
    hands: tuple[str, ...] = ()  # tool names its call site may expose
    consent_key: str | None = None  # the settings switch that covers it
    audited: bool = True  # its acts belong in the audit log


# --------------------------------------------------------------------------- #
# The registry — the whole table of who sits where.                            #
# --------------------------------------------------------------------------- #
SEATS: dict[str, Seat] = {
    seat.purpose: seat
    for seat in (
        Seat(
            purpose="chat.turn",
            charge=(
                "converse with the user; name work as tasks; never claim "
                "acts (builds, sends, reminders) the platform did not do"
            ),
            hands=(
                "list_files",
                "read_file",
                "write_file",
                "list_runs",
                "run_log",
                "list_nodes",
                "get_settings",
                "set_setting",
                "send_message",
                "create_reminder",
                "list_reminders",
                "find_friend",
                "build_node",  # node windows only, consent-gated
            ),
            # The chat's file hands reach the LIFE drawer (or, inside a
            # node window, that node's drawer) through ChatTools — which
            # enforce the same tenant/owner walls as the routes.
            reads=("",),
            writes=("",),
            audited=False,  # conversation is private; RUNS it starts audit
        ),
        Seat(
            purpose="node.build",
            charge=(
                "judge a goal executable and write the node's execution "
                "function — one script, emit_result once, the web only "
                "through http_request"
            ),
            # The agentic author's hands (native tool-calling models):
            # read the desk's contracts and upstream outputs, verify,
            # and finish through the schema-checked door. A model
            # without tool-calling keeps the one-shot path, handless.
            hands=(
                "list_nodes",
                "read_node_output",
                "read_file",
                "verify_function",
                "finish_node",
                "decline",
            ),
            reads=("src/", "lessons/"),
            writes=("src/",),
            consent_key="account.autobuild_consent",
            audited=True,
        ),
        Seat(
            purpose="node.repair",
            charge=(
                "edit a node's failing function to close the exact "
                "reported gap; the edit is verified by execution before "
                "it is trusted"
            ),
            reads=("src/",),
            writes=("src/",),
            audited=True,
        ),
        Seat(
            purpose="plan.intake",
            charge="structure a request into a brief; invent nothing",
        ),
        Seat(
            purpose="plan.route",
            charge="choose among existing routes; never mint capabilities",
        ),
        Seat(
            purpose="plan.synthesize",
            charge=(
                "write a gap-filling script for one node goal; verified "
                "by execution before it is trusted or cached"
            ),
        ),
        Seat(
            purpose="plan.rebuild",
            charge=(
                "after retries run out, replan the route and write the "
                "missing code — under the auto-build consent"
            ),
            consent_key="account.autobuild_consent",
            audited=True,
        ),
        Seat(
            purpose="rep.draft",
            charge=(
                "draft a reply in the account's own voice; deliver "
                "nothing — the human's send is the send"
            ),
            audited=False,
        ),
        Seat(
            purpose="node.review",
            charge=(
                "judge a verified function before it is listed — contract "
                "fit, the exact-value rule, slot-vocabulary reuse; a block "
                "names its reason and publishes nothing"
            ),
            # Judgement only: it reads what the author wrote, holds no
            # tools, writes nothing — the reviewer that can edit is an
            # author with extra steps.
            reads=("src/",),
            audited=True,
        ),
    )
}


def _normal(path: str) -> PurePosixPath:
    pure = PurePosixPath(str(path).replace("\\", "/"))
    if pure.is_absolute() or any(part in ("..", "", ".") for part in pure.parts):
        raise SeatViolation(f"path escapes the drawer: {path!r}")
    return pure


def _in_scope(path: PurePosixPath, scopes: tuple[str, ...]) -> bool:
    text = str(path)
    return any(scope == "" or text.startswith(scope) for scope in scopes)


class DeskFiles:
    """One node's drawer, held through one seat — the uniform file hand.

    Whatever model occupies the seat, this is how its outputs become
    files: reads and writes are checked against the SEAT's declared
    scopes (never the model's ambitions), paths are drawer-relative and
    escape-proof, and every write is remembered so the caller can put
    the act on the audit log. A seat with a consent key will not even
    open without the caller's attestation that its consent door was
    passed.
    """

    def __init__(
        self,
        store,  # durable.UserFileStore
        *,
        tenant: str,
        node_id: str,
        seat: Seat,
        consented: bool = False,
    ) -> None:
        if seat.consent_key and not consented:
            raise SeatViolation(
                f"the {seat.purpose} seat sits behind the "
                f"'{seat.consent_key}' consent — the caller must pass its "
                "door and attest to it"
            )
        self._store = store
        self._tenant = tenant
        self._node_id = node_id
        self._seat = seat
        self.written: list[str] = []

    # ------------------------------------------------------------------ #
    def read(self, path: str) -> str | None:
        pure = _normal(path)
        if not _in_scope(pure, self._seat.reads):
            raise SeatViolation(
                f"the {self._seat.purpose} seat may not read {path!r}"
            )
        found = self._find(pure)
        return found.content if found is not None else None

    def write(self, path: str, content: str) -> None:
        pure = _normal(path)
        if not _in_scope(pure, self._seat.writes):
            raise SeatViolation(
                f"the {self._seat.purpose} seat may not write {path!r}"
            )
        from .durable.files import UserFile

        folder = str(pure.parent) if str(pure.parent) != "." else ""
        existing = self._find(pure)
        if existing is not None:
            self._store.save(existing.model_copy(update={"content": content}))
        else:
            self._store.save(
                UserFile(
                    tenant_id=self._tenant,
                    node_id=self._node_id,
                    folder=folder,
                    name=pure.name,
                    media_type=_seat_media_type(pure.name),
                    content=content,
                )
            )
        self.written.append(str(pure))

    # ------------------------------------------------------------------ #
    def _find(self, pure: PurePosixPath):
        folder = str(pure.parent) if str(pure.parent) != "." else ""
        for file in self._store.list(tenant=self._tenant, node_id=self._node_id):
            if file.folder == folder and file.name == pure.name:
                return file
        return None


def _seat_media_type(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".py"):
        return "text/x-python"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".md"):
        return "text/markdown"
    return "text/plain"
