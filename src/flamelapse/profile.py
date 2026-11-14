"""Running the configured commands under callgrind (full profiles) or perf stat (cheap totals)."""
from __future__ import annotations

import collections
import functools
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .build import Built
from .config import Config
from .util import fill

DEPTH = 60          # call-stack depth callgrind keeps per function context
_SUFFIX = re.compile(r"(\.(isra|part|constprop|cold|lto_priv)\.\d+)+$|@@?GLIBC.*$")


def _inputs(cfg: Config) -> list:
    """Size and mtime of every {here}/file the commands mention, so replacing an input re-profiles."""
    out = []
    for cmd in [r.command for r in cfg.runs] + [cfg.check or ""]:
        for rel in re.findall(r"\{here\}/([^\s'\"|;&<>)]+)", cmd):
            f = cfg.here / rel
            st = f.stat() if f.is_file() else None
            out.append([rel, st.st_size if st else None, int(st.st_mtime) if st else None])
    return sorted(out)


def profile_key(cfg: Config) -> str:
    """Build key plus everything that changes what gets measured: runs, check, input files."""
    from .build import build_key
    blob = json.dumps([[[r.name, r.command] for r in cfg.runs], cfg.check, _inputs(cfg)])
    return f"{build_key(cfg)}-{hashlib.sha1(blob.encode()).hexdigest()[:6]}"


def frames_dir(cfg: Config) -> Path:
    return cfg.work / "profiles" / profile_key(cfg)


def _command(cfg: Config, built: Built, cmd: str, measured: Path | None = None) -> str:
    """The run command with placeholders filled. {bin} is `measured` (a wrapper that profiles the
    program) when given; {plainbin} is always the program itself, for helper calls."""
    return fill(cmd, bin=measured or built.bin, plainbin=built.bin, here=cfg.here, build=built.root)


def _wrapper(tmp: Path, built: Built, tool: str) -> Path:
    """A script standing in for the program that runs it under `tool`, one output file per call,
    so a run command can use {bin} in pipes, after &&, in $(...), any number of times."""
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    w = d / built.bin.name
    out = shlex.quote(str(tmp / "out"))
    w.write_text(f'#!/bin/sh\nexec {tool.format(out=out)} {shlex.quote(str(built.bin))} "$@"\n')
    w.chmod(0o755)
    (tmp / "out").mkdir(exist_ok=True)
    return w


def _run_measured(cfg: Config, built: Built, run, tmp: Path, tool: str) -> tuple[list[Path], str | None]:
    """Run one [[run]] with {bin} wrapped by `tool`; returns (output files, failure reason)."""
    w = _wrapper(tmp, built, tool)
    for f in (tmp / "out").iterdir():
        f.unlink()
    p = subprocess.run(["bash", "-c", _command(cfg, built, run.command, measured=w)], env=built.env(),
                       cwd=cfg.here, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, text=True, errors="replace")
    outs = sorted((tmp / "out").iterdir())
    if p.returncode != 0:
        vlog = "".join(o.read_text(errors="replace") for o in outs if o.name.startswith("log."))
        m = re.search(r"unhandled instruction bytes: (.*)", vlog)
        if m:
            return outs, (f"run {run.name!r} failed: Valgrind cannot run an instruction in this build "
                          f"({m.group(1).strip()[:40]}), probably from -march=native or hand-written SIMD")
        return outs, f"run {run.name!r} failed: {_last_words(p.stderr) or _exit_text(p.returncode)}"
    if not outs:
        return outs, f"run {run.name!r} never ran {{bin}}"
    return outs, None


def _exit_text(code: int) -> str:
    if code < 0 or code > 128:
        import signal
        n = -code if code < 0 else code - 128
        try:
            return f"killed by {signal.Signals(n).name}"
        except ValueError:
            pass
    return f"exit status {code}"


def _last_words(stderr: str) -> str:
    """The program's own last complaint, without valgrind's ==pid== chatter."""
    lines = [l.strip() for l in stderr.splitlines() if l.strip() and not re.match(r"==\d+==", l)]
    return lines[-1][:200] if lines else ""


def check(cfg: Config, built: Built) -> bool:
    return check_why(cfg, built) is None


