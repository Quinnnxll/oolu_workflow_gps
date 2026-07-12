"""Local desktop training: `oolu representative-train` end to end.

Exit gate (docs/representative-plan.md, Phase 2): a desktop user produces
a working adapter fully offline — one command sweeps what's due onto the
dedicated queue, trains (a fake command honoring the real contract here),
lands the artifact and registry row, and says so in plain words. Status
speaks before training does; failures exit nonzero without wedging.
"""

from __future__ import annotations

import io
import sys

from oolu.cli import main
from oolu.representative import RepresentativeStore

_FAKE_TRAIN = (
    "import json, sys, pathlib;"
    "cfg = json.loads(pathlib.Path(sys.argv[1]).read_text());"
    "out = pathlib.Path(cfg['output_dir']);"
    "(out / 'adapter_config.json').write_text('{}');"
    "(out / 'metrics.json').write_text(json.dumps({'holdout_ppl': 4.25}))"
)


def _install(tmp_path, *, exchanges=10):
    data = tmp_path / "unified"
    data.mkdir()
    store = RepresentativeStore(data / "representative.db")
    store.configure("t1:alice", mode="draft")
    for i in range(exchanges):
        store.remember_exchange(
            "t1:alice",
            key=f"k{i}",
            prompt=f"question number {i} about the deploy?",
            reply=f"answer {i}: push to main and the action does the rest",
        )
    store.close()
    return data


def _train_cmd(script: str) -> list[str]:
    # shlex-safe: no spaces inside the python -c payload's path handling.
    return [
        "representative-train",
        "--trainer-command",
        f'"{sys.executable}" -c "{script}" {{config}}',
    ]


def test_status_speaks_before_any_training(tmp_path):
    data = _install(tmp_path, exchanges=3)
    buf = io.StringIO()
    assert main(["representative-status", "--data", str(data)], out=buf) == 0
    line = buf.getvalue()
    assert "t1:alice" in line and "mode=draft" in line
    assert "gathering voice (3/" in line and "no adapter yet" in line

    # A wrong --data is a plain refusal, not a stack trace.
    assert main(["representative-status", "--data", str(tmp_path / "nope")]) == 2


def test_one_command_takes_the_desktop_from_due_to_active_voice(tmp_path):
    data = _install(tmp_path)
    buf = io.StringIO()
    code = main(
        [*_train_cmd(_FAKE_TRAIN), "--data", str(data), "--floor", "5"],
        out=buf,
    )
    report = buf.getvalue()
    assert code == 0, report
    assert "1 scope(s) due" in report
    assert "v1 trained" in report and "ACTIVE" in report
    assert "ppl 4.25" in report

    store = RepresentativeStore(data / "representative.db")
    active = store.active_adapter("t1:alice")
    assert active is not None and active["artifact_ref"].startswith("sha256:")
    store.close()
    # The adapter lives ON this machine, under the install's own directory.
    live = list((data / "adapters" / "live").rglob("adapter_config.json"))
    assert len(live) == 1

    # Status now names the trained voice, and nothing is due.
    buf = io.StringIO()
    main(["representative-status", "--data", str(data)], out=buf)
    assert "v1 (ppl 4.25)" in buf.getvalue()

    # Running again with nothing new is a quiet no-op.
    buf = io.StringIO()
    assert (
        main(
            [*_train_cmd(_FAKE_TRAIN), "--data", str(data), "--floor", "5"],
            out=buf,
        )
        == 0
    )
    assert "0 scope(s) due" in buf.getvalue()


def test_below_the_floor_nothing_trains_and_failures_exit_nonzero(tmp_path):
    data = _install(tmp_path, exchanges=4)
    buf = io.StringIO()
    assert (
        main(
            [*_train_cmd(_FAKE_TRAIN), "--data", str(data), "--floor", "50"],
            out=buf,
        )
        == 0
    )
    assert "0 scope(s) due" in buf.getvalue()

    # A trainer that dies fails the run loudly — and the user isn't wedged:
    # no adapter went active, drafting continues on the base model.
    buf = io.StringIO()
    code = main(
        [
            *_train_cmd("import sys; sys.exit(3)"),
            "--data",
            str(data),
            "--floor",
            "2",
        ],
        out=buf,
    )
    assert code == 1
    assert "FAILED" in buf.getvalue()
    store = RepresentativeStore(data / "representative.db")
    assert store.active_adapter("t1:alice") is None
    store.close()
