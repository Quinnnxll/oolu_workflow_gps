"""The live-audition rig, wire-true — a brain behind the REAL router.

Exit gates: the Level B seat is reachable through OoLu's actual
provider stack — ChatModelRouter, the Anthropic adapter, the secret
vault, the call meter — with only the HTTP wire scripted. The scripted
"provider" speaks the protocol and earns FIT through the whole
vertical; every model call enters the books so the audition's cost
column is measured, not estimated; the planner's protocol prompt rides
the wire as Anthropic's system PARAMETER; and a rig with no brain
configured refuses in words instead of pretending.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cadquery")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from level_b import fit_for_the_seat, model_planner, run  # noqa: E402
from level_b_audition import AUDITION_PURPOSE, build_brain  # noqa: E402
from test_model_planner import (  # noqa: E402
    _ASSEMBLE,
    _BUILD,
    _approve,
    _file_evidence,
    _grow_bore,
    _step,
)

from oolu.billing import ModelCallMeter  # noqa: E402
from oolu.durable.connection import DurableConnection  # noqa: E402
from oolu.providers.base import ProviderResponse  # noqa: E402
from oolu.providers.chatmodel import ChatModelRouter  # noqa: E402
from oolu.providers.keyring import ModelKeyring  # noqa: E402


class WireBrain:
    """A provider on the wire: answers Anthropic's /messages shape with
    the engineer's protocol steps, one per call, and keeps every request
    body so the wire itself can be inspected."""

    def __init__(self):
        self.requests: list[dict] = []
        self._steps = [
            _step(_grow_bore(base=1)),
            _step(_BUILD),
            _step(_ASSEMBLE),
            _step(_file_evidence(base=2)),
            _step(_approve(base=3)),
            _step({"verb": "done"}),
        ]

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append(
            {"url": url, "headers": dict(headers or {}), "body": body}
        )
        text = (
            self._steps.pop(0)
            if self._steps
            else json.dumps({"verb": "done"})
        )
        return ProviderResponse(
            status=200,
            json={
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 400, "output_tokens": 90},
            },
        )


def test_a_wire_true_brain_earns_the_seat_and_pays_its_way(tmp_path):
    wire = WireBrain()
    meter = ModelCallMeter()
    conn = DurableConnection(tmp_path / "d.db")
    try:
        keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
        keyring.store("bench", "anthropic", "sk-ant-0123456789")
        router = ChatModelRouter(
            keyring,
            "bench",
            transport=wire,
            meter=meter,
            source=lambda: "own-api",
            max_tokens=2048,
            purpose=AUDITION_PURPOSE,
        )
        report = run(model_planner(router), name="wire-brain")
    finally:
        conn.close()

    # The whole vertical, through the real adapter stack, to a verdict.
    assert report.completed, report.acceptance
    assert fit_for_the_seat(report)
    assert report.steps_used == 5

    # The audition PAID for its turns, on the books, per §22.
    charges = meter.charges(AUDITION_PURPOSE)
    assert len(charges) == 6  # five steps + the closing "done"
    assert sum(c.cost for c in charges) > 0

    # The wire is honest Anthropic shape: the planner's protocol rides
    # as the system PARAMETER, the key in its one header, and the
    # kernel's feedback reached the model as conversation turns.
    first = wire.requests[0]
    assert "/messages" in first["url"]
    assert "planning seat" in first["body"]["system"]
    assert first["headers"]["x-api-key"] == "sk-ant-0123456789"
    last = wire.requests[-1]
    assert any(
        "committed" in str(m.get("content", ""))
        for m in last["body"]["messages"]
    )


def test_the_rig_refuses_in_words_without_a_brain(tmp_path, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OOLU_LOCAL_URL",
        "OOLU_LOCAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_brain(tmp_path, tier="fast") is None


def test_the_rig_builds_an_own_api_brain_from_the_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OOLU_LOCAL_URL", raising=False)
    monkeypatch.delenv("OOLU_LOCAL_MODEL", raising=False)
    brain = build_brain(tmp_path, tier="fast")
    assert brain is not None
    router, meter = brain
    assert isinstance(router, ChatModelRouter)
    assert meter.charges(AUDITION_PURPOSE) == []
