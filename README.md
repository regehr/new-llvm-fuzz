# new-fuzz

Tooling for finding LLVM backend miscompilations with
[alive2](https://github.com/AliveToolkit/alive2)'s translation validator.

## Making a Seed Set

`harvest_tv_cases.py` turns an LLVM source tree into a corpus of single-function
LLVM IR files, partitioned by what `backend-tv` says about each one. The LLVM
test suite is a large pile of hand-written IR that already exercises interesting
corners of the language, which makes it a good starting point for a seed set.

### What it does

1. Walks the tree for `.ll` and `.bc` files.
2. Enumerates every function *definition* in each module.
3. Splits each module with `llvm-extract`, one function per file. Globals and
   declarations the function references are kept, so the IR stays valid.
4. Strips `target datalayout` and `target triple`, so the backend under test
   picks the target rather than the test file. `ModuleID` and `source_filename`
   go too — they are per-source noise that would otherwise defeat
   de-duplication.
5. Strips debug info and SSA value names with `opt -passes=strip`, then removes
   the debug-only entries `opt` leaves behind in `!llvm.module.flags`.
6. Drops any function longer than `--max-insts` instructions (default 15),
   before anything reaches the validator.
7. Renames the extracted function to `@f` and renumbers the remaining globals
   to `@g0`, `@g1`, ...
8. Runs `backend-tv` on the result and sorts it by the verdict.

### Requirements

- `backend-tv`, from an alive2 build (default: `~/alive2-regehr/build/backend-tv`)
- `llvm-extract`, `llvm-dis` and `opt` on `PATH`, or pass `--llvm-bin`
- Python 3.6+, no third-party packages

### Usage

```sh
python3 harvest_tv_cases.py ~/llvm-project -o tv-cases -j $(nproc)
```

That is a long run — a full checkout has ~48,000 `.ll` and ~456 `.bc` files,
holding a few hundred thousand functions. Start with a sample to get a feel for
the throughput and hit rate on your machine:

```sh
python3 harvest_tv_cases.py ~/llvm-project -o tv-cases --shuffle --limit-files 2000
```

Runs are resumable. `--resume` reads `OUT/results.jsonl` and skips any function
already validated, so an interrupted run picks up where it left off:

```sh
python3 harvest_tv_cases.py ~/llvm-project -o tv-cases --resume
```

### Output

```
tv-cases/
├── mismatch/      cases where backend-tv found a miscompilation
├── correct/       cases backend-tv validated
├── logs/          backend-tv output for each kept case
└── results.jsonl  one record per function validated, kept or not
```

Kept files get random hex names (`3cadcb0d005f7eabd8a07729996e23b5.ll`).
Provenance is not lost: every record in `results.jsonl` carries the source file
and the original function name, and each log begins with a header naming both.

```json
{"src": "CodeGen/SystemZ/and-04.ll", "func": "f14", "name": "1bcc83...", "verdict": "correct", "rc": 0, "seconds": 0.08, "sha256": "..."}
```

### How cases are classified

`backend-tv` is run as:

```sh
backend-tv -backend=riscv64 --smt-to=15000 --fn=f case.ll
```

| Output contains | Verdict | Result |
| --- | --- | --- |
| `Value mismatch` | `mismatch` | kept in `mismatch/` |
| `Transformation seems to be correct!` | `correct` | kept in `correct/` |
| anything else | `other` | file deleted |
| killed at `--hard-timeout` | `timeout` | file deleted |

Everything that is not a clear yes or no is discarded. That covers solver
timeouts, unsupported constructs, lifting failures, and IR that `llvm-extract`
produced but the verifier rejects. All of it is still recorded in
`results.jsonl`, so a run can be audited after the fact.

### Options worth knowing

| Flag | Default | Notes |
| --- | --- | --- |
| `--backend` | `riscv64` | passed through to `backend-tv -backend=` |
| `-j`, `--jobs` | CPU count | one `backend-tv` process per worker |
| `--max-insts` | `15` | drop functions longer than this, before validating (0 = no limit) |
| `--smt-timeout` | `15` | seconds, per SMT **query** (`--smt-to`) |
| `--hard-timeout` | `300` | seconds of wall clock before a run is killed |
| `--resume` | off | skip functions already in `results.jsonl` |
| `--shuffle`, `--limit-files` | off | sample the tree instead of sweeping it |
| `--only-tests` | off | restrict to paths under a `test/` directory |
| `--extract-only` | off | write the split functions and stop, without validating |
| `--no-strip` | off | keep debug info and SSA value names |
| `--keep-global-names` | off | keep original global names; renumbering is the default (see below) |
| `--keep-names` | off | keep original function names instead of renaming to `@f` |
| `--no-dedup` | off | keep functions whose stripped IR is identical |

### Notes

**Two different timeouts.** `--smt-timeout` bounds a single SMT query, and one
function costs several queries, so a run can exceed it. `--hard-timeout` is the
backstop: it `SIGKILL`s the whole process group, which also catches a process
wedged outside the solver, during lifting. Neither one can leave a worker stuck.

**Cases come out stripped.** `opt -passes=strip` removes debug info and all
value names, so a kept case is numbered (`%0`, `%1`, ...) with no `!dbg`, no
`DILocation`, and no debug records. `opt` leaves the module flags that describe
debug info (`"Debug Info Version"`, `"Dwarf Version"`) in place, so those are
removed afterwards, along with any metadata node they leave unreferenced; other
module flags such as `"wchar_size"` are kept. Type names (`%struct.S`) are names
of types rather than of SSA values, and survive. On a 5,452-function sample, no
file kept a named SSA value or any debug residue.

Note the stripping runs *before* the size filter, so `--max-insts` counts the
instructions in the file as it will be tested.

**Size filtering happens before `backend-tv` runs,** so a long function costs
an `llvm-extract` call and nothing more. Instructions are counted from the
extracted text rather than by another `opt` invocation, which would roughly
double the process count for the whole run. Every instruction in the function
is counted, including instructions in unreachable blocks; debug records
(`#dbg_value(...)`) are not instructions and are not counted. Note this differs
slightly from LLVM's own `print<func-properties>`, which counts only *reachable*
instructions. On a 5,464-function sample the two agreed except where a function
had unreachable blocks.

Most of LLVM's test functions are small, so the default is not as aggressive as
it sounds: on that same sample, 96% of extracted functions were 15 instructions
or fewer.

**Globals are renumbered by default.** Every global -- variables, function
definitions and declarations, aliases and ifuncs -- becomes `@g0`, `@g1`, ...
except three groups whose spelling is load-bearing:

- `llvm.*`, covering intrinsics and reserved globals such as `llvm.used`
- the 528 library functions LLVM's `TargetLibraryInfo` recognizes (`pow`,
  `memcpy`, `malloc`, ...), embedded from the generated `TargetLibraryInfo.inc`
