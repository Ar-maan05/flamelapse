"""Loading and validating flamelapse.toml."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "flamelapse.toml"


class ConfigError(Exception):
    pass


@dataclass
class Run:
    name: str
    command: str
    show: str = ""              # how the command is written on the report (default: the command itself)

    def display(self, binary: str) -> str:
        """The command as a person would type it: program name instead of {bin}, paths relative."""
        if self.show:
            return self.show
        prog = Path(binary).name or "program"
        return (self.command.replace("{bin}", prog).replace("{plainbin}", prog)
                .replace("{here}/", "").replace("{here}", "."))


@dataclass
class Rule:
    label: str
    match: re.Pattern
    where: str = "any"          # "leaf": only the innermost function; "any": anywhere on the stack
    description: str = ""


@dataclass
class Config:
    path: Path                   # the flamelapse.toml file
    name: str
    repo: str                    # local path or URL
    branch: str = "HEAD"
    since: str | None = None     # tag, commit or YYYY-MM-DD
    samples: int = 60
    jobs: int = 3
    threshold: float = 0.02      # jumps smaller than this are not pinned
    build_command: str = ""
    binary: str = ""
    build_env: dict = field(default_factory=dict)
    check: str | None = None
    runs: list[Run] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    group_by: str = "auto"       # how uncategorised work is grouped: auto | run | file
    builtin_rules: bool = True
    notes: dict = field(default_factory=dict)
    report_intro: str = ""
    task: str = "this task"

    @property
    def here(self) -> Path:
        return self.path.parent

    @property
    def work(self) -> Path:
        return self.here / ".flamelapse"

    @property
    def report_dir(self) -> Path:
        return self.here / "flamelapse-report"

    @property
    def key(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", self.name).strip("-") or "project"

    def repo_url_for_links(self) -> str | None:
        """https URL of the repository, used to link commits in the report."""
        r = self.repo
        m = re.match(r"^(?:git@|ssh://git@)github\.com[:/](.+?)(?:\.git)?/?$", r)
        if m:
            return f"https://github.com/{m.group(1)}"
        if r.startswith(("https://", "http://")):
            return re.sub(r"\.git/?$", "", r.rstrip("/"))
        return None


def find_config(start: str | os.PathLike | None) -> Path:
    p = Path(start or ".").resolve()
    if p.is_dir():
        p = p / CONFIG_NAME
    if not p.exists():
        raise ConfigError(f"No {CONFIG_NAME} found at {p}. Run `flamelapse init <repo>` first.")
    return p


def load(path: str | os.PathLike | None = None) -> Config:
    p = find_config(path)
    try:
        raw = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from None

    def need(table, key, where):
        if key not in table or table[key] in ("", None):
            raise ConfigError(f"{p}: missing `{key}` in {where}")
        return table[key]

    build = raw.get("build", {})
    cfg = Config(
        path=p,
        name=need(raw, "name", "the top level"),
        repo=need(raw, "repo", "the top level"),
        branch=raw.get("branch", "HEAD"),
        since=raw.get("since"),
        samples=int(raw.get("samples", 60)),
        jobs=int(raw.get("jobs", 3)),
        threshold=float(raw.get("threshold", 0.02)),
        build_command=need(build, "command", "[build]"),
        binary=need(build, "binary", "[build]"),
        build_env={k: str(v) for k, v in build.get("env", {}).items()},
        check=raw.get("check"),
        group_by=raw.get("group_by", "auto"),
        builtin_rules=bool(raw.get("builtin_rules", True)),
        notes={str(k): str(v) for k, v in raw.get("notes", {}).items()},
        report_intro=raw.get("report", {}).get("intro", ""),
        task=raw.get("report", {}).get("task", "this task"),
    )
    runs = raw.get("run", [])
    if not runs:
        raise ConfigError(f"{p}: add at least one [[run]] with a `command` that uses {{bin}}")
    names = set()
    for i, r in enumerate(runs):
        cmd = need(r, "command", f"[[run]] #{i + 1}")
        name = r.get("name") or (f"Run {i + 1}" if len(runs) > 1 else "main")
        if name in names:
            raise ConfigError(f"{p}: two runs are both called {name!r}")
        names.add(name)
        cfg.runs.append(Run(name=name, command=cmd, show=r.get("show", "")))
    for i, c in enumerate(raw.get("category", [])):
        where = c.get("where", "any")
        if where not in ("any", "leaf"):
            raise ConfigError(f"{p}: [[category]] #{i + 1}: `where` must be \"any\" or \"leaf\"")
        try:
            rx = re.compile(need(c, "match", f"[[category]] #{i + 1}"))
        except re.error as e:
            raise ConfigError(f"{p}: [[category]] #{i + 1}: bad regex: {e}") from None
        cfg.rules.append(Rule(label=need(c, "label", f"[[category]] #{i + 1}"), match=rx, where=where,
                              description=c.get("description", "")))
    if cfg.group_by not in ("auto", "run", "file"):
        raise ConfigError(f"{p}: `group_by` must be auto, run or file")
    return cfg
