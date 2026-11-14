"""Build one commit in a throwaway worktree and keep only what is needed to run it."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .gitutil import src_dir



COMPILERS = ("cc", "gcc", "clang", "c++", "g++", "clang++")


def _cpu_has_avx512() -> bool:
    try:
        return " avx512f" in Path("/proc/cpuinfo").read_text()
    except OSError:
        return False


def build_environment(work: Path, extra: dict) -> dict:
    """The environment builds run in. Valgrind cannot run AVX-512 instructions, which
    -march=native emits on CPUs that have them, so on such CPUs every C/C++ compiler call goes
    through a wrapper that appends -mno-avx512f (a no-op for builds that do not use it)."""
    env = dict(os.environ)
    env.update(extra)
    if _cpu_has_avx512():
        shims = work / "compiler-shims"
        if not shims.is_dir():
            tmp = work / f".compiler-shims.{os.getpid()}"
            tmp.mkdir(parents=True, exist_ok=True)
            for name in COMPILERS:
                real = shutil.which(name)
                if real:
                    f = tmp / name
                    # take this folder off PATH first, so wrappers like ccache cannot find us again
                    f.write_text("#!/bin/sh\n"
                                 f"PATH=$(printf %s \"$PATH\" | tr : '\\n' | grep -vxF {shlex.quote(str(shims))} | paste -sd:)\n"
                                 f'export PATH\nexec {shlex.quote(real)} "$@" -mno-avx512f\n')
                    f.chmod(0o755)
            try:
                tmp.rename(shims)
            except OSError:            # another build got there first
                shutil.rmtree(tmp, ignore_errors=True)
        env["PATH"] = f"{shims}:{env.get('PATH', '')}"
    return env


def build_key(cfg: Config) -> str:
    blob = json.dumps([cfg.build_command, cfg.binary, sorted(cfg.build_env.items())])
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


def builds_dir(cfg: Config) -> Path:
    return cfg.work / "builds" / build_key(cfg)


class Built:
    def __init__(self, root: Path):
        self.root = root
        meta = json.loads((root / "build.json").read_text())
        self.bin = root / meta["bin"]
        self.libdir = root / "lib"

    def env(self) -> dict:
        e = dict(os.environ)
        if self.libdir.is_dir():
            e["LD_LIBRARY_PATH"] = f"{self.libdir}:{e.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        return e


def _is_libtool_wrapper(p: Path) -> bool:
    try:
        head = p.read_bytes()[:4096]
    except OSError:
        return False
    return head.startswith(b"#!") and b"libtool" in head


def _real_binary(wt: Path, rel: str) -> Path | None:
    p = (wt / rel)
    if not p.exists():
        # Programs move around between versions (e.g. build/minigzip vs build/test/minigzip):
        # fall back to the closest executable with the same name.
        same = [c for c in wt.rglob(Path(rel).name) if c.is_file() and os.access(c, os.X_OK) and ".git" not in c.parts]
        if same:
            p = min(same, key=lambda c: (len(c.relative_to(wt).parts), str(c)))
    if p.is_file() and _is_libtool_wrapper(p):
        for cand in (p.parent / ".libs" / p.name, p.parent / ".libs" / f"lt-{p.name}"):
            if cand.is_file():
                return cand
    return p if p.is_file() and os.access(p, os.X_OK) else None


def _copy_tree_libs(binary: Path, wt: Path, dest: Path) -> None:
    """Copy shared libraries the binary loads from inside the build tree (e.g. libjq.so)."""
    search = sorted({str(p.parent) for p in wt.rglob("*.so*") if p.is_file()})
    env = dict(os.environ, LD_LIBRARY_PATH=":".join(search))
    try:
        out = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, env=env).stdout
    except FileNotFoundError:
        return
    wt_s = str(wt.resolve())
    for m in re.finditer(r"=>\s+(\S+)", out):
        lib = Path(m.group(1))
        if str(lib.resolve()).startswith(wt_s):
            dest.mkdir(exist_ok=True)
            shutil.copy2(lib.resolve(), dest / lib.name)


_ERR = re.compile(r"(fatal error|error:|error\b.*\bnot found|No such file|command not found|undefined reference|CMake Error|Error \d+|cannot find)", re.I)


def first_error(log: Path) -> str:
    """The line of a build log most likely to explain the failure."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for l in lines:
        if _ERR.search(l) and not re.search(r"Waiting for unfinished|warning:", l):
            return l.strip()[:200]
    return lines[-1].strip()[:200] if lines else ""


def failure(cfg: Config, sha: str) -> str:
    f = builds_dir(cfg) / sha / "FAILED"
    return f.read_text().strip() if f.exists() else ""


def build(cfg: Config, sha: str) -> Built | None:
    """Return the cached build of `sha`, building it if needed. None if it does not build."""
    out = builds_dir(cfg) / sha
    if (out / "build.json").exists():
        return Built(out)
    if (out / "FAILED").exists():
        return None
    out.mkdir(parents=True, exist_ok=True)
    src = src_dir(cfg)
    wt = cfg.work / "worktrees" / sha
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", str(src), "worktree", "prune"], check=False)
    try:
        subprocess.run(["git", "-C", str(src), "worktree", "add", "--quiet", "--detach", str(wt), sha],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(wt), "submodule", "update", "--init", "--recursive", "--quiet"],
                       capture_output=True, check=False)
        env = build_environment(cfg.work, cfg.build_env)
        with open(out / "build.log", "w") as logf:
            ok = subprocess.run(["bash", "-c", cfg.build_command], cwd=wt, env=env,
                                stdout=logf, stderr=subprocess.STDOUT).returncode == 0
        binary = _real_binary(wt, cfg.binary) if ok else None
        if binary is None:
            why = (f"build failed: {first_error(out / 'build.log')}" if not ok
                   else f"built, but {cfg.binary} was not produced")
            (out / "FAILED").write_text(why + "\n")
            return None
        (out / "bin").mkdir(exist_ok=True)
        dest = out / "bin" / Path(cfg.binary).name
        shutil.copy2(binary, dest)
        _copy_tree_libs(binary, wt, out / "lib")
        (out / "build.json").write_text(json.dumps({"bin": f"bin/{dest.name}", "sha": sha}))
        return Built(out)
    finally:
        subprocess.run(["git", "-C", str(src), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, check=False)
        shutil.rmtree(wt, ignore_errors=True)
