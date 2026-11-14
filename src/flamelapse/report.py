"""Turning profiles into the static report folder (open index.html, no server needed)."""
from __future__ import annotations

import collections
import json
import re
import shutil
from importlib import resources
from pathlib import Path

from . import gitutil
from .categories import Classifier
from .config import Config
from .profile import frames_dir

MIN_FRAC = 0.002     # functions below 0.2% of every version are left out of the flame view


def _workload_text(cfg: Config) -> str:
    lines = []
    for r in cfg.runs:
        cmd = r.display(cfg.binary)
        lines.append(cmd if len(cfg.runs) == 1 else f"{r.name}:\n  {cmd}")
    return "\n".join(lines)


def build(cfg: Config, out: Path | None = None) -> Path:
    out = out or cfg.report_dir
    src = gitutil.src_dir(cfg)
    prof_dir = frames_dir(cfg)
    shas = [p.stem for p in prof_dir.glob("*.json")] if prof_dir.exists() else []
    if not shas:
        raise SystemExit("Nothing profiled yet. Run `flamelapse run` first.")
    tip = gitutil.resolve_branch(cfg, src)
    order = {s: i for i, s in enumerate(gitutil.mainline(cfg, src, tip))}
    tagmap = gitutil.tags(src)
    profiles = {s: json.loads((prof_dir / f"{s}.json").read_text()) for s in shas}

    frames = []
    for sha in shas:
        subj, pr = gitutil.title(src, sha)
        frames.append({"sha": sha, "date": gitutil.git(src, "log", "-1", "--format=%cI", sha),
                       "subject": subj, "pr": pr, "tag": tagmap.get(sha, "")})
    frames.sort(key=lambda f: (order.get(f["sha"], 10**9), f["date"]))
    for a, b in zip(frames, frames[1:]):
        b["direct"] = gitutil.git(src, "rev-parse", "--short=10", b["sha"] + "^") == a["sha"]

    clf = Classifier(cfg, [profiles[f["sha"]] for f in frames])
    keys = [k.key for k in clf.kinds]
    single = len(cfg.runs) == 1
    n = len(frames)
    incl = collections.defaultdict(lambda: [0] * n)
    for i, f in enumerate(frames):
        p = profiles[f["sha"]]
        cats = collections.Counter()
        total = 0
        for stack, v in p["stacks"].items():
            total += v
            cats[clf.classify(stack, p["files"])] += v
            path = stack.split(";")
            if single:
                path = path[1:]
            for d in range(1, len(path) + 1):
                incl[tuple(path[:d])][i] += v
        f["total"] = total
        f["cats"] = [cats[k] for k in keys]
        note = next((v for k, v in cfg.notes.items() if f["sha"].startswith(k[:10]) or k.startswith(f["sha"])), None)
        if note:
            f["note"] = note

    # Biggest kinds first (bottom of the stack, first colours); at most six named kinds, the rest fold into Other.
    totals = collections.Counter()
    for f in frames:
        for k, v in zip(keys, f["cats"]):
            totals[k] += v
    visible = [k for k in keys if k != "other" and any(f["cats"][keys.index(k)] >= 0.005 * f["total"] for f in frames)]
    visible.sort(key=lambda k: -totals[k])
    named, folded = visible[:6], set(keys) - set(visible[:6])
    order = named + ["other"]
    kinds_by_key = {k.key: k for k in clf.kinds}
    for f in frames:
        by = dict(zip(keys, f["cats"]))
        f["cats"] = [by[k] for k in named] + [sum(by[k] for k in folded)]
    kinds = [kinds_by_key[k] for k in order]

    keep = [k for k in incl if any(incl[k][i] >= MIN_FRAC * frames[i]["total"] for i in range(n))]
    idx, nodes = {}, []
    for k in sorted(keep, key=lambda k: (len(k), k)):
        if len(k) > 1 and k[:-1] not in idx:
            continue
        idx[k] = len(nodes)
        nodes.append([k[-1], idx[k[:-1]] if len(k) > 1 else -1, incl[k]])

    intro = cfg.report_intro or (
        "Every version was built from source and run with this command:" if single
        else f"Every version was built from source and run {len(cfg.runs)} times:")
    data = {
        "project": cfg.name,
        "about": {"repo": cfg.repo_url_for_links(), "task": cfg.task, "workload_intro": intro},
        "workload": _workload_text(cfg),
        "cats": [{"key": k.key, "label": k.label, "desc": k.desc} for k in kinds],
        "frames": frames,
        "nodes": nodes,
    }
    out.mkdir(parents=True, exist_ok=True)
    viewer = resources.files("flamelapse") / "viewer"
    for name in ("index.html", "flame.html"):
        (out / name).write_text((viewer / name).read_text())
    (out / f"data-{cfg.key}.js").write_text("window.FLAMELAPSE=" + json.dumps(data, separators=(",", ":")) + ";\n")
    _write_index(out)
    return out


def _write_index(out: Path) -> None:
    """projects.js lists every project in this report folder, for the switcher."""
    projects = []
    for f in sorted(out.glob("data-*.js")):
        m = re.search(r'"project":"((?:[^"\\]|\\.)*)"', f.read_text()[:2000])
        projects.append({"key": f.stem[5:], "name": json.loads(f'"{m.group(1)}"') if m else f.stem[5:]})
    (out / "projects.js").write_text("window.FLAMELAPSE_PROJECTS=" + json.dumps(projects) + ";\n")


def copy_into(src_report: Path, dest: Path) -> None:
    """Merge one report folder into another (to show several projects in one place)."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in src_report.glob("data-*.js"):
        shutil.copy2(f, dest / f.name)
    _write_index(dest)
