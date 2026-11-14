# Flamelapse guide

Everything beyond the [README](../README.md): what each command does, how to choose a workload, the config file, and the fine print.

## what you get

A static page (open it straight from disk, no server needed) with:

- a one-line headline, like "lz4 does 5% less work on this task than it did in 2016", and where the change came from
- a chart of the work done by every version, split into a handful of plain-language kinds of work
- numbered "moments that mattered": each change of 2% or more, pinned to one commit or merged pull request, with its title and an optional note in your own words
- a panel with the full breakdown for any version you click, and a Play button that walks through the history
- a flame graph of every function for anyone who wants the detail

## requirements

- Linux, git, Python 3.11+
- valgrind (does the profiling)
- perf (optional; makes pinning faster. Without it valgrind is used for that too)
- whatever the project itself needs to build

Run `flamelapse doctor` to check. On macOS or Windows, use the Docker image described in the README.

## usage

```
flamelapse init <repo> --run "<command using {bin}>"   # writes flamelapse.toml here
flamelapse try                                          # build the newest version once and run it, to debug the config
flamelapse run                                          # build + profile + pin + report; safe to stop and resume
flamelapse open                                         # open the report
flamelapse serve                                        # or serve it on localhost
```

`flamelapse quick <repo> ...` does init, run and open in one go, in a folder named after the project.

`init` detects autotools, CMake, Meson, Cargo, Go and plain Makefiles, builds the newest commit once and picks the program it produced (you can pick another with `--binary`). Everything it decided is written to `flamelapse.toml` with comments, so you can read and change it.

### Choosing what to run

The run command is the most important choice. Pick a realistic job that takes well under a second natively, since every version runs it under Valgrind, which is about 50 times slower. `init` tries each run once and tells you how long it took.

The command is ordinary shell. In it:

- `{bin}` is the program built from each version. Every call to it is measured, so pipes, `&&` and `$(...)` all work: `cat {here}/big.txt | {bin} --count`.
- `{plainbin}` is the same program but not measured, for helper calls like checking which flags a version supports.
- `{here}` is the folder with flamelapse.toml, so keep input files next to it (`quick` copies input files named in `--run` there for you). Replacing an input file re-measures everything.

Several runs are fine and each becomes its own colour:

```
flamelapse init https://github.com/madler/zlib --since 2017-01-01 \
  --run "Compress={bin} -c {here}/input.bin" \
  --run "Decompress={bin} -d -c {here}/input.gz"
```

Without any `--run`, flamelapse measures `{bin} --version`, which only tells you about start-up.

## how it works

1. A private clone of the repository (your own checkout is never touched).
2. Commits are sampled evenly along the main line (`--first-parent`), plus every release tag on it.
3. Each one is built in a throwaway git worktree. Only the program and any shared libraries it loads from the build tree are kept.
4. Each run goes through `valgrind --tool=callgrind`, which counts every instruction, with full call stacks.
5. For every jump of 2% or more between two neighbouring samples, every commit in between is measured (with `perf stat` when available), and the commit that made the biggest single step is profiled too.
6. The report groups the work: your own `[[category]]` rules first, then built-in kinds (memory, file reading and writing, program loading), then by run, or by source file when there is only one run.

Everything is cached under `.flamelapse/`, so a second `flamelapse run` only does what is new, and stopping it with Ctrl+C at any point is safe.

## configuration

See `examples/jq/flamelapse.toml` and `examples/lz4/flamelapse.toml`. The main keys:

| key | meaning |
|---|---|
| `name`, `repo` | project name on the report; path or URL of the repository |
| `since` | oldest version: a tag, commit or YYYY-MM-DD |
| `samples`, `jobs` | how many versions to spread over the range; how many to build at once |
| `threshold` | smallest change that gets pinned to one commit (default 0.02 = 2%) |
| `[build] command`, `binary`, `env` | shell commands run in the source tree; path of the program; extra environment |
| `[[run]] name`, `command`, `show` | what to measure, repeatable; `show` is how the command is written on the report, if the real one is hard to read |
| `check` | command that must succeed for a version to count, for example comparing output |
| `[[category]] label`, `match`, `where`, `description` | name a kind of work by a regex on function names; `where = "leaf"` only looks at the function doing the work |
| `group_by` | `auto`, `run` or `file`: how work not matched by a rule is grouped |
| `[notes]` | commit hash prefix = a sentence explaining that change on the report |
| `[report] intro` | the sentence above the workload on the report |

To compare projects on one page, point their reports at the same folder: `flamelapse report --out ~/flamelapse-all`.

## things to know

- Numbers are instruction counts, not seconds. That is what makes them repeatable (two runs agree to within about 0.02%), but they are a proxy: a change that runs more instructions in parallel can be faster in real time. zlib's "speed up CRC-32 by 1.5 to 3x" commit, for example, shows up as 2.7% more instructions.
- Old versions often fail to build with today's compilers. Those versions are skipped, and the end of the run lists the reasons, most common first, with a build log to look at. The build command can usually be taught to cope, for example `-fpermissive` for old C, or `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` for old CMake (added automatically). If the fix is on your machine instead (a missing library, say), `flamelapse run --retry` tries the failed versions again.
- Valgrind cannot run AVX-512 instructions. On a CPU that has them, flamelapse adds `-mno-avx512f` to every C and C++ compiler call, so projects built with `-march=native` still work. Code with hand-written AVX-512 that the compiler cannot avoid will still fail, and the reason says so.
- `run` builds and measures the newest version first, so a broken config fails in a minute, not after an hour.
- The workload is fixed across all versions, so it has to work with the oldest one too. Use `since` to skip versions that do not support it, or a `check` command to reject them.
- Disk use is roughly one program binary per version plus the clone.
- Changing the build command starts a fresh set of builds; changing a run, the check or an input file keeps the builds and only measures again.

## examples

```
flamelapse run examples/lz4    # ten years of lz4: about 8 minutes on 16 cores
flamelapse run examples/jq     # jq 1.5 to today: about 25 minutes, mostly autotools builds
```

## development

```
pip install -e '.[test]'
pytest
```

The tests build a tiny C project on the spot and check that flamelapse pins the commit that made it slower, so they need a C compiler, make and valgrind (they are skipped otherwise).
