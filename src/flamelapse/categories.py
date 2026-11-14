"""Grouping work into a handful of plain-language kinds, with no configuration needed.

Order of precedence for each sampled stack:
  1. the project's own [[category]] rules, in the order written
  2. built-in kinds every native program has (memory, file I/O, loading)
  3. a fallback: by run (when there are several runs) or by source file
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass

from .config import Config, Rule

BUILTIN = [
    Rule("Memory", re.compile(
        r"^(malloc|free|calloc|realloc|cfree|_int_\w+|sysmalloc|malloc_\w+|tcache\w*|unlink_chunk|"
        r"__libc_(malloc|free|calloc|realloc)\w*|operator new.*|operator delete.*|je_\w+|mi_\w+|"
        r"__rust_(alloc|dealloc|realloc)\w*|__rdl_\w+)$"), "leaf",
        "Asking the system for memory and giving it back."),
    Rule("Reading and writing files", re.compile(
        r"^(fread|fwrite|fflush|fclose|fopen\w*|fgets|getline|getc|fputs|fputc|_IO_\w+|__GI__IO_\w+|"
        r"__libc_read|__libc_write|read|write|__read\w*|__write\w*|pread\w*|pwrite\w*)$"), "any",
        "Moving data between files and the program."),
    Rule("Loading the program", re.compile(r"^(_dl_\w+|dl_main|do_lookup_x|check_match|_dl_runtime_resolve\w*)$"), "any",
         "The system linker loading the program and its libraries."),
]
MAX_FILES = 6
OTHER = "Other code"


@dataclass
class Kind:
    key: str
    label: str
    desc: str


SYSTEM = "@system"


GENERIC_NAMES = {"mod.rs", "lib.rs", "main.rs", "index.js", "index.ts", "__init__.py", "main.go", "main.c", "main.cpp"}


def _short_path(path: str) -> str:
    """A grouping key for a source file: repository-relative for the project's own code,
    one key per dependency crate, and one for system code (the C library and friends)."""
    m = re.search(r"/worktrees/[0-9a-f]+/(.+)$", path)
    if m:
        return m.group(1)
    m = re.search(r"^/rustc/[0-9a-f]+/library/(\w+)/src/(.+)$", path)
    if m:
        return f"@rust:{m.group(1)}/{m.group(2)}"
    m = re.search(r"/\.cargo/(?:registry/src/[^/]+|git/checkouts)/([A-Za-z0-9_-]+?)-(?:\d|[0-9a-f]{7})[^/]*/", path)
    if m:
        return f"@crate:{m.group(1)}"
    return SYSTEM if path.startswith(("/usr/", "/lib", "/opt/")) or "/glibc-" in path or "/sysdeps/" in path else path


def _kind_for(key: str, i: int, taken: list[str]) -> Kind:
    """A plain-language name for a group of files."""
    if key == SYSTEM:
        return Kind(f"g{i}", "System libraries", "The C library and other system code: copying memory, string handling and so on.")
    if key.startswith("@crate:"):
        name = key[7:]
        return Kind(f"g{i}", f"{name} (crate)", f"Code in {name}, a library this project depends on.")
    if key.startswith("@rust:"):
        crate, _, rest = key[6:].partition("/")
        parts = rest.split("/")
        if parts[-1] in ("mod.rs", "lib.rs"):
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].removesuffix(".rs")
        where = "/".join(parts[-2:]) or crate
        return Kind(f"g{i}", f"Rust {crate}: {where}", f"Code in Rust's standard library: {crate}/src/{rest}.")
    parts = key.split("/")
    label = parts[-1]
    if (label in GENERIC_NAMES or any(t.rsplit("/", 1)[-1] == label for t in taken if t != key)) and len(parts) > 1:
        label = "/".join(parts[-2:])
    return Kind(f"g{i}", label, f"Code in {key}.")


class Classifier:
    def __init__(self, cfg: Config, profiles: list[dict]):
        self.rules = list(cfg.rules) + (BUILTIN if cfg.builtin_rules else [])
        self.leaf_rules = [(i, r) for i, r in enumerate(self.rules) if r.where == "leaf"]
        self.any_rules = [(i, r) for i, r in enumerate(self.rules) if r.where == "any"]
        mode = cfg.group_by
        if mode == "auto":
            mode = "run" if len(cfg.runs) > 1 else "file"
        self.mode = mode
        kinds = [Kind(f"r{i}", r.label, r.description) for i, r in enumerate(self.rules)]
        if mode == "run":
            self.groups = {r.name: Kind(f"g{i}", r.name, f"Everything else done by: {r.display(cfg.binary)}")
                           for i, r in enumerate(cfg.runs)}
        else:
            per_file = collections.Counter()
            for p in profiles:
                for stack, v in p["stacks"].items():
                    fr = stack.split(";")
                    if self._rule(fr[1:]) is None:
                        per_file[self._file(fr[-1], p["files"])] += v
            top = [f for f, _ in per_file.most_common() if f][:MAX_FILES]
            self.groups = {f: _kind_for(f, i, top) for i, f in enumerate(top)}
        self.other = Kind("other", OTHER, "Everything else, including libraries without debug information.")
        self.kinds = kinds + list(self.groups.values()) + [self.other]
        self._cache: dict = {}

    @staticmethod
    def _file(fn: str, files: dict) -> str:
        f = files.get(fn, "")
        return "" if not f or f == "???" else _short_path(f)

    def _rule(self, frames: list[str]) -> int | None:
        if frames:
            leaf = frames[-1]
            for i, r in self.leaf_rules:
                if r.match.match(leaf):
                    return i
        for i, r in self.any_rules:
            if any(r.match.match(f) for f in frames):
                return i
        return None

    def classify(self, stack: str, files: dict) -> str:
        fr = stack.split(";")
        i = self._rule(fr[1:])
        if i is not None:
            return f"r{i}"
        if self.mode == "run":
            g = self.groups.get(fr[0])
        else:
            g = self.groups.get(self._file(fr[-1], files))
        return g.key if g else "other"
