from __future__ import annotations

import os
import shlex
import subprocess
import sys

_quiet = False


def set_quiet(q: bool) -> None:
    global _quiet
    _quiet = q


def log(msg: str) -> None:
    if not _quiet:
        print(msg, file=sys.stderr, flush=True)


def run(args, check=True, **kw):
    return subprocess.run(args, check=check, **kw)


def default_jobs() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 4))


def fill(command: str, **values: object) -> str:
    """Replace {bin}, {here}, ... with shell-quoted values; leave other braces alone."""
    out = command
    for k, v in values.items():
        out = out.replace("{" + k + "}", shlex.quote(str(v)))
    return out
