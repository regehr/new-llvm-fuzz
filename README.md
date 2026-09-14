# new-llvm-fuzz

Tooling for finding LLVM backend miscompilations with
[alive2](https://github.com/AliveToolkit/alive2)'s translation validator.

## Making a Seed Set

`harvest_tv_cases.py` turns an LLVM source tree into a corpus of single-function
LLVM IR files, partitioned by what `backend-tv` says about each one.

### What it does

1. Walks the tree for `.ll` and `.bc` files.
2. Splits each module with `llvm-extract`, one function per file, keeping the
   globals and declarations it references.
3. Strips `target datalayout` and `target triple`, so the backend under test
   picks the target.
4. Strips debug info and SSA value names (`opt -passes=strip`).
5. Rewrites the deprecated `undef` constant to `poison`, then drops functions
   longer than `--max-insts` instructions.
6. Renames the function to `@f` and renumbers other globals to `@g0`, `@g1`, ...
7. Runs `backend-tv` and sorts the case by the verdict.

### Requirements

- `backend-tv`, from an alive2 build (default: `~/alive2-regehr/build/backend-tv`)
- `llvm-extract`, `llvm-dis` and `opt` on `PATH`, or pass `--llvm-bin`
- Python 3.6+, no third-party packages

### Usage

```sh
python3 harvest_tv_cases.py ~/llvm-project -o tv-cases -j $(nproc) --resume
```

A full checkout is ~48,000 files holding a few hundred thousand functions, so
expect several hours. `--resume` skips functions already in `results.jsonl`, so
re-running the same command continues an interrupted run.

To sample instead of sweeping:

```sh
python3 harvest_tv_cases.py ~/llvm-project -o tv-cases --shuffle --limit-files 2000
```

### Output

```
tv-cases/
├── mismatch/  mismatch-logs/   backend-tv found a miscompilation
├── correct/   correct-logs/    backend-tv validated the case
├── crash/     crash-logs/      backend-tv crashed
├── refused/   refused-logs/    backend-tv could not process it
├── unproven/  unproven-logs/   processed, but proved nothing either way
└── results.jsonl               one record per function validated, kept or not
```

Each `<verdict>-logs/` holds the `backend-tv` output for the cases in the
directory beside it, under the same name. Timeouts are discarded.

Kept files get random hex names. Provenance lives in `results.jsonl`, which
records the source file and original function name for every case:

```json
{"src": "CodeGen/SystemZ/and-04.ll", "func": "f14", "name": "1bcc83...", "verdict": "correct", "rc": 0, "seconds": 0.08, "reason": null, "sha256": "..."}
```

### How cases are classified

| `backend-tv` outcome | Verdict | Result |
| --- | --- | --- |
| crashed — killed by a signal, or LLVM's crash handler ran | `crash` | kept |
| `Transformation doesn't verify!` — any unsoundness | `mismatch` | kept |
| `Transformation seems to be correct!` | `correct` | kept |
| `failed-to-prove` that is not a timeout | `unproven` | kept |
| anything else — unsupported constructs, lifting failures, IR that fails to type check | `refused` | kept |
| killed at `--hard-timeout`, or alive2 reports `ERROR: Timeout` | `timeout` | file deleted |

`mismatch` keys on alive2's unsoundness banner, not on one message, so it covers
every miscompile it can report: `Value mismatch`, `Target is more poisonous than
source`, `Target's return value is more undefined`, `Mismatch in memory`, and a
differing return domain. The specific one lands in the `reason` field of
`results.jsonl`.

Crash is checked first: a process that hit the crash handler cannot be trusted
to have printed a sound verdict. Every verdict is recorded in `results.jsonl`,
including discarded timeouts.

### Options worth knowing

| Flag | Default | Notes |
| --- | --- | --- |
| `--backend` | `riscv64` | passed through to `backend-tv -backend=` |
| `-j`, `--jobs` | CPU count | one `backend-tv` process per worker |
| `--max-insts` | `15` | drop functions longer than this (0 = no limit) |
| `--undef` | `poison` | rewrite `undef` to `poison`; or `drop` the function, or `keep` it |
| `--smt-timeout` | `15` | seconds, per SMT **query** (`--smt-to`) |
| `--hard-timeout` | `--smt-timeout` + 5 | seconds of wall clock before a run is killed |
| `--resume` | off | skip functions already in `results.jsonl` |
| `--shuffle`, `--limit-files` | off | sample the tree instead of sweeping it |
| `--only-tests` | off | restrict to paths under a `test/` directory |
| `--extract-only` | off | write the split functions and stop, without validating |
| `--no-strip` | off | keep debug info and SSA value names |
| `--keep-global-names` | off | keep original global names |
| `--keep-names` | off | keep the original function name instead of `@f` |
| `--no-dedup` | off | keep functions whose stripped IR is identical |

### Notes

**Two timeouts.** `--smt-timeout` bounds a single SMT query; `--hard-timeout` is
the wall-clock backstop that `SIGKILL`s the process group, catching a process
wedged outside the solver. Raise it if you would rather wait than discard slow
cases.

**Renumbering skips names that carry meaning:** `llvm.*`, the library functions
LLVM's `TargetLibraryInfo` recognizes, and reserved `__*` identifiers. Renaming
those would turn a modeled call into an opaque one and change what a case tests.
`--libfuncs-file` preserves extra names, one per line.

**Sampling is per-file.** `--shuffle` randomizes file order and `--limit-funcs`
then takes functions in order, so a small sample can be dominated by one large
file. Prefer `--limit-files`.

**Not every input is valid IR.** Deliberately malformed tests and `.bc` too old
for the current reader are counted and skipped. The end-of-run summary breaks
down where everything went.
