"""Fast tests: no compiler or valgrind needed."""
import collections
import re
import subprocess
from pathlib import Path

import pytest

from flamelapse import gitutil
from flamelapse.categories import Classifier
from flamelapse.config import Config, ConfigError, Run, Rule, load
from flamelapse.pipeline import culprit
from flamelapse.profile import fold, parse_callgrind, profile_key
from flamelapse.util import fill


# ---------------------------------------------------------------- util

def test_fill_quotes_values_and_leaves_other_braces():
    out = fill("{bin} -e 'local t={}' {here}/in put.txt", bin=Path("/a b/prog"), here=Path("/w"))
    assert out == "'/a b/prog' -e 'local t={}' /w/in put.txt"


# ---------------------------------------------------------------- config

def write(tmp_path, text):
    (tmp_path / "flamelapse.toml").write_text(text)
    return tmp_path


BASIC = '''
name = "demo"
repo = "https://github.com/example/demo.git"
[build]
command = "make"
binary = "demo"
[[run]]
command = "{bin} --go"
'''


def test_load_defaults(tmp_path):
    cfg = load(write(tmp_path, BASIC))
    assert cfg.runs == [Run("main", "{bin} --go")]
    assert cfg.samples == 60 and cfg.threshold == 0.02
    assert cfg.repo_url_for_links() == "https://github.com/example/demo"
    assert cfg.work == tmp_path / ".flamelapse"


@pytest.mark.parametrize("bad, msg", [
    (BASIC.replace('binary = "demo"', ""), "missing `binary`"),
    (BASIC.split("[[run]]")[0], "at least one [[run]]"),
    (BASIC + '[[category]]\nlabel = "x"\nmatch = "("\n', "bad regex"),
    (BASIC + '[[category]]\nlabel = "x"\nmatch = "a"\nwhere = "top"\n', "`where`"),
    (BASIC + '[[run]]\nname = "Run 1"\ncommand = "{bin}"\n', "both called"),
    ("name = ", "flamelapse.toml"),
])
def test_load_errors_are_readable(tmp_path, bad, msg):
    with pytest.raises(ConfigError, match=re.escape(msg)):
        load(write(tmp_path, bad))


def test_missing_config_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="flamelapse init"):
        load(tmp_path)


def test_ssh_repo_links_to_github():
    cfg = Config(path=Path("/x/flamelapse.toml"), name="n", repo="git@github.com:o/r.git")
    assert cfg.repo_url_for_links() == "https://github.com/o/r"


# ---------------------------------------------------------------- cache keys

def test_profile_key_follows_runs_check_and_inputs(tmp_path):
    cfg = load(write(tmp_path, BASIC.replace("--go", "{here}/in.txt")))
    (tmp_path / "in.txt").write_text("a")
    k1 = profile_key(cfg)
    assert profile_key(cfg) == k1
    (tmp_path / "in.txt").write_text("abc")          # different size
    k2 = profile_key(cfg)
    assert k2 != k1
    cfg.check = "{bin} --version"
    assert profile_key(cfg) != k2
    k3 = profile_key(cfg)
    cfg.runs[0].command += " -v"
    assert profile_key(cfg) != k3
    assert profile_key(cfg).split("-")[0] == k1.split("-")[0]     # same builds are reused


# ---------------------------------------------------------------- profiles

CALLGRIND = """\
version: 1
positions: line
events: Ir
fl=(1) /src/worktrees/abc123/prog.c
fn=(1) work'main'(below main)
10 500
cfn=(2) helper.isra.0'work'main
calls=1 0
11 999999
fn=(2)
20 300
fl=(2) /usr/lib64/libc.so.6
fn=(3) memcpy'work'main
5 200
fn=(4) _start
1 7
"""


def test_parse_and_fold(tmp_path):
    f = tmp_path / "cg.out"
    f.write_text(CALLGRIND)
    cost, files = parse_callgrind(f)
    assert cost["work'main'(below main)"] == 500          # the inclusive call cost is not counted twice
    assert cost["helper.isra.0'work'main"] == 300
    assert files["memcpy"] == "/usr/lib64/libc.so.6"
    folded = fold(cost, "Run")
    assert folded == collections.Counter({
        "Run;main;work": 500, "Run;main;work;helper": 300, "Run;main;work;memcpy": 200, "Run;_start": 7})


def test_fold_drops_recursion_depth_markers():
    folded = fold(collections.Counter({"f'2'f'main": 5}), "R")
    assert folded == collections.Counter({"R;main;f;f": 5})


# ---------------------------------------------------------------- grouping

