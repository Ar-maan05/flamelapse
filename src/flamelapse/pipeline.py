"""The whole run: sample commits, build, profile, pin every jump to one commit."""
from __future__ import annotations

import collections
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitutil
from .build import build, builds_dir, failure
from .config import Config
from .profile import check, check_why, frames_dir, measure, perf_usable, profile, profile_key
from .util import log


def _profiled(cfg: Config) -> set[str]:
    d = frames_dir(cfg)
    return {p.stem for p in d.glob("*.json")} if d.exists() else set()


def _fail_file(cfg: Config) -> Path:
    return cfg.work / f"failed-{profile_key(cfg)}.json"


def _load_fails(cfg: Config) -> dict:
    f = _fail_file(cfg)
    return json.loads(f.read_text()) if f.exists() else {}


def profile_commit(cfg: Config, sha: str) -> tuple[str, str]:
    """Build, check and profile one commit. Returns (sha, status)."""
    if sha in _profiled(cfg):
        return sha, "cached"
    b = build(cfg, sha)
    if b is None:
        return sha, failure(cfg, sha) or "build failed"
    why = check_why(cfg, b)
    if why is not None:
        return sha, f"check failed: {why}"
    why = profile(cfg, b, sha)
    return sha, why or "ok"


def _parallel(cfg: Config, shas: list[str], label: str) -> dict[str, str]:
    fails = _load_fails(cfg)
    todo = [s for s in shas if s not in fails]
    results = {}
    if not todo:
        return results
    width = len(str(len(todo)))
    with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as ex:
        for k, (sha, status) in enumerate(ex.map(lambda s: profile_commit(cfg, s), todo), 1):
            results[sha] = status
            if status not in ("ok", "cached"):
                fails[sha] = status
            if status != "cached":
                log(f"  [{k:>{width}}/{len(todo)}] {label} {sha}  {_short(status)}")
    _fail_file(cfg).write_text(json.dumps(fails, indent=1))
    return results


def _short(status: str, n: int = 90) -> str:
    return status if len(status) <= n else status[:n - 3] + "..."


def summarize_failures(cfg: Config, shas: list[str]) -> None:
    """Group the failures among `shas` by reason, most common first."""
    fails = _load_fails(cfg)
    by = collections.defaultdict(list)
    for s in shas:
        if s in fails:
            # the same error at a different line is the same reason
            by[re.sub(r":\d+(:\d+)?:", ":", fails[s])].append(s)
    if not by:
        return
    log(f"{sum(map(len, by.values()))} versions were skipped:")
    for why, group in sorted(by.items(), key=lambda kv: -len(kv[1]))[:4]:
        log(f"  {len(group):>3} x {_short(why, 110)}")
        if why.startswith("build failed"):
            log(f"        log: {builds_dir(cfg) / group[-1] / 'build.log'}")
    if len(by) > 4:
        log(f"  ... and {len(by) - 4} other reasons ({_fail_file(cfg)})")


def _total(cfg: Config, sha: str) -> int:
    p = json.loads((frames_dir(cfg) / f"{sha}.json").read_text())
    return sum(p["runs"].values())


def culprit(between: list[str], cache: dict, jump: float) -> tuple[str, str] | None:
    """(parent, commit) of the biggest single-commit step in the same direction as `jump`.
    `between` is consecutive first-parent commits; `cache` maps commit -> total (None = no result)."""
    sign = 1 if jump > 0 else -1
    best = None
    for x, y in zip(between, between[1:]):
        if cache.get(x) and cache.get(y):
            step = (cache[y] - cache[x]) / cache[x] * sign
            if step > 0 and (best is None or step > best[0]):
                best = (step, x, y)
    return (best[1], best[2]) if best else None


