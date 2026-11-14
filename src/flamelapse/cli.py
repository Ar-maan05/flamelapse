"""flamelapse: watch a program's performance change across its git history."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__, gitutil
from .config import CONFIG_NAME, Config, ConfigError, load
from .util import default_jobs, log, set_quiet

EXAMPLE_RUN = '{bin} --version'


# ---------------------------------------------------------------- helpers

def _toml_str(s: str) -> str:
    if "\n" in s:
        return '"""\n' + s.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
    return json.dumps(s)


def _name_from_repo(repo: str) -> str:
    if Path(repo).expanduser().is_dir():
        repo = str(Path(repo).expanduser().resolve())
    return re.sub(r"\.git$", "", repo.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "project"


def _cfg_stub(workspace: Path, name: str, repo: str, branch: str) -> Config:
    return Config(path=workspace / CONFIG_NAME, name=name, repo=repo, branch=branch)


def _render_config(*, name, repo, branch, since, samples, jobs, system, command, env, env_note, binary,
                   alternatives, runs) -> str:
    out = [
        f"# flamelapse configuration for {name}",
        "# Edit freely, then run `flamelapse run`. Everything here is optional except name, repo, [build] and [[run]].",
        "",
        f"name = {_toml_str(name)}",
        f"repo = {_toml_str(repo)}          # local path or URL; flamelapse works on its own clone",
    ]
    if branch != "HEAD":
        out.append(f"branch = {_toml_str(branch)}")
    out += [
        (f"since = {_toml_str(since)}            # oldest commit to include: a tag, commit or YYYY-MM-DD"
         if since else '# since = "v1.0.0"            # oldest commit to include: a tag, commit or YYYY-MM-DD'),
        f"samples = {samples}                # versions spread evenly over that range (release tags are added on top)",
        f"jobs = {jobs}                    # versions built and profiled at the same time",
        "",
        f"[build]                      # detected: {system}",
        f"command = {_toml_str(command)}",
        f"binary = {_toml_str(binary)}" + ("" if binary else "   # TODO: path of the program, relative to the repository"),
    ]
    if alternatives:
        out.append("# other programs the build produced: " + ", ".join(alternatives[:8]))
    if env:
        out.append(f"# {env_note}" if env_note else "")
        out.append("env = { " + ", ".join(f"{k} = {_toml_str(v)}" for k, v in env.items()) + " }")
    elif env_note:
        out.append(f"# {env_note}")
    out += [
        "",
        "# What to measure. {bin} is the program built from each version, {here} is this folder.",
        "# Use a realistic job that takes well under a second natively; it runs under Valgrind (about 50x slower).",
        "# Several [[run]] blocks are fine: each one becomes its own colour in the report.",
    ]
    for r in runs:
        out += ["[[run]]", f"name = {_toml_str(r[0])}", f"command = {_toml_str(r[1])}", ""]
    out += [
        "# Optional: a command that must succeed for a version to count (e.g. compare output).",
        '# check = "{bin} --version"',
        "",
        "# Optional: name the kinds of work in your own words. Rules are tried in order; `match` is a",
        "# regex on function names; where = \"leaf\" looks only at the function doing the work,",
        "# \"any\" at the whole call stack. Anything unmatched is grouped by run or by source file.",
        "# [[category]]",
        '# label = "Parsing"',
        '# match = "^parse_"',
        '# description = "Reading the input."',
        "",
        "# Optional: explain a commit in plain words on the report (key = commit hash prefix).",
        "# [notes]",
        '# "abc1234567" = "Switched the hash table to open addressing."',
        "",
    ]
    return "\n".join(out)


def _bring_inputs(runs, workspace: Path) -> None:
    """Commands run inside the workspace, but people keep input files where they typed the
    command. Copy any file a run names that exists here but not in the workspace."""
    cwd = Path.cwd()
    if workspace == cwd:
        return
    for _, cmd in runs:
        try:
            words = shlex.split(cmd)
        except ValueError:
            continue
        for w in words:
            rel = w.replace("{here}/", "")
            if "{" in rel or rel.startswith(("-", "/")) or not rel:
                continue
            src, dst = cwd / rel, workspace / rel
            if src.is_file() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                log(f"Copied {rel} into {workspace.name}/ (input files live next to flamelapse.toml)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- commands

def cmd_init(a) -> Path:
    workspace = Path(a.dir).resolve()
    cfg_path = workspace / CONFIG_NAME
    if cfg_path.exists() and not a.force:
        raise SystemExit(f"{cfg_path} already exists (use --force to overwrite).")
    workspace.mkdir(parents=True, exist_ok=True)
    name = a.name or _name_from_repo(a.repo)
    if Path(a.repo).expanduser().is_dir():
        a.repo = str(Path(a.repo).expanduser().resolve())     # so the config works from any folder
    stub = _cfg_stub(workspace, name, a.repo, a.branch)
    src = gitutil.sync(stub)
    tip = gitutil.resolve_branch(stub, src)
    from .detect import discover, guess
    top = set(gitutil.git(src, "ls-tree", "--name-only", tip).split())
    g = guess(top)
    if a.build:
        from .detect import BuildGuess
        g = BuildGuess("given", a.build, {}, "")
    if g is None:
        raise SystemExit("Could not tell how to build this project. Pass --build \"<commands>\" and --binary <path>.")
    runs = []
    for i, r in enumerate(a.run or []):
        nm, _, cmd = r.partition("=") if re.match(r"^[\w .,'()-]+=", r) and "{bin}" in r.partition("=")[2] else ("", "", r)
        runs.append((nm.strip() or (f"Run {i + 1}" if len(a.run) > 1 else "main"), cmd.strip()))
    if not runs:
        runs = [("main", EXAMPLE_RUN)]
        log("No --run given, so the report will measure `--version` (start-up only). For a real story, "
            "edit [[run]] to do representative work.")
    _bring_inputs(runs, workspace)
    binary, alts, problems = a.binary or "", [], []
    if not a.no_try:
        log(f"Detected a {g.system} build. Building the newest commit once to find the program...")
        d = discover(src, tip, g, stub.work, name, binary=binary, runs=runs, here=workspace)
        if not d.built:
            log("The build failed. Last lines of its output:\n" + d.log_tail)
            problems.append("the newest commit does not build")
        if d.programs and not binary:
            binary, alts = d.programs[0], d.programs[1:]
            log(f"Program: {binary}" + (f"   (others: {', '.join(alts[:5])})" if alts else ""))
        elif not d.programs and not binary:
            log("The build produced no programs; set `binary` in the config.")
            problems.append("no program found")
        for rname, code, secs, err in d.runs:
            if code == 0:
                slow = "  (slow: under Valgrind this will take minutes per version; consider a smaller job)" if secs > 2 else ""
                log(f"Run {rname!r} works: {secs:.2f}s natively{slow}")
            else:
                log(f"Run {rname!r} FAILED (exit {code}):\n  " + "\n  ".join(err.splitlines() or ["(no output)"]))
                problems.append(f"run {rname!r} fails")
    cfg_path.write_text(_render_config(
        name=name, repo=a.repo, branch=a.branch, since=a.since, samples=a.samples, jobs=a.jobs or default_jobs(),
        system=g.system, command=g.command, env=g.env, env_note=g.note, binary=binary, alternatives=alts, runs=runs))
    log(f"Wrote {cfg_path}")
    if problems:
        log(f"Fix this before `flamelapse run`: {'; '.join(problems)}. Edit the config and test it with `flamelapse try`.")
    a.problems = problems
    return cfg_path


def _load(a) -> Config:
    try:
        return load(getattr(a, "config", None))
    except ConfigError as e:
        raise SystemExit(str(e))


def cmd_run(a) -> None:
    from .pipeline import run_all
    cfg = _load(a)
    if a.samples:
        cfg.samples = a.samples
    if a.jobs:
        cfg.jobs = a.jobs
    run_all(cfg, do_refine=not a.no_pin, retry=getattr(a, "retry", False))
    cmd_report(a, cfg)


def cmd_report(a, cfg: Config | None = None) -> None:
    from .report import build
    cfg = cfg or _load(a)
    out = build(cfg, Path(a.out).resolve() if getattr(a, "out", None) else None)
    log(f"Report: {out / 'index.html'}")
    if getattr(a, "open", False):
        webbrowser.open((out / "index.html").as_uri() + f"?p={cfg.key}")


def cmd_open(a) -> None:
    cfg = _load(a)
    idx = cfg.report_dir / "index.html"
    if not idx.exists():
        raise SystemExit("No report yet. Run `flamelapse run` first.")
    url = idx.as_uri() + f"?p={cfg.key}"
    log(url)
    webbrowser.open(url)


def cmd_serve(a) -> None:
    root = Path(a.dir).resolve() if a.dir else _load(a).report_dir
    if not (root / "index.html").exists():
        raise SystemExit(f"No report in {root}.")
    port = a.port or _free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    log(f"Serving {root} at {url} (Ctrl+C to stop)")
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_try(a) -> None:
    """Build one version and run everything once, showing what goes wrong."""
    from .build import build, builds_dir
    from .profile import _command, check
    cfg = _load(a)
    src = gitutil.sync(cfg)
    sha = gitutil.git(src, "rev-parse", "--short=10", (a.rev or gitutil.resolve_branch(cfg, src)) + "^{commit}")
    shutil.rmtree(builds_dir(cfg) / sha, ignore_errors=True)
    log(f"Building {sha} ...")
    b = build(cfg, sha)
    logf = builds_dir(cfg) / sha / "build.log"
    if b is None:
        why = (builds_dir(cfg) / sha / "FAILED").read_text().strip()
        tail = "\n".join(logf.read_text(errors="replace").splitlines()[-30:]) if logf.exists() else ""
        raise SystemExit(f"{why}\n--- last lines of {logf} ---\n{tail}")
    log(f"Built {b.bin}")
    if cfg.check:
        log("check: " + ("passed" if check(cfg, b) else "FAILED"))
    for r in cfg.runs:
        cmd = _command(cfg, b, r.command)
        log(f"run {r.name!r}: {cmd}")
        p = subprocess.run(["bash", "-c", "time -p " + cmd], env=b.env(), cwd=cfg.here,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        log(f"  exit {p.returncode}; " + " ".join(l for l in p.stderr.splitlines() if l.startswith(("real", "user"))))
        if p.returncode:
            log("  " + "\n  ".join(p.stderr.strip().splitlines()[-10:]))


def cmd_doctor(a) -> None:
    from .profile import perf_usable
    ok = True

    def show(name, good, hint=""):
        nonlocal ok
        ok &= good or name.startswith("perf")
        print(f"  {'ok ' if good else 'no '} {name}" + ("" if good else f"   {hint}"))
    print(f"flamelapse {__version__}, Python {sys.version.split()[0]}")
    show("Linux", sys.platform.startswith("linux"), "Valgrind profiling needs Linux.")
    show("git", shutil.which("git") is not None, "install git")
    show("valgrind", shutil.which("valgrind") is not None, "install valgrind (dnf/apt install valgrind)")
    show("perf (optional, faster pinning)", perf_usable(),
         "install perf, or allow it: sudo sysctl kernel.perf_event_paranoid=2. Valgrind is used otherwise.")
    sys.exit(0 if ok else 1)


def cmd_quick(a) -> None:
    a.dir = a.dir or _name_from_repo(a.repo)
    cfg_path = Path(a.dir).resolve() / CONFIG_NAME
    if not cfg_path.exists() or a.force:
        cmd_init(a)
        if a.problems:
            raise SystemExit(1)
    a.config = str(cfg_path)
    a.open = True
    cmd_run(a)


# ---------------------------------------------------------------- argument parsing

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="flamelapse", description=__doc__)
    p.add_argument("--version", action="version", version=f"flamelapse {__version__}")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def init_args(s):
        s.add_argument("repo", help="path or URL of the git repository")
        s.add_argument("--run", action="append", metavar="CMD",
                       help='command to measure, e.g. "{bin} -c big.txt"; prefix with "Name=" to label it; repeatable')
        s.add_argument("--name", help="project name shown on the report")
        s.add_argument("--since", help="oldest version: tag, commit or YYYY-MM-DD")
        s.add_argument("--branch", default="HEAD")
        s.add_argument("--samples", type=int, default=60)
        s.add_argument("--jobs", type=int)
        s.add_argument("--build", help="build commands (skip detection)")
        s.add_argument("--binary", help="program path relative to the repository (skip discovery)")
        s.add_argument("--no-try", action="store_true", help="do not build once to find the program")
        s.add_argument("--force", action="store_true", help="overwrite an existing flamelapse.toml")

    s = sub.add_parser("init", help="create flamelapse.toml for a repository")
    init_args(s)
    s.add_argument("--dir", default=".", help="folder for flamelapse.toml and results (default: here)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("quick", help="init + run + open in one go")
    init_args(s)
    s.add_argument("--dir", help="workspace folder (default: ./<project name>)")
    s.add_argument("--no-pin", action="store_true")
    s.add_argument("--out")
    s.set_defaults(func=cmd_quick)

    s = sub.add_parser("run", help="build, profile and report (resumes where it stopped)")
    s.add_argument("config", nargs="?", help="flamelapse.toml or its folder (default: here)")
    s.add_argument("--samples", type=int)
    s.add_argument("--jobs", type=int)
    s.add_argument("--no-pin", action="store_true", help="skip pinning jumps to single commits")
    s.add_argument("--retry", action="store_true", help="try versions that failed before again")
    s.add_argument("--out", help="report folder (default: flamelapse-report next to the config)")
    s.add_argument("--open", action="store_true", help="open the report when done")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("report", help="rebuild the report from existing profiles")
    s.add_argument("config", nargs="?")
    s.add_argument("--out", help="report folder; point several projects at one folder to compare them")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("open", help="open the report in your browser")
    s.add_argument("config", nargs="?")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("serve", help="serve the report over http on localhost")
    s.add_argument("config", nargs="?")
    s.add_argument("--dir", help="report folder to serve instead")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("try", help="build one version and run it once, to debug the config")
    s.add_argument("config", nargs="?")
    s.add_argument("--rev", help="commit or tag (default: newest)")
    s.set_defaults(func=cmd_try)

    s = sub.add_parser("doctor", help="check that the tools flamelapse needs are installed")
    s.set_defaults(func=cmd_doctor)

    a = p.parse_args(argv)
    set_quiet(a.quiet)
    try:
        a.func(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        cmd = " ".join(map(str, e.cmd)) if isinstance(e.cmd, list) else e.cmd
        err = (e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")).strip()
        sys.exit(f"Command failed: {cmd}\n{err}")


if __name__ == "__main__":
    main()