def classifier(tmp_path, runs=1, rules=(), group_by="auto"):
    cfg = Config(path=tmp_path / "flamelapse.toml", name="p", repo="r",
                 runs=[Run(f"R{i}", "{bin}") for i in range(runs)], rules=list(rules), group_by=group_by)
    files = {"parse": "/w/.flamelapse/worktrees/abc/src/parse.c", "emit": "/w/.flamelapse/worktrees/abc/src/emit.c",
             "memcpy": "/usr/lib64/libc.so.6"}
    prof = {"stacks": {"R0;main;parse": 10, "R0;main;emit": 5, "R0;main;memcpy": 3, "R0;main;parse;malloc": 2},
            "files": files}
    return Classifier(cfg, [prof]), files


def test_groups_by_source_file_with_one_run(tmp_path):
    clf, files = classifier(tmp_path)
    labels = {k.key: k.label for k in clf.kinds}
    assert labels[clf.classify("R0;main;parse", files)] == "parse.c"
    assert labels[clf.classify("R0;main;memcpy", files)] == "System libraries"
    assert labels[clf.classify("R0;main;parse;malloc", files)] == "Memory"      # built-in rule wins


def test_user_rules_come_first_and_leaf_only_matches_leaf(tmp_path):
    rules = [Rule("Parsing", re.compile("^parse$"), "leaf"), Rule("Output", re.compile("^emit$"), "any")]
    clf, files = classifier(tmp_path, rules=rules)
    labels = {k.key: k.label for k in clf.kinds}
    assert labels[clf.classify("R0;main;parse", files)] == "Parsing"
    assert labels[clf.classify("R0;main;parse;malloc", files)] == "Memory"     # parse is not the leaf
    assert labels[clf.classify("R0;main;emit;memcpy", files)] == "Output"      # "any" sees the caller


def test_groups_by_run_with_several_runs(tmp_path):
    clf, files = classifier(tmp_path, runs=2)
    labels = {k.key: k.label for k in clf.kinds}
    assert labels[clf.classify("R1;main;parse", files)] == "R1"


# ---------------------------------------------------------------- pinning

def test_culprit_follows_the_direction_of_the_jump():
    line = ["a", "b", "c", "d", "e"]
    cache = {"a": 100, "b": 130, "c": 80, "d": 82, "e": 81}       # overall down, biggest up-step is a->b
    assert culprit(line, cache, jump=-0.19) == ("b", "c")
    assert culprit(line, cache, jump=+0.30) == ("a", "b")


def test_culprit_skips_gaps_and_gives_up_without_neighbours():
    cache = {"a": 100, "b": None, "c": 50}
    assert culprit(["a", "b", "c"], cache, jump=-0.5) is None
    assert culprit(["a", "b", "c", "d"], {**cache, "d": 40}, jump=-0.6) == ("c", "d")


# ---------------------------------------------------------------- git

def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


def test_sampling_keeps_ends_and_tags(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for i in range(20):
        (repo / "f").write_text(str(i))
        git(repo, "add", "f")
        git(repo, "commit", "-q", "-m", f"c{i}")
        if i == 7:
            git(repo, "tag", "-a", "v1.0", "-m", "release")
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = Config(path=ws / "flamelapse.toml", name="t", repo="../repo", samples=4)
    src = gitutil.sync(cfg)                                    # relative to the config, not the cwd
    line = gitutil.mainline(cfg, src, gitutil.resolve_branch(cfg, src))
    assert len(line) == 20
    picked = gitutil.pick(cfg, src, line)
    assert picked[0] == line[0] and picked[-1] == line[-1]
    assert line[7] in picked                                   # the tagged commit
    assert gitutil.tags(src)[line[7]] == "v1.0"
    cfg.since = "v1.0"
    assert gitutil.mainline(cfg, src, line[-1])[0] == line[7]


def test_rust_and_generic_file_names_get_readable_labels(tmp_path):
    from flamelapse.categories import _kind_for, _short_path
    std = _short_path("/rustc/59807616e1fa/library/core/src/fmt/mod.rs")
    assert _kind_for(std, 0, [std]).label == "Rust core: fmt"
    sort = _short_path("/rustc/59807616e1fa/library/core/src/slice/sort.rs")
    assert _kind_for(sort, 0, [sort]).label == "Rust core: slice/sort"
    dep = _short_path("/home/u/.cargo/registry/src/index.crates.io-6f17d22bba15001f/memchr-2.7.4/src/memchr.rs")
    assert dep == "@crate:memchr" and _kind_for(dep, 0, [dep]).label == "memchr (crate)"
    assert _kind_for("src/lib.rs", 0, ["src/lib.rs"]).label == "src/lib.rs"
    taken = ["a/util.c", "b/util.c", "c/parse.c"]
    assert [_kind_for(t, 0, taken).label for t in taken] == ["a/util.c", "b/util.c", "parse.c"]
