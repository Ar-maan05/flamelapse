"""Guessing how to build a project, and which program it produces."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .build import build_environment
from .util import fill

FP = "-fno-omit-frame-pointer"


@dataclass
class BuildGuess:
    system: str
    command: str
    env: dict = field(default_factory=dict)
    note: str = ""


def guess(files: set[str]) -> BuildGuess | None:
    """`files` is the set of top-level names in the repository."""
    if files & {"configure.ac", "configure.in"} or "autogen.sh" in files:
        return BuildGuess("autotools",
                          "[ -x configure ] || autoreconf -fi\n./configure --disable-dependency-tracking\nmake -j$(nproc)",
                          {"CFLAGS": f"-O2 -g {FP}", "CXXFLAGS": f"-O2 -g {FP}"},
                          "autoconf's usual default is -O2 -g; frame pointers are added for clearer profiles.")
    if "CMakeLists.txt" in files:
        return BuildGuess("cmake",
                          "cmake -S . -B _fl_build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_TESTING=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5\n"
                          "cmake --build _fl_build -j$(nproc)",
                          {"CFLAGS": FP, "CXXFLAGS": FP},
                          "RelWithDebInfo is -O2 -g.")
    if "meson.build" in files:
        return BuildGuess("meson",
                          "meson setup _fl_build --buildtype=debugoptimized\nninja -C _fl_build",
                          {"CFLAGS": FP, "CXXFLAGS": FP},
                          "debugoptimized is -O2 -g.")
    if "Cargo.toml" in files:
        return BuildGuess("cargo", "cargo build --release",
                          {"CARGO_PROFILE_RELEASE_DEBUG": "true", "RUSTFLAGS": "-C force-frame-pointers=yes"},
                          "Release build with debug info.")
    if "go.mod" in files:
        return BuildGuess("go", "mkdir -p _fl_build && go build -o _fl_build/ ./...", {},
                          "Every main package is built into _fl_build/.")
    if files & {"Makefile", "makefile", "GNUmakefile"}:
        return BuildGuess("make", "make -j$(nproc)", {},
                          "Uses the project's own flags. If profiles show addresses instead of function "
                          "names, add debug info here, for example CFLAGS = \"-O2 -g\".")
    return None


def _is_elf_exe(p: Path) -> bool:
    try:
        if not p.is_file() or p.is_symlink() or not os.access(p, os.X_OK):
            return False
        with open(p, "rb") as f:
            head = f.read(18)
        return head[:4] == b"\x7fELF" and head[16] == 2      # ET_EXEC: not a shared object
    except OSError:
        return False


def _is_elf_any(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(18)
        return head[:4] == b"\x7fELF" and head[16] in (2, 3) and os.access(p, os.X_OK) and ".so" not in p.name
    except OSError:
        return False


@dataclass
class Discovery:
    built: bool
    programs: list[str]                       # best guess first
    log_tail: str
    runs: list[tuple[str, int, float, str]] = field(default_factory=list)   # name, exit status, seconds, stderr tail


def discover(src: Path, sha: str, g: BuildGuess, work: Path, name_hint: str,
             binary: str = "", runs: list[tuple[str, str]] = (), here: Path | None = None) -> Discovery:
    """Build `sha` once, list the programs it produced, and try each run command once
    (natively, not under valgrind) with the chosen program."""
    wt = Path(tempfile.mkdtemp(prefix="discover-", dir=work))
    shutil.rmtree(wt)
    try:
        subprocess.run(["git", "-C", str(src), "worktree", "add", "--quiet", "--detach", str(wt), sha],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(wt), "submodule", "update", "--init", "--recursive", "--quiet"],
                       capture_output=True)
        start = time.time() - 1
        env = build_environment(work, g.env)
        p = subprocess.run(["bash", "-c", g.command], cwd=wt, env=env, capture_output=True, text=True)
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-25:])
        found = []
        for f in wt.rglob("*"):
            rel_parts = f.relative_to(wt).parts
            if (any(part.startswith(".") for part in rel_parts[:-1]) or "CMakeFiles" in rel_parts
                    or "deps" in rel_parts or f.name.startswith(("build-script-", "build_script_"))):
                continue
            if (_is_elf_exe(f) or _is_elf_any(f)) and f.stat().st_mtime >= start:
                found.append(f.relative_to(wt))
            elif f.is_file() and f.suffix == "" and os.access(f, os.X_OK) and f.stat().st_mtime >= start:
                try:
                    if b"libtool" in f.read_bytes()[:4096]:
                        found.append(f.relative_to(wt))
                except OSError:
                    pass
        hint = name_hint.lower()

        def rank(rel: Path):
            n = rel.name.lower()
            testy = any(t in n for t in ("test", "bench", "example", "fuzz", "demo", "conftest", "cover"))
            return (n != hint, testy, hint not in n, len(rel.parts), str(rel))

        found = [str(f) for f in sorted(set(found), key=rank)]
        d = Discovery(p.returncode == 0, found, tail)
        prog = binary or (found[0] if found else "")
        if prog and (wt / prog).exists():
            for name, cmd in runs:
                c = fill(cmd, bin=wt / prog, plainbin=wt / prog, here=here or work.parent, build=wt)
                t = time.time()
                try:
                    r = subprocess.run(["bash", "-c", c], cwd=here or work.parent, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                       errors="replace", timeout=120)
                    d.runs.append((name, r.returncode, time.time() - t, "\n".join(r.stderr.strip().splitlines()[-4:])))
                except subprocess.TimeoutExpired:
                    d.runs.append((name, -1, 120.0, "still running after 2 minutes"))
        return d
    finally:
        subprocess.run(["git", "-C", str(src), "worktree", "remove", "--force", str(wt)], capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)
