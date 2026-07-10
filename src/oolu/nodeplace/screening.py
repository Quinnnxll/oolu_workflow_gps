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
