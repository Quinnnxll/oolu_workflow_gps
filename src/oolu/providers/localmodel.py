"""The machine's own brain, by default: Qwen3-4B through Ollama.

One model family serves both local jobs: ``qwen3:4b`` is the chat default
(the ``model.local_model`` setting), and its Hugging Face twin
``Qwen/Qwen3-4B-Instruct`` is the representative trainer's QLoRA base —
the voice a user trains locally is the same family they chat with.

``ensure_default_local_model`` is the launch hook: best-effort, never
blocking, never fatal. Ollama installed and the model absent → pull it;
anything else → report in one word and get out of the way. Users who want
a different local model change the setting — this only fills the empty
default, it never overrides a choice.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

# The Ollama tag of the default local chat model. Same family as
# representative.trainer's DEFAULT base (Qwen/Qwen3-4B-Instruct).
DEFAULT_LOCAL_MODEL = "qwen3:4b"
DEFAULT_LOCAL_URL = "http://127.0.0.1:11434/v1"

# Pulling ~2.5 GB on a slow line takes a while; the hook runs in the
# background, so generous beats truncated.
_PULL_TIMEOUT_S = 3600.0


def ensure_default_local_model(
    model: str = DEFAULT_LOCAL_MODEL,
    *,
    run: Callable = subprocess.run,
    which: Callable = shutil.which,
) -> str:
    """Make the default local model exist if this machine can host one.

    Returns one word for the log: ``no-ollama`` (nothing to install into),
    ``present`` (already pulled), ``pulled`` (installed now), or
    ``failed`` (Ollama refused — its own message tells the user why).
    """
    ollama = which("ollama")
    if not ollama:
        return "no-ollama"
    try:
        listed = run(
            [ollama, "list"], capture_output=True, text=True, timeout=30.0
        )
        if listed.returncode == 0 and model.split(":")[0] in (
            listed.stdout or ""
        ):
            return "present"
        pulled = run(
            [ollama, "pull", model],
            capture_output=True,
            text=True,
            timeout=_PULL_TIMEOUT_S,
        )
        return "pulled" if pulled.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError):
        return "failed"
