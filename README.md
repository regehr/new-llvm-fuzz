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
5. Drops functions longer than `--max-insts` instructions.
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
├── mismatch/      cases where backend-tv found a miscompilation
├── correct/       cases backend-tv validated
├── logs/          backend-tv output for each kept case
└── results.jsonl  one record per function validated, kept or not
```

Kept files get random hex names. Provenance lives in `results.jsonl`, which
records the source file and original function name for every case:

```json
{"src": "CodeGen/SystemZ/and-04.ll", "func": "f14", "name": "1bcc83...", "verdict": "correct", "rc": 0, "seconds": 0.08, "sha256": "..."}
```

### How cases are classified

| `backend-tv` output | Verdict | Result |
| --- | --- | --- |
| `Value mismatch` | `mismatch` | kept in `mismatch/` |
| `Transformation seems to be correct!` | `correct` | kept in `correct/` |
| anything else | `other` | file deleted |
| killed at `--hard-timeout` | `timeout` | file deleted |

Anything that is not a clear yes or no is discarded — solver timeouts,
unsupported constructs, lifting failures, IR the verifier rejects — but is still
recorded in `results.jsonl`.

### Options worth knowing

| Flag | Default | Notes |
| --- | --- | --- |
| `--backend` | `riscv64` | passed through to `backend-tv -backend=` |
| `-j`, `--jobs` | CPU count | one `backend-tv` process per worker |
| `--max-insts` | `15` | drop functions longer than this (0 = no limit) |
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
