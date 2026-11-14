"""The private clone flamelapse works from, and commit selection."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config
from .util import log, run


def git(src: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True, text=True).stdout.strip()


def src_dir(cfg: Config) -> Path:
    return cfg.work / "src"


def sync(cfg: Config) -> Path:
    """Clone (or update) a private copy of the repository. The user's checkout is never touched."""
    src = src_dir(cfg)
    origin = cfg.repo
    local = Path(origin).expanduser()
    if not local.is_absolute():
        local = cfg.here / local          # relative paths are relative to flamelapse.toml
    if not re.match(r"^[\w+.-]+://|^[\w.-]+@", origin) and local.exists():
        origin = str(local.resolve())
    if not (src / ".git").exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        log(f"Cloning {cfg.repo}")
        # a local path is cloned with hardlinks: cheap, and safe if the original is later gc'd
        run(["git", "clone", "--quiet", "--no-checkout", origin, str(src)])
    else:
        run(["git", "-C", str(src), "fetch", "--quiet", "--tags", "origin"], check=False)
    return src


def resolve_branch(cfg: Config, src: Path) -> str:
    """The commit to walk back from: the configured branch, or origin's default branch."""
    b = cfg.branch
    for cand in ([b] if b != "HEAD" else []) + [f"origin/{b}" if b != "HEAD" else "origin/HEAD", "HEAD"]:
        try:
            return git(src, "rev-parse", "--verify", "--quiet", cand + "^{commit}")
        except subprocess.CalledProcessError:
            continue
    raise SystemExit(f"Cannot find branch {cfg.branch!r} in {cfg.repo}")


def mainline(cfg: Config, src: Path, tip: str) -> list[str]:
    """First-parent history from `since` (exclusive of older history) to tip, oldest first."""
    args = ["rev-list", "--first-parent", "--reverse"]
    if cfg.since and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg.since):
        args += [f"--since={cfg.since}", tip]
    elif cfg.since:
        base = git(src, "rev-parse", cfg.since + "^{commit}")
        args += [f"{base}^..{tip}"] if git(src, "rev-list", "--count", base) != "1" else [tip]
    else:
        args += [tip]
    return [s[:10] for s in git(src, *args).split()]


def tags(src: Path) -> dict[str, str]:
    out = {}
    for line in git(src, "tag", "--format=%(objectname:short=10) %(*objectname:short=10) %(refname:short)").splitlines():
        p = line.split()
        out[p[-2] if len(p) == 3 else p[0]] = p[-1]
    return out


def pick(cfg: Config, src: Path, line: list[str]) -> list[str]:
    """Evenly spaced samples along the mainline, plus every release tag on it, plus the newest commit."""
    if not line:
        return []
    n = max(2, cfg.samples)
    step = max(1, len(line) // n)
    chosen = set(line[::step]) | {line[0], line[-1]}
    on_line = set(line)
    chosen |= {sha for sha in tags(src) if sha in on_line}
    order = {s: i for i, s in enumerate(line)}
    return sorted(chosen, key=order.get)


def title(src: Path, sha: str) -> tuple[str, str]:
    """Commit subject, or the pull request title for GitHub merge commits."""
    subj, body = git(src, "log", "-1", "--format=%s%x00%b", sha).split("\x00", 1)
    m = re.match(r"Merge pull request (#\d+)", subj)
    if m:
        line = next((l.strip() for l in body.splitlines() if l.strip()), "")
        if line:
            return line, f"PR {m.group(1)}"
    return subj, ""
