"""Deliverable 4 — the real compiled graph self-heals a missing dependency.

Nothing about the healing is mocked: the FakeGateway only supplies the script; the
real SubprocessBackend runs it, the real failure is classified, the dependency is
resolved through the builtin mismatch map (slugify -> python-slugify), and uv really
installs it. Needs langgraph + uv.
"""

from __future__ import annotations

import pytest

from workflow_gps.config import Settings, build_workflow_gps
from workflow_gps.routing.gateway import FakeGateway
from workflow_gps.runtime.backend import ResourceLimits
from workflow_gps.runtime.isolation import SubprocessBackend

pytestmark = [pytest.mark.slow, pytest.mark.needs_uv, pytest.mark.needs_langgraph]

SCRIPT = """```python
import slugify
from _wfgps_runtime import emit_result
emit_result({"slug": slugify.slugify("Hello, World!  Workflow-GPS rocks")})
```"""


def test_real_dependency_self_heal():
    engine = build_workflow_gps(
        Settings(),
        gateway=FakeGateway([SCRIPT]),          # one synthesis; heal must re-run, not re-synthesize
        backend=SubprocessBackend(),            # real uv install + execution
    )
    # Tighten timeouts a touch via a fresh limits object on the settings path is
    # unnecessary here; defaults (120s install / 30s exec) are plenty.
    result = engine.run("slugify a greeting")

    assert result.success, result.failure_reason
    assert result.answer == {"slug": "hello-world-workflow-gps-rocks"}
    assert result.recalc_count == 1  # exactly one heal cycle
