"""The whole pipeline on a tiny C project made on the spot: init, run, pin, report.
Needs a C compiler, make and valgrind; takes a few seconds."""
import json
import os
import shutil
import subprocess

import pytest

from flamelapse.cli import main

pytestmark = pytest.mark.skipif(not (shutil.which("valgrind") and shutil.which("cc") and shutil.which("make")),
                                reason="needs valgrind, cc and make")

PROG = r"""
#include <stdio.h>
#include <stdlib.h>
static long sum(int n) { long s = 0; for (int i = 0; i < n; i++) s += i %% 7; return s; }
int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 1000;
    long s = 0;
    for (int r = 0; r < %d; r++) s += sum(n);
    printf("%%ld\n", s);
    return 0;
}
"""


def commit(repo, msg, rounds):
    (repo / "prog.c").write_text(PROG % rounds)
    (repo / "Makefile").write_text("prog: prog.c\n\tcc -O1 -g -o prog prog.c\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", msg], check=True, env=env)


def test_finds_the_commit_that_made_it_slower(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-qb", "main"], check=True)
    for i, rounds in enumerate([10, 10, 10, 30, 30, 30]):          # commit 4 triples the work
        commit(repo, f"change {i}" if i != 3 else "Do the sum three times", rounds)
    ws = tmp_path / "ws"
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: None)

    main(["-q", "init", str(repo), "--dir", str(ws), "--run", "{bin} 20000", "--samples", "2", "--jobs", "2"])
    toml = (ws / "flamelapse.toml").read_text()
    assert 'binary = "prog"' in toml
    assert f'repo = "{repo}"' in toml                                # local paths are stored absolute

    main(["-q", "run", str(ws)])
    data_js = (ws / "flamelapse-report" / "data-proj.js").read_text()
    data = json.loads(data_js.removeprefix("window.FLAMELAPSE=").rstrip().rstrip(";"))
    subjects = [f["subject"] for f in data["frames"]]
    # the two samples, plus the culprit and its parent found by pinning
    assert subjects == ["change 0", "change 2", "Do the sum three times", "change 5"]
    totals = [f["total"] for f in data["frames"]]
    assert totals[2] > 2.5 * totals[1]
    assert all(sum(f["cats"]) == f["total"] for f in data["frames"])
    assert "prog.c" in [c["label"] for c in data["cats"]]
    for name in ("index.html", "flame.html", "projects.js"):
        assert (ws / "flamelapse-report" / name).exists()

    # a second run has nothing to do and gives the same report
    main(["-q", "run", str(ws)])
    assert (ws / "flamelapse-report" / "data-proj.js").read_text() == data_js


def test_a_broken_run_command_stops_early_with_the_reason(tmp_path, capsys):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-qb", "main"], check=True)
    for i in range(3):
        commit(repo, f"c{i}", 10)
    ws = tmp_path / "ws"
    main(["init", str(repo), "--dir", str(ws), "--run", "{bin} 10 && ls /nonexistent-flamelapse"])
    err = capsys.readouterr().err
    assert "FAILED" in err and "nonexistent-flamelapse" in err
    with pytest.raises(SystemExit, match="newest version failed.*nonexistent-flamelapse"):
        main(["run", str(ws)])


def test_bin_works_in_pipes_and_compound_commands(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-qb", "main"], check=True)
    commit(repo, "a", 10)
    commit(repo, "b", 10)
    ws = tmp_path / "ws"
    main(["-q", "init", str(repo), "--dir", str(ws), "--run", "Once={bin} 5000",
          "--run", "Piped=echo x | {bin} 5000 | cat", "--run", "Twice={bin} 5000 && {bin} 5000",
          "--run", "Helper={bin} $({plainbin} 1 >/dev/null && echo 5000)"])
    main(["-q", "run", str(ws), "--no-pin"])
    prof = next((ws / ".flamelapse" / "profiles").glob("*/*.json"))
    runs = json.loads(prof.read_text())["runs"]
    once = runs["Once"]
    assert abs(runs["Piped"] - once) < 0.001 * once
    assert abs(runs["Twice"] - 2 * once) < 0.001 * once
    assert abs(runs["Helper"] - once) < 0.001 * once            # {plainbin} is not measured
