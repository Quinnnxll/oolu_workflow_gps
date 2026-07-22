"""The exact-value reference layer: the LLM plans with refs, the runtime
holds the values.

The architectural form of the exact-value rule. Every authoritative
value is stored ONCE — immutable, typed, tenant-owned, content-hashed —
and everything upstream of execution speaks about it by reference:

    value://{tenant}/{value_id}

The model (or any planner) selects and arranges references; the
deterministic BINDER resolves them — tenant wall, type check, honest
lookup failure — into the exact stored values just before execution, so
what reaches the sandbox's ``bindings.json`` is what the runtime holds,
never what a model retyped. After a run, result outputs snapshot into
the same store (``source="result"``), and the deterministic RENDERER
substitutes them into response segments — the model writes the sentence
structure, the store supplies every number, identifier, and date.

Failure is never fabrication: an unknown reference, a tenant mismatch,
or a type mismatch refuses with the reason named. The store is
append-only and content-addressed — the same tenant storing the same
typed value gets the same reference back, so provenance stays one row.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class ValueError_(LookupError):
    """A reference that cannot be honored — unknown, walled, or mistyped.
    Named with the reason; never answered with a made-up value."""


_REF_RE = re.compile(r"^value://(?P<tenant>[^/]+)/(?P<value_id>[A-Za-z0-9_-]+)$")

# The edge form: an output PORT reference — "whatever the named producer
# last filed on that port". An edge never copies a value; it declares the
# relationship, and the binder resolves it through the port index at run
# time (the doc's ``nodeA.output.port``).
_OUT_RE = re.compile(
    r"^output://(?P<producer>[A-Za-z0-9_:.-]+)/(?P<port>[A-Za-z0-9_.-]+)$"
)

# The trinity the slot vocabulary knows, plus the doc's exact-critical
# types. "json" carries structured values verbatim.
VALUE_TYPES = (
    "str",
    "number",
    "path",
    "decimal",
    "date",
    "datetime",
    "identifier",
    "currency",
    "email",
    "json",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _id_for(tenant: str, value_type: str, canonical: str) -> str:
    digest = hashlib.sha256(
        f"{tenant}|{value_type}|{canonical}".encode()
    ).hexdigest()
    return f"val{digest[:20]}"


class ValueRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    value_id: str
    tenant_id: str
    value_type: str
    # The exact value, JSON-encoded — decimals and identifiers ride as
    # strings so scale, leading zeros, and case survive verbatim.
    canonical_json: str
    label: str = ""
    source: str = "input"  # input | result | manual
    classification: str = ""  # e.g. "financial", "untrusted_data"
    version: int = 1
    sha256: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def ref(self) -> str:
        return f"value://{self.tenant_id}/{self.value_id}"

    @property
    def value(self) -> Any:
        return json.loads(self.canonical_json)


_SCHEMA = """CREATE TABLE IF NOT EXISTS value_records (
    value_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, value_id)
)"""

# The port index: (producer, port) -> the value LAST filed there. One row
# per port that always points at the newest snapshot; the history stays in
# the append-only value_records — retries never overwrite a value, they
# move this pointer.
_PORTS_SCHEMA = """CREATE TABLE IF NOT EXISTS value_ports (
    tenant_id TEXT NOT NULL,
    producer TEXT NOT NULL,
    port TEXT NOT NULL,
    value_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, producer, port)
)"""

# The lineage ledger: which stored values went INTO producing which stored
# values, and through whom. Append-only; one row per (input, output, node).
_LINEAGE_SCHEMA = """CREATE TABLE IF NOT EXISTS value_lineage (
    tenant_id TEXT NOT NULL,
    output_value_id TEXT NOT NULL,
    input_value_id TEXT NOT NULL,
    node TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, output_value_id, input_value_id, node)
)"""


def parse_ref(ref: Any) -> tuple[str, str] | None:
    """(tenant, value_id) when ``ref`` is a value reference — a plain
    ``value://…`` string or the ``{"$ref": …}`` envelope — else None."""
    if isinstance(ref, dict) and set(ref) >= {"$ref"}:
        ref = ref.get("$ref")
    if not isinstance(ref, str):
        return None
    match = _REF_RE.match(ref)
    if match is None:
        return None
    return match.group("tenant"), match.group("value_id")