def refine(cfg: Config, line: list[str]) -> list[str]:
    """For each jump between neighbouring profiled versions, measure every commit in between
    and profile the commit (and its parent) that made the biggest single step."""
    pos = {s: i for i, s in enumerate(line)}
    method = "perf" if perf_usable() else "valgrind"
    cache_f = cfg.work / f"measure-{profile_key(cfg)}-{method}.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}

    def measure_one(sha):
        if sha in cache:
            return sha, cache[sha]
        b = build(cfg, sha)
        v = measure(cfg, b) if b and check(cfg, b) else None
        return sha, v

    pinned = []
    frames = sorted((s for s in _profiled(cfg) if s in pos), key=lambda s: pos[s])
    jumps = []
    for a, b in zip(frames, frames[1:]):
        ta, tb = _total(cfg, a), _total(cfg, b)
        if ta and abs(tb - ta) / ta >= cfg.threshold and pos[b] - pos[a] >= 2:
            jumps.append((a, b, (tb - ta) / ta))
    if not jumps:
        return pinned
    log(f"Pinning {len(jumps)} jump{'s' if len(jumps) > 1 else ''} to single commits (measuring with {method})")
    for a, b, r in jumps:
        between = line[pos[a]:pos[b] + 1]
        with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as ex:
            for sha, v in ex.map(measure_one, between):
                cache[sha] = v
        cache_f.write_text(json.dumps(cache))
        found = culprit(between, cache, r)
        if not found:
            log(f"  {a}..{b} {r:+.1%}: could not pin to one commit (a commit in between does not build)")
            continue
        parent, culprit_sha = found
        subj, pr = gitutil.title(gitutil.src_dir(cfg), culprit_sha)
        step = (cache[culprit_sha] - cache[parent]) / cache[parent]
        log(f"  {a}..{b} {r:+.1%}  ->  {culprit_sha} {step:+.1%}  {(pr + ' ') if pr else ''}{subj[:70]}")
        pinned += [parent, culprit_sha]
    _parallel(cfg, pinned, "pin")
    return pinned


def forget_failures(cfg: Config) -> int:
    """Let versions that failed before be tried again (after installing a missing library, say)."""
    n = 0
    for f in builds_dir(cfg).glob("*/FAILED") if builds_dir(cfg).exists() else []:
        f.unlink()
        n += 1
    _fail_file(cfg).unlink(missing_ok=True)
    for m in cfg.work.glob(f"measure-{profile_key(cfg)}-*.json"):
        cache = json.loads(m.read_text())
        m.write_text(json.dumps({k: v for k, v in cache.items() if v}))
    return n


def run_all(cfg: Config, do_refine: bool = True, retry: bool = False) -> None:
    if retry:
        log(f"Forgetting earlier failures ({forget_failures(cfg)} failed builds will be retried)")
    src = gitutil.sync(cfg)
    tip = gitutil.resolve_branch(cfg, src)
    line = gitutil.mainline(cfg, src, tip)
    shas = gitutil.pick(cfg, src, line)
    have = _profiled(cfg)
    if shas and shas[-1] not in have:
        # Try the newest version first: if it fails, every other one probably will too.
        log(f"Building and profiling the newest version ({shas[-1]}) first...")
        _, status = profile_commit(cfg, shas[-1])
        if status not in ("ok", "cached"):
            raise SystemExit(f"The newest version failed: {status}\n"
                             f"Fix the config, then check it with `flamelapse try`.")
        have = _profiled(cfg)
    log(f"{cfg.name}: {len(line)} commits on the main line, sampling {len(shas)} "
        f"({len([s for s in shas if s in have])} already done), {cfg.jobs} at a time")
    _parallel(cfg, shas, "build+profile")
    ok = len(_profiled(cfg) & set(shas))
    log(f"Profiled {ok} of {len(shas)} sampled versions")
    summarize_failures(cfg, shas)
    if ok < 2:
        raise SystemExit("Fewer than two versions could be profiled. Check the build command and binary path "
                         "with `flamelapse try`.")
    if do_refine:
        refine(cfg, line)
