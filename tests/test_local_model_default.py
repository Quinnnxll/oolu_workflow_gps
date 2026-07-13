"""The machine's own brain by default: qwen3:4b, pulled at launch.

Exit gate: the local-model setting defaults to the same model family the
representative trainer uses as its QLoRA base (one family, chat and
voice); the launch hook pulls it through Ollama when Ollama exists, says
one word about what happened, and NEVER raises — a missing Ollama or a
dead network must not touch the shell's startup.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from oolu.providers.localmodel import (
    DEFAULT_LOCAL_MODEL,
    ensure_default_local_model,
)
from oolu.settings_node import SETTINGS_CATALOG


def _field(key):
    return next(field for field in SETTINGS_CATALOG if field.key == key)


def test_the_default_local_model_matches_the_qlora_family():
    assert DEFAULT_LOCAL_MODEL == "qwen3:4b"
    assert _field("model.local_model").default == DEFAULT_LOCAL_MODEL
    # The URL default is Ollama's door — the same server the pull fills.
    assert "11434" in _field("model.local_url").default


def _runner(script):
    """A fake subprocess.run: records commands, plays scripted results."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        verb = cmd[1]
        code, stdout = script.get(verb, (0, ""))
        return NS(returncode=code, stdout=stdout, stderr="")

    return run, calls


def test_the_launch_hook_pulls_only_what_is_missing():
    # No Ollama on this machine: nothing to install into, no error.
    assert (
        ensure_default_local_model(which=lambda _: None, run=None) == "no-ollama"
    )

    # Already pulled: listing names the family, no pull happens.
    run, calls = _runner({"list": (0, "NAME\nqwen3:4b  latest  2.6 GB")})
    assert (
        ensure_default_local_model(which=lambda _: "/usr/bin/ollama", run=run)
        == "present"
    )
    assert [cmd[1] for cmd in calls] == ["list"]

    # Missing: one pull, reported as installed.
    run, calls = _runner({"list": (0, "NAME\nllama3.2  latest  2 GB")})
    assert (
        ensure_default_local_model(which=lambda _: "/usr/bin/ollama", run=run)
        == "pulled"
    )
    assert [cmd[1] for cmd in calls] == ["list", "pull"]
    assert calls[1][2] == DEFAULT_LOCAL_MODEL

    # Ollama refuses (disk, network): one word, never a raise.
    run, _ = _runner({"list": (0, ""), "pull": (1, "")})
    assert (
        ensure_default_local_model(which=lambda _: "/usr/bin/ollama", run=run)
        == "failed"
    )

    def explode(cmd, **kwargs):
        raise OSError("no exec")

    assert (
        ensure_default_local_model(
            which=lambda _: "/usr/bin/ollama", run=explode
        )
        == "failed"
    )