def parse_output_ref(ref: Any) -> tuple[str, str] | None:
    """(producer, port) when ``ref`` is an output-port reference — the
    ``output://{producer}/{port}`` edge form (plain string or the
    ``{"$ref": …}`` envelope) — else None."""
    if isinstance(ref, dict) and set(ref) >= {"$ref"}:
        ref = ref.get("$ref")
    if not isinstance(ref, str):
        return None
    match = _OUT_RE.match(ref)
    if match is None:
        return None
    return match.group("producer"), match.group("port")


class ValueStore:
    """The immutable, content-addressed home of every exact value."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or _now
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(_PORTS_SCHEMA)
            db.execute(_LINEAGE_SCHEMA)

    # -- storing --------------------------------------------------------- #
    def put(
        self,
        tenant: str,
        value: Any,
        *,
        value_type: str = "str",
        label: str = "",
        source: str = "input",
        classification: str = "",
    ) -> ValueRecord:
        """Store one exact value; the same tenant storing the same typed
        value gets the same reference back (content-addressed), so
        provenance stays one row and refs are stable across retries."""
        if value_type not in VALUE_TYPES:
            raise ValueError_(f"unknown value type '{value_type}'")
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str
        )
        record = ValueRecord(
            value_id=_id_for(tenant, value_type, canonical),
            tenant_id=tenant,
            value_type=value_type,
            canonical_json=canonical,
            label=label,
            source=source,
            classification=classification,
            sha256=hashlib.sha256(canonical.encode()).hexdigest(),
            created_at=self._clock(),
        )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO value_records (value_id, tenant_id, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(tenant_id, value_id) DO NOTHING""",
                (record.value_id, tenant, record.model_dump_json()),
            )
        return self.get(record.ref, tenant=tenant)

    # -- reading (the wall lives here) ------------------------------------ #
    def get(self, ref: str, *, tenant: str) -> ValueRecord:
        out = parse_output_ref(ref)
        if out is not None:
            # The edge form: resolve the port to the value last filed
            # there, inside THIS tenant's index alone. An empty port is
            # an honest miss — the upstream node has not produced yet.
            resolved = self.port_ref(tenant, out[0], out[1])
            if resolved is None:
                raise ValueError_(
                    f"no value filed on output port '{out[1]}' of "
                    f"'{out[0]}' — the upstream node has not produced it"
                )
            return self.get(resolved, tenant=tenant)
        parsed = parse_ref(ref)
        if parsed is None:
            raise ValueError_(f"'{ref}' is not a value reference")
        ref_tenant, value_id = parsed
        if ref_tenant != tenant:
            # A cross-tenant reference is a security event, not a miss.
            raise ValueError_(
                "the reference belongs to another tenant — refused"
            )
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM value_records"
                " WHERE tenant_id = ? AND value_id = ?",
                (tenant, value_id),
            ).fetchone()
        if row is None:
            raise ValueError_(f"value reference not found: {ref}")
        return ValueRecord.model_validate_json(row["payload_json"])

    def resolve(
        self, ref: str, *, tenant: str, expected_type: str | None = None
    ) -> Any:
        record = self.get(ref, tenant=tenant)
        if expected_type is not None and record.value_type != expected_type:
            raise ValueError_(
                f"type mismatch: expected {expected_type}, "
                f"the reference holds {record.value_type}"
            )
        return record.value

    # -- the binder over a bindings dict ---------------------------------- #
    def resolve_bindings(
        self, bindings: dict[str, Any], *, tenant: str
    ) -> tuple[dict[str, Any], list[dict]]:
        """The deterministic binder: every ``value://`` reference in a
        bindings dict becomes its exact stored value; literals pass
        through untouched. Returns ``(resolved, provenance)`` — one
        provenance line per resolved reference, ready for the audit
        log. Any reference that cannot be honored raises with the
        parameter and reason named; nothing is half-bound."""
        resolved: dict[str, Any] = {}
        provenance: list[dict] = []
        for name, raw in dict(bindings or {}).items():
            out = parse_output_ref(raw)
            parsed = parse_ref(raw)
            if out is None and parsed is None:
                resolved[name] = raw
                continue
            ref = (
                f"output://{out[0]}/{out[1]}"
                if out is not None
                else f"value://{parsed[0]}/{parsed[1]}"
            )
            try:
                record = self.get(ref, tenant=tenant)
            except ValueError_ as exc:
                raise ValueError_(f"binding '{name}': {exc}") from exc
            resolved[name] = record.value
            line = {
                "parameter": name,
                "value_ref": record.ref,
                "value_type": record.value_type,
                "sha256": record.sha256,
                "source": record.source,
            }
            if out is not None:
                # The edge the planner wrote, kept next to the value it
                # resolved to — the audit reads both.
                line["port_source"] = ref
            provenance.append(line)
        return resolved, provenance

    # -- result snapshots -------------------------------------------------- #
    def snapshot_outputs(
        self,
        tenant: str,
        outputs: Any,
        *,
        label: str = "",
        producer: str = "",
    ) -> dict[str, str]:
        """A run's result outputs, filed as immutable values — the refs
        the renderer (and any later plan) speaks about them by. Scalar
        fields of dict outputs become one value each; anything else is
        filed whole as JSON. Naming a ``producer`` also points the port
        index at each filed value, so ``output://{producer}/{port}``
        edges resolve to this run's answer from now on."""
        refs: dict[str, str] = {}

        def _file(name: str, value: Any) -> None:
            value_type = (
                "number"
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else "str"
                if isinstance(value, str)
                else "json"
            )
            record = self.put(
                tenant,
                value,
                value_type=value_type,
                label=label or name,
                source="result",
            )
            refs[name] = record.ref
            if producer:
                self._point_port(tenant, producer, name, record.value_id)

        if isinstance(outputs, dict):
            for name, value in outputs.items():
                _file(str(name), value)
        else:
            _file("result", outputs)
        return refs

    # -- the port index ---------------------------------------------------- #
    def _point_port(
        self, tenant: str, producer: str, port: str, value_id: str
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO value_ports
                       (tenant_id, producer, port, value_id, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, producer, port)
                   DO UPDATE SET value_id = excluded.value_id,
                                 updated_at = excluded.updated_at""",
                (tenant, producer, port, value_id, self._clock().isoformat()),
            )

    def port_ref(self, tenant: str, producer: str, port: str) -> str | None:
        """The ``value://`` reference last filed on a producer's output
        port — the edge's current answer — or None while the port is
        still empty. Walled: only this tenant's index is consulted."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT value_id FROM value_ports"
                " WHERE tenant_id = ? AND producer = ? AND port = ?",
                (tenant, producer, port),
            ).fetchone()
        if row is None:
            return None
        return f"value://{tenant}/{row['value_id']}"

    def ports_of(self, tenant: str, producer: str) -> dict[str, str]:
        """Every output port a producer has filed, with its current ref —
        the shape downstream work binds against."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT port, value_id FROM value_ports"
                " WHERE tenant_id = ? AND producer = ? ORDER BY port",
                (tenant, producer),
            ).fetchall()
        return {
            row["port"]: f"value://{tenant}/{row['value_id']}" for row in rows
        }

    # -- lineage ------------------------------------------------------------ #
    def record_lineage(
        self,
        tenant: str,
        node: str,
        input_refs: list[str],
        output_refs: list[str],
    ) -> int:
        """File the input→output relationships one execution created:
        every input value fed every output value, through ``node``.
        Idempotent (a retry files the same rows once); non-references
        among the inputs are skipped — literals have no lineage."""
        rows = 0
        stamp = self._clock().isoformat()
        with self._conn.transaction() as db:
            for out_ref in output_refs:
                out = parse_ref(out_ref)
                if out is None or out[0] != tenant:
                    continue
                for in_ref in input_refs:
                    parsed = parse_ref(in_ref)
                    if parsed is None or parsed[0] != tenant:
                        continue
                    db.execute(
                        """INSERT OR IGNORE INTO value_lineage
                               (tenant_id, output_value_id, input_value_id,
                                node, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (tenant, out[1], parsed[1], node, stamp),
                    )
                    rows += 1
        return rows

    def lineage(self, tenant: str, ref: str) -> dict:
        """A value's place in the chain, both directions: the inputs it
        was computed FROM, and the outputs computed from IT — each line
        naming the node that did the work. An unknown reference answers
        empty lists, never an invention."""
        parsed = parse_ref(ref)
        if parsed is None or parsed[0] != tenant:
            return {"inputs": [], "outputs": []}
        value_id = parsed[1]
        with self._conn.lock:
            made_from = self._conn.db.execute(
                "SELECT input_value_id, node FROM value_lineage"
                " WHERE tenant_id = ? AND output_value_id = ?"
                " ORDER BY input_value_id",
                (tenant, value_id),
            ).fetchall()
            went_into = self._conn.db.execute(
                "SELECT output_value_id, node FROM value_lineage"
                " WHERE tenant_id = ? AND input_value_id = ?"
                " ORDER BY output_value_id",
                (tenant, value_id),
            ).fetchall()
        return {
            "inputs": [
                {
                    "value_ref": f"value://{tenant}/{row['input_value_id']}",
                    "node": row["node"],
                }
                for row in made_from
            ],
            "outputs": [
                {
                    "value_ref": f"value://{tenant}/{row['output_value_id']}",
                    "node": row["node"],
                }
                for row in went_into
            ],
        }


