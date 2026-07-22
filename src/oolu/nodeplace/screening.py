"""The function screen: refuse the obvious before anything runs.

Developers can bring functions written OUTSIDE OoLu, so every script that
becomes a node's function passes this screen — the antivirus step of the
gate. It is a SCREEN, not a guarantee: the real walls are the sandbox
(docker isolation, no network, no host credentials at run time) and
verify-by-execution before anything is trusted or cached. What this adds
is an honest, immediate refusal for code that is obviously hostile, with
the reason in words — instead of letting it reach the sandbox at all.

Each pattern names ONE thing no legitimate node function does. Plain
`base64.b64decode` on data is fine; feeding decoded bytes to exec is not.
"""

from __future__ import annotations

import re

_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:exec|eval)\s*\(.{0,120}?(?:b64decode|a85decode|fromhex|"
            r"marshal\.loads|zlib\.decompress)",
            re.S,
        ),
        "executes decoded/obfuscated payloads",
    ),
    (
        re.compile(
            r"(?:b64decode|a85decode|fromhex|marshal\.loads|zlib\.decompress)"
            r".{0,120}?(?:exec|eval)\s*\(",
            re.S,
        ),
        "decodes a payload straight into exec/eval",
    ),
    (
        re.compile(r"\bcompile\s*\(.{0,80}?['\"]exec['\"]", re.S),
        "compiles dynamic code for execution",
    ),
    (
        re.compile(r"\bsocket\s*\.\s*socket\s*\("),
        "opens raw sockets (the sandbox has no network; a node's function "
        "has no business trying)",
    ),
    (
        re.compile(r"pty\.spawn|/dev/tcp/|/bin/sh\s+-i|/bin/bash\s+-i"),
        "spawns an interactive shell (reverse-shell pattern)",
    ),
    (
        re.compile(r"/proc/1/|docker\.sock|\bchroot\s*\(|\bptrace\b"),
        "probes the container boundary",
    ),
    (
        re.compile(r"\.ssh/id_|aws/credentials|machine\.key|\.gnupg/"),
        "reaches for host credentials (none exist in the sandbox — trying "
        "is disqualifying)",
    ),
    (
        re.compile(r"\bos\.fork\s*\(|multiprocessing\.Pool\s*\(\s*\d{3,}"),
        "forks processes (fork-bomb pattern)",
    ),
    (
        re.compile(r"stratum\+tcp|xmrig|minerd\b"),
        "carries crypto-miner markers",
    ),
    (
        re.compile(r"\bctypes\b.{0,120}?(?:CDLL|WinDLL|windll|cdll)", re.S),
        "loads native libraries directly",
    ),
)


def screen_script(script: str) -> list[str]:
    """The reasons this script is refused — empty means it may proceed to
    the sandbox and the verify-by-execution gate."""
    text = str(script or "")
    return [reason for pattern, reason in _SIGNALS if pattern.search(text)]


# --------------------------------------------------------------------------- #
# The exact-value screen: mock code is a lie the sandbox cannot catch.         #
# --------------------------------------------------------------------------- #
# A mocked function RUNS — it emits a baked-in answer and every gate that
# only checks "did it execute" passes. The tell is deterministic: the
# result handed to emit_result is a constant the model wrote, or the code
# names its own pretending (mock/placeholder/dummy/sample data). The LLM
# decides structure; the runtime supplies values — a function that cannot
# reach its real data must emit_error, never fabricate.
# Substring, not word-bounded: the pretending hides inside identifiers
# too (make_mock_data, sample_data_rows).
_MOCK_MARKERS = re.compile(
    r"(mock|placeholder|dummy|fake[_ ]?data|sample[_ ]?data"
    r"|simulated|lorem ipsum)",
    re.IGNORECASE,
)


def _constant_only(node) -> bool:
    import ast

    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_constant_only(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (k is None or _constant_only(k)) and _constant_only(v)
            for k, v in zip(node.keys, node.values)
        )
    if isinstance(node, ast.JoinedStr):
        # An f-string of constants is still a constant in costume.
        return all(
            isinstance(v, ast.Constant)
            or (isinstance(v, ast.FormattedValue) and _constant_only(v.value))
            for v in node.values
        )
    return False


def mock_smells(script: str) -> list[str]:
    """The reasons an AUTHORED function reads as pretending — empty means
    it computes. Deterministic and AST-based, so the refusal is the same
    every time and the model can fix exactly what is named."""
    import ast

    text = str(script or "")
    reasons = []
    if _MOCK_MARKERS.search(text):
        reasons.append(
            "it names mock/placeholder/dummy/sample data — compute the "
            "real thing from real inputs, or emit_error naming what is "
            "missing; never fabricate"
        )
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return reasons  # the sandbox speaks to syntax; this screen is quiet
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "emit_result" or not node.args:
            continue
        if _constant_only(node.args[0]):
            reasons.append(
                "emit_result is handed a constant the model wrote — the "
                "result must be COMPUTED from real inputs (./bindings.json, "
                "staged files, the webhook payload, or http_request), "
                "never a baked-in value"
            )
            break
    return reasons
