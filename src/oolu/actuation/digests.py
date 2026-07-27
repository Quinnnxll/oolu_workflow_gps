"""Canonical digests for actuation authorization.

The plan defines *what* the aggregate digest binds; this module defines
*how* the bytes are produced, because two implementations that disagree
on key order or float formatting will compute different digests for the
same job — turning the strongest control in the protocol into an interop
failure.

The rules, pinned once:

* canonical JSON: keys sorted, compact separators, UTF-8, no NaN/Inf;
* floats are **rejected** in digest-bound material — physical parameters
  that matter to authorization must be carried as strings or scaled
  integers chosen by the schema, so a serializer's float formatting can
  never fork a digest;
* every digest is domain-separated by a versioned prefix, so a job
  digest can never collide with, say, a program digest over the same
  bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

# Domain-separation prefixes. Bump the version when the bound field set
# or canonicalization rules change — old digests must not verify as new.
JOB_DIGEST_DOMAIN = "oolu-actuation-job/v1"

# The fields §24.4 requires the aggregate digest to bind. A job missing
# any of these cannot be digested, so it cannot be leased or armed.
JOB_DIGEST_FIELDS = frozenset(
    {
        "intent_id",
        "plan_version_id",
        "workcell_version_id",
        "workpiece_instance_ids",
        "input_state_digest",
        "machine_instance_ids",
        "controller_instance_ids",
        "tool_instance_ids",
        "fixture_instance_ids",
        "coordinate_frame_graph_digest",
        "process_recipe_version_id",
        "process_parameters",
        "machine_program_digest",
        "validated_operating_envelope_id",
        "local_safety_configuration_digest",
        "expected_state_delta",
        "approval_bundle_id",
        "execution_window_start",
        "execution_window_end",
        "nonce",
    }
)


class DigestError(ValueError):
    """Raised when material cannot be canonically digested."""


def _reject_floats(value: Any, path: str) -> None:
    if isinstance(value, float):
        raise DigestError(
            f"float at {path!r} cannot enter digest-bound material; "
            "carry physical parameters as strings or scaled integers"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DigestError(f"non-string key at {path!r}")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")
    elif not isinstance(value, (str, int, bool, type(None))):
        raise DigestError(f"non-canonical type {type(value).__name__} at {path!r}")


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to the one canonical byte form."""

    _reject_floats(value, "$")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def aggregate_digest(fields: Mapping[str, Any]) -> str:
    """The §24.4 aggregate digest binding every execution-critical field.

    Refuses to digest an incomplete field set: a missing binding is an
    error here, not a weaker digest downstream. Extra fields are also
    refused so callers cannot silently widen what "bound" means.
    """

    missing = JOB_DIGEST_FIELDS - set(fields)
    if missing:
        raise DigestError(f"job digest missing bound fields: {sorted(missing)}")
    extra = set(fields) - JOB_DIGEST_FIELDS
    if extra:
        raise DigestError(f"job digest given unbound fields: {sorted(extra)}")
    payload = canonical_json({key: fields[key] for key in sorted(fields)})
    return sha256_hex(f"{JOB_DIGEST_DOMAIN}\n".encode() + payload)