# --------------------------------------------------------------------------- #
# The deterministic renderer: structure from the model, values from the store. #
# --------------------------------------------------------------------------- #
# Registered formatters only — the model never controls rounding.
_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "raw": lambda v: v if isinstance(v, str) else json.dumps(
        v, ensure_ascii=False, sort_keys=True, default=str
    ),
    # Decimals ride as strings end to end; preserving scale means NOT
    # touching them.
    "decimal_exact": lambda v: str(v),
    "currency_code": lambda v: str(v).upper(),
    "date_iso": lambda v: str(v)[:10],
    "identifier": lambda v: str(v),
}

MAX_SEGMENTS = 100
MAX_TEXT_CHARS = 2000


def render_segments(
    segments: list[dict], *, store: ValueStore, tenant: str
) -> str:
    """The response, deterministically: text segments verbatim, value
    segments resolved from the store through a registered formatter.
    A missing reference or unknown formatter refuses — the renderer
    never fabricates the value it was asked to guarantee."""
    if len(segments) > MAX_SEGMENTS:
        raise ValueError_(f"too many segments (max {MAX_SEGMENTS})")
    output: list[str] = []
    for segment in segments:
        kind = segment.get("type")
        if kind == "text":
            content = str(segment.get("content", ""))
            if len(content) > MAX_TEXT_CHARS:
                raise ValueError_("text segment too long")
            output.append(content)
            continue
        if kind == "value":
            formatter = _FORMATTERS.get(str(segment.get("format", "raw")))
            if formatter is None:
                raise ValueError_(
                    f"unknown formatter '{segment.get('format')}' — only "
                    "registered formatters render"
                )
            value = store.resolve(str(segment.get("ref", "")), tenant=tenant)
            output.append(formatter(value))
            continue
        raise ValueError_(f"unsupported segment type '{kind}'")
    return "".join(output)