def check_why(cfg: Config, built: Built) -> str | None:
    """None if the check passes (or there is none), else what it printed."""
    if not cfg.check:
        return None
    c = _command(cfg, built, cfg.check)
    p = subprocess.run(["bash", "-c", c], env=built.env(), cwd=cfg.here, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    return None if p.returncode == 0 else (_last_words(p.stderr) or _exit_text(p.returncode))


def parse_callgrind(path: Path) -> tuple[collections.Counter, dict]:
    """Self cost per calling context, and the source file of each function."""
    names: dict[str, str] = {}
    files: dict[str, str] = {}
    fn_file: dict[str, str] = {}
    cost = collections.Counter()
    cur, cur_file, skip, npos = None, "", False, 1

    def resolve(spec: str, table: dict) -> str:
        m = re.match(r"\((\d+)\)(?:\s+(.*))?", spec)
        if not m:
            return spec
        i, n = m.groups()
        if n is not None:
            table[i] = n
        return table.get(i, "?")

    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("positions:"):
                npos = len(line.split()) - 1
            elif line.startswith("fl="):
                cur_file = resolve(line[3:].strip(), files)
            elif line.startswith(("fi=", "fe=")):
                resolve(line[3:].strip(), files)
            elif line.startswith("fn="):
                cur = resolve(line[3:].strip(), names)
                fn_file.setdefault(cur.split("'", 1)[0], cur_file)
            elif line.startswith("cfn="):
                resolve(line[4:].strip(), names)
            elif line.startswith(("cfi=", "cfl=")):
                resolve(line[4:].strip(), files)
            elif line.startswith("calls="):
                skip = True
            elif line[:1].isdigit() or line[:1] in "+-*":
                if skip:
                    skip = False
                    continue
                parts = line.split()
                if len(parts) > npos and cur is not None:
                    cost[cur] += int(parts[npos])
    return cost, fn_file


def fold(cost: collections.Counter, root: str) -> collections.Counter:
    """Turn callgrind's callee'caller'caller contexts into root;...;leaf stacks."""
    out = collections.Counter()
    for ctx, c in cost.items():
        if not c:
            continue
        frames = [_SUFFIX.sub("", f) for f in ctx.split("'")]
        frames = [f for f in reversed(frames) if f and not f.isdigit()]
        if "main" in frames:
            frames = frames[frames.index("main"):]
        out[";".join([root] + frames)] += c
    return out


def profile(cfg: Config, built: Built, sha: str) -> str | None:
    """Full callgrind profile of every run; writes profiles/<sha>.json. Returns why a run failed, or None."""
    out_dir = frames_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    stacks = collections.Counter()
    files: dict[str, str] = {}
    totals = {}
    tool = (f"valgrind --tool=callgrind --separate-callers={DEPTH} "
            "--callgrind-out-file={out}/cg.$$ --log-file={out}/log.$$")
    with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
        for r in cfg.runs:
            outs, why = _run_measured(cfg, built, r, Path(tmp), tool)
            if why:
                return why
            folded = collections.Counter()
            for cg in (o for o in outs if o.name.startswith("cg.")):
                cost, fn_file = parse_callgrind(cg)
                folded.update(fold(cost, r.name.replace(";", ",")))
                for k, v in fn_file.items():
                    files.setdefault(_SUFFIX.sub("", k), v)
            stacks.update(folded)
            totals[r.name] = sum(folded.values())
    tmp_json = out_dir / f".{sha}.json.tmp"
    tmp_json.write_text(json.dumps({"runs": totals, "stacks": stacks, "files": files}))
    tmp_json.replace(out_dir / f"{sha}.json")       # atomic: an interrupted run never leaves half a profile
    return None


@functools.cache
def perf_usable() -> bool:
    try:
        p = subprocess.run(["perf", "stat", "-x,", "-e", "instructions:u", "true"],
                           capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and re.search(r"^\d+,", p.stderr, re.M) is not None


def measure(cfg: Config, built: Built) -> int | None:
    """Total user-space instructions over all runs (cheap; used to pin jumps to one commit)."""
    if perf_usable():
        tool, pat = "perf stat -x, -e instructions:u -o {out}/perf.$$", r"^(\d+),"
    else:
        tool, pat = "valgrind --tool=callgrind --callgrind-out-file=/dev/null --log-file={out}/log.$$", r"Collected\s*:\s*(\d+)"
    total = 0
    with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
        for r in cfg.runs:
            outs, why = _run_measured(cfg, built, r, Path(tmp), tool)
            if why:
                return None
            for o in outs:
                m = re.search(pat, o.read_text(errors="replace"), re.M)
                if not m:
                    return None
                total += int(m.group(1))
    return total
