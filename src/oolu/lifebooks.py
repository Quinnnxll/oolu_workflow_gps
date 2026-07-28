"""Life books — one function for everyone, one private book each.

The prebuilt personal nodes (Invoice Scan, Cashflow, Stock, Automation,
Reminder, Task, Calendar) share ONE architecture instead of seven
scattered ones:

- **The function is central and owned by the house.** Every member's
  node calls the SAME registered book kind, owned by the OoLu official
  account (``OFFICIAL_OWNER``) — members never own the function, so
  there is nothing per-member to fork, patch, or leak through.
- **The data is the member's own, in Life/Files.** Each book is one
  file in the member's OWN Life drawer at a canonical pointer
  (``books/<kind>.json``). The drawer's memories gate is the whole
  access model: the owner reads it, nobody else — not the node, not
  the admin, not another member. Everything a node documents lands
  there, reviewable at one glance.
- **The pointer is the command.** ``pointer(kind)`` names the one
  folder+name a book ever lives at, so OoLu reads and writes reliably
  — no search, no guess: the same kind, the same file, forever.
- **Rows are typed and chartable.** A row is ``{at, label, value,
  note}`` — enough for an invoice line, a cashflow entry, a stock
  count, a trigger, a task, or an event, and enough for the
  conversation's chart block to draw without interpretation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# The centralized owner of every prebuilt book function — an OoLu
# official account, not any member and not a per-tenant admin.
OFFICIAL_OWNER = "oolu-official"
BOOKS_FOLDER = "books"
MAX_ROWS = 2_000  # a book, not an archive — oldest rows fall off


@dataclass(frozen=True)
class BookKind:
    kind: str
    title: str
    unit: str  # what `value` means, for the chart's axis ("" = count)


BOOKS: dict[str, BookKind] = {
    b.kind: b
    for b in (
        BookKind("invoice_scan", "Invoice Scan", "amount"),
        BookKind("cashflow", "Cashflow", "amount"),
        BookKind("stock", "Stock", "quantity"),
        BookKind("automation", "Automation", ""),
        BookKind("reminder", "Reminders", ""),
        BookKind("task", "Tasks", ""),
        BookKind("calendar", "Calendar", ""),
    )
}


def pointer(kind: str) -> dict:
    """The one place a book ever lives — the pointer OoLu commands by."""
    if kind not in BOOKS:
        raise ValueError(f"unknown book: {kind!r}")
    return {"folder": BOOKS_FOLDER, "name": f"{kind}.json"}


def _find(files, *, tenant: str, owner: str, kind: str):
    where = pointer(kind)
    for f in files.list(tenant=tenant, owner=owner):
        if f.folder == where["folder"] and f.name == where["name"]:
            return f
    return None


def read_rows(files, *, tenant: str, owner: str, kind: str) -> list[dict]:
    """The member's own book, oldest first — empty when never written."""
    file = _find(files, tenant=tenant, owner=owner, kind=kind)
    if file is None or not file.content:
        return []
    try:
        rows = json.loads(file.content)
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def append_rows(
    files, *, tenant: str, owner: str, kind: str, rows: list[dict]
) -> int:
    """Write through the pointer: the rows land in the member's OWN
    drawer file (created at the pointer if absent), deduplicated on
    (at, label) so an import re-run never doubles a book."""
    from .durable.files import UserFile

    where = pointer(kind)
    standing = read_rows(files, tenant=tenant, owner=owner, kind=kind)
    seen = {(r.get("at"), r.get("label")) for r in standing}
    added = 0
    for row in rows:
        clean = {
            "at": str(row.get("at") or ""),
            "label": str(row.get("label") or ""),
            "value": row.get("value"),
            "note": str(row.get("note") or ""),
        }
        if (clean["at"], clean["label"]) in seen:
            continue
        standing.append(clean)
        seen.add((clean["at"], clean["label"]))
        added += 1
    standing.sort(key=lambda r: r["at"])
    standing = standing[-MAX_ROWS:]
    file = _find(files, tenant=tenant, owner=owner, kind=kind)
    content = json.dumps(standing)
    if file is None:
        files.save(
            UserFile(
                tenant_id=tenant,
                owner=owner,
                name=where["name"],
                folder=where["folder"],
                media_type="application/json",
                content=content,
            )
        )
    else:
        files.save(file.model_copy(update={"content": content}))
    return added


def chart_points(kind: str, rows: list[dict], *, limit: int = 12) -> list[dict]:
    """The conversation chart's data, deterministically: numeric rows
    plot as they stand (the last ``limit``); label-only rows plot as
    counts per label — never an interpretation, always the book."""
    numeric = [
        {"label": r["label"] or r["at"][:10], "value": float(r["value"])}
        for r in rows
        if isinstance(r.get("value"), (int, float))
    ]
    if numeric:
        return numeric[-limit:]
    counts: dict[str, int] = {}
    for r in rows:
        key = (r.get("label") or r.get("at", "")[:10] or "…")[:24]
        counts[key] = counts.get(key, 0) + 1
    return [
        {"label": k, "value": v}
        for k, v in sorted(counts.items())[:limit]
    ]