- reserved `__*` identifiers, such as the compiler-rt calls a backend emits
  (`__muldi3`)

Those exclusions are the whole point. alive2 is handed a `TargetLibraryInfo`, so
renaming `@pow` turns a modeled library call into an opaque external call, and
renaming an intrinsic does the same. Measured on 400 real cases: renaming
everything changed the `backend-tv` verdict for 55 of them (both directions);
renaming everything but intrinsics changed 36; renaming with the policy above
changed **none**. Some of those changes look like improvements -- a `@pow` case
that failed with `global symbol 'llvm.pow.f64' not found` validates once the
call is opaque -- but that is routing around a backend-tv limitation and
silently dropping libcall coverage from the corpus, not a win.

Use `--libfuncs-file` to preserve extra names, one per line.

The reason this is the default is legibility. Cases in the corpus get read by hand, and
names inherited from whatever test the function came from -- `@.str10`,
`@_Z3usePv`, a mangled symbol from some other target's test suite -- carry no
meaning once the function stands alone. Renumbering them to `@g0`, `@g1` leaves
the parts that still mean something:

```llvm
@.str10 = external constant [70 x i8]        @g0 = external constant [70 x i8]
declare i32 @printk(ptr, ...)                declare i32 @g1(ptr, ...)
                                    ---->
  %3 = tail call i32 (ptr, ...)                %3 = tail call i32 (ptr, ...)
       @printk(ptr @.str10, i32 0, ...)             @g1(ptr @g0, i32 0, ...)
```

Renumbering is safe to leave on: across 400 real cases it changed no verdict at
all, so it costs nothing in what gets validated. It helps de-duplication too,
but barely -- on a 5,452-function sample it touched 605 files and collapsed 6
more duplicates. `--keep-global-names` turns it off.

**De-duplication is on by default,** keyed on the SHA-256 of the stripped IR.
LLVM's tests repeat the same function constantly across files; without this,
most of a run is spent re-proving things it has already proven. Normalizing the
name to `@f` makes this noticeably more effective, since two functions with
identical bodies and different names now hash the same.

**Renaming to `@f` is collision-aware.** A module that already has a global
named `@f` — common, and it is not always a function — gets that global renamed
to `@f.0` first. An implicit `comdat`, which `llvm-dis` prints bare when the
comdat name matches the global name, is renamed alongside the function. The
rewrite tracks comments and string literals, so an `@f` inside a string constant
or a metadata string is left alone.

**Sampling is per-file, not per-function.** `--shuffle` randomizes the file
order, and `--limit-funcs` then takes functions in order, so a small sample can
end up dominated by one or two large files. If those happen to be, say,
target-specific intrinsic tests for some other architecture, the run will report
nothing but `other`. Prefer `--limit-files` for sampling.

**Not every input is valid IR.** LLVM's test tree contains files that are
deliberately malformed, plus `.bc` too old for the current bitcode reader. These
are counted and skipped, not fatal. The end-of-run summary breaks down where
everything went.
