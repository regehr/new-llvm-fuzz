#!/usr/bin/env python3
"""
Harvest single-function LLVM IR test cases from an LLVM source tree and triage
them with alive2's backend translation validator.

Pipeline
--------
1. Walk the tree for *.ll and *.bc files.
2. Enumerate every function *definition* in each module.
3. Use llvm-extract to split each module into one-function-per-file .ll files
   (referenced globals/declarations are kept so the IR stays valid).
4. Strip `target datalayout` / `target triple` (plus ModuleID/source_filename,
   which are per-source noise and would defeat de-duplication).
5. Rename the extracted function to @f.  Any other global already called @f is
   renamed out of the way first.  Normalizing the name also makes de-duplication
   much more effective, since identical bodies now hash identically.
6. Run:  backend-tv -backend=<BACKEND> --smt-to=<MS> --fn=f file.ll
7. Classify the output:
       "Value mismatch"                      -> keep in  OUT/mismatch/
       "Transformation seems to be correct!" -> keep in  OUT/correct/
       anything else (errors, timeouts, ...) -> delete the file

Kept files are named with a random hex string.  Provenance (source file and
original function name) is recorded in OUT/results.jsonl, which is appended to
for every run; the tool output for kept cases is written to OUT/logs/<name>.txt.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Output classification.  These are the strings backend-tv actually prints:
# "ERROR: Value mismatch" for a miscompile, "Transformation seems to be
# correct!" for a validated function.
# ---------------------------------------------------------------------------
MISMATCH_RE = re.compile(r"Value mismatch")
CORRECT_RE = re.compile(r"Transformation seems to be correct!")

# `define ... @name(` -- name is either a quoted string or a bare LLVM identifier.
# A literal quote cannot appear inside a quoted name (it is spelled \22).
FUNC_DEF_RE = re.compile(
    r'^define\b[^\n]*?@(?P<name>"[^"\n]*"|[-a-zA-Z$._0-9]+)\s*\(', re.M)

# Module-level lines removed from every extracted file.
STRIP_RE = re.compile(
    r'^(?:target[ \t]+(?:datalayout|triple)[ \t]*=.*'
    r'|source_filename[ \t]*=.*'
    r'|;[ \t]*ModuleID[ \t]*=.*)\n?', re.M)

HEX2_RE = re.compile(r'[0-9A-Fa-f]{2}')
BARE_NAME_RE = re.compile(r'[-a-zA-Z$._0-9]+')
# Points where the meaning of the text can change: a comment, a string, or a
# global/comdat reference.  Everything between them is copied verbatim.
INTERESTING_RE = re.compile(r'[;"@$]')


def walk_ir(ir, mapping=None):
    """Scan textual IR for @global and $comdat references.

    Tracks comments and double-quoted strings so that an `@name` sitting inside
    a string constant or metadata string is never touched (LLVM has no \\" escape:
    a literal quote is spelled \\22, so the next quote always closes the string).

    Returns (rewritten_text, names), where names is a set of (sigil, name).  If
    mapping is None the text is returned unchanged.
    """
    out = []
    names = set()
    i, n = 0, len(ir)
    while i < n:
        m = INTERESTING_RE.search(ir, i)
        if m is None:
            out.append(ir[i:])
            break
        j = m.start()
        out.append(ir[i:j])
        c = ir[j]
        if c == ';':                                    # comment to end of line
            k = ir.find('\n', j)
            k = n if k < 0 else k
            out.append(ir[j:k])
            i = k
        elif c == '"':                                  # string constant
            k = ir.find('"', j + 1)
            k = n if k < 0 else k + 1
            out.append(ir[j:k])
            i = k
        else:                                           # '@' or '$'
            if j + 1 < n and ir[j + 1] == '"':          # quoted name
                k = ir.find('"', j + 2)
                if k < 0:
                    out.append(c)
                    i = j + 1
                    continue
                name = unquote_llvm_name(ir[j + 1:k + 1])
                end = k + 1
            else:
                mm = BARE_NAME_RE.match(ir, j + 1)
                if mm is None:                          # bare '@'/'$', e.g. in text
                    out.append(c)
                    i = j + 1
                    continue
                name = mm.group(0)
                end = mm.end()
            if name is not None:
                names.add((c, name))
            if mapping and name is not None and (c, name) in mapping:
                out.append(c + mapping[(c, name)])
            else:
                out.append(ir[j:end])
            i = end
    return "".join(out), names


def normalize_function_name(ir, target, new="f"):
    """Rename @target to @new, moving any colliding global out of the way."""
    _, names = walk_ir(ir)
    if ('@', target) not in names:
        return None
    if target == new:
        return ir
    mapping = {('@', target): new}
    taken = {nm for sig, nm in names if sig == '@'}
    if new in taken:
        k, alt = 0, new + ".0"
        while alt in taken:
            k += 1
            alt = "%s.%d" % (new, k)
        mapping[('@', new)] = alt
    # llvm-dis prints a bare `comdat` when the comdat name matches the global
    # name, so an implicit comdat has to be renamed alongside the function.
    if ('$', target) in names:
        mapping[('$', target)] = new
    return walk_ir(ir, mapping)[0]


def unquote_llvm_name(raw):
    """Turn the regex-captured name into the raw name llvm-extract wants.

    In .ll, a quoted identifier escapes bytes as \\HH; everything else is
    literal.  Returns None for names that cannot survive a trip through argv
    (embedded NUL, or bytes that are not valid UTF-8)."""
    if not raw.startswith('"'):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] == '\\' and HEX2_RE.match(body, i + 1, i + 3):
            out.append(int(body[i + 1:i + 3], 16))
            i += 3
        else:
            out.extend(body[i].encode('utf-8', 'surrogateescape'))
            i += 1
    if 0 in out:
        return None
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError:
        return None


def run_cmd(cmd, timeout_s):
    """Run cmd, merging stderr into stdout.

    Returns (returncode_or_None, output_text, timed_out).  On timeout the whole
    process group is killed, so any children die too."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError as e:
        return None, "failed to exec %s: %s" % (cmd[0], e), False
    try:
        out, _ = p.communicate(timeout=timeout_s)
        return p.returncode, out.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, _ = p.communicate(timeout=15)
        except Exception:
            out = b""
        return None, out.decode("utf-8", "replace"), True


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.c = {}

    def bump(self, key, n=1):
        with self.lock:
            self.c[key] = self.c.get(key, 0) + n

    def get(self, key):
        with self.lock:
            return self.c.get(key, 0)

    def snapshot(self):
        with self.lock:
            return dict(self.c)


class Harvester:
    def __init__(self, args):
        self.args = args
        self.stats = Stats()
        self.seen_hashes = set()
        self.hash_lock = threading.Lock()
        self.jsonl_lock = threading.Lock()
        self.stop = threading.Event()

        self.out = os.path.abspath(args.out)
        self.dir_mismatch = os.path.join(self.out, "mismatch")
        self.dir_correct = os.path.join(self.out, "correct")
        self.dir_logs = os.path.join(self.out, "logs")
        self.dir_extracted = os.path.join(self.out, "extracted")
        for d in (self.dir_mismatch, self.dir_correct, self.dir_logs):
            os.makedirs(d, exist_ok=True)
        if args.extract_only:
            os.makedirs(self.dir_extracted, exist_ok=True)

        self.jsonl_path = os.path.join(self.out, "results.jsonl")
        self.done_pairs = set()
        if args.resume and os.path.exists(self.jsonl_path):
            n = 0
            with open(self.jsonl_path, errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    self.done_pairs.add((r["src"], r["func"]))
                    if r.get("sha256"):
                        self.seen_hashes.add(r["sha256"])
                    n += 1
            sys.stderr.write("resume: %d functions already validated\n" % n)
        self.jsonl = open(self.jsonl_path, "a", buffering=1)

    # -- helpers ----------------------------------------------------------
    def record(self, obj):
        line = json.dumps(obj)
        with self.jsonl_lock:
            self.jsonl.write(line + "\n")

    def module_text(self, path):
        """Textual IR for a .ll or .bc file, or None if unreadable."""
        if path.endswith(".bc"):
            rc, out, to = run_cmd([self.args.llvm_dis, "-o", "-", path],
                                  self.args.tool_timeout)
            if to or rc != 0:
                return None
            return out
        try:
            with open(path, "r", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def func_names(self, text):
        names, seen = [], set()
        for m in FUNC_DEF_RE.finditer(text):
            n = unquote_llvm_name(m.group("name"))
            if n is None:
                self.stats.bump("skipped_unrepresentable_name")
                continue
            if n not in seen:
                seen.add(n)
                names.append(n)
        return names

    def case_name(self):
        """Random hex name; provenance lives in results.jsonl."""
        return secrets.token_hex(self.args.name_bytes)

    # -- phase 1: enumerate ----------------------------------------------
    def enumerate_file(self, src):
        text = self.module_text(src)
        if text is None:
            self.stats.bump("unparsable_modules")
            return []
        names = self.func_names(text)
        if not names:
            self.stats.bump("modules_without_definitions")
            return []
        if self.args.max_funcs_per_file and len(names) > self.args.max_funcs_per_file:
            self.stats.bump("truncated_modules")
            names = names[:self.args.max_funcs_per_file]
        self.stats.bump("modules_with_definitions")
        return [(src, n) for n in names]

    # -- phase 2: extract, strip, validate --------------------------------
    def extract(self, src, func):
        """Return (stripped single-function IR, name of the function), or None."""
        rc, out, to = run_cmd(
            [self.args.llvm_extract, "-S", "-func", func, "-o", "-", src],
            self.args.tool_timeout)
        if to:
            self.stats.bump("extract_timeout")
            return None
        if rc != 0 or not out.strip():
            self.stats.bump("extract_failed")
            return None
        ir = STRIP_RE.sub("", out)
        if not FUNC_DEF_RE.search(ir):
            self.stats.bump("extract_empty")
            return None
        name = func
        if not self.args.keep_names:
            renamed = normalize_function_name(ir, func, self.args.func_name)
            if renamed is None:
                self.stats.bump("rename_failed")
                return None
            ir, name = renamed, self.args.func_name
        return ir.strip() + "\n", name

    def process(self, task):
        if self.stop.is_set():
            return
        src, func = task
        got = self.extract(src, func)
        if got is None:
            return
        ir, fn_name = got

        digest = hashlib.sha256(ir.encode()).hexdigest()
        if not self.args.no_dedup:
            with self.hash_lock:
                if digest in self.seen_hashes:
                    self.stats.bump("duplicates")
                    return
                self.seen_hashes.add(digest)

        name = self.case_name()
        self.stats.bump("functions_extracted")

        if self.args.extract_only:
            with open(os.path.join(self.dir_extracted, name + ".ll"), "w") as f:
                f.write(ir)
            return

        fd, tmp = tempfile.mkstemp(prefix=name + ".", suffix=".ll",
                                   dir=self.args.tmp_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(ir)

            t0 = time.time()
            rc, out, timed_out = run_cmd(
                [self.args.backend_tv,
                 "-backend=" + self.args.backend,
                 "--smt-to=%d" % int(self.args.smt_timeout * 1000),
                 "--fn=" + fn_name,
                 tmp],
                self.args.hard_timeout)
            elapsed = time.time() - t0

            if timed_out:
                verdict = "timeout"
            elif MISMATCH_RE.search(out):
                verdict = "mismatch"
            elif CORRECT_RE.search(out):
                verdict = "correct"
            else:
                verdict = "other"

            self.stats.bump(verdict)
            self.record({"src": os.path.relpath(src, self.args.tree),
                         "func": func, "name": name, "verdict": verdict,
                         "rc": rc, "seconds": round(elapsed, 2),
                         "sha256": digest})

            if verdict in ("mismatch", "correct"):
                dest = self.dir_mismatch if verdict == "mismatch" else self.dir_correct
                shutil.move(tmp, os.path.join(dest, name + ".ll"))
                tmp = None
                if not self.args.no_logs:
                    with open(os.path.join(self.dir_logs, name + ".txt"), "w") as f:
                        f.write("# %s.ll  <-  %s  @%s\n" %
                                (name, os.path.relpath(src, self.args.tree), func))
                        f.write("$ %s -backend=%s --smt-to=%d --fn=%s %s.ll\n\n" %
                                (self.args.backend_tv, self.args.backend,
                                 int(self.args.smt_timeout * 1000), fn_name, name))
                        f.write(out)
        finally:
            if tmp is not None and os.path.exists(tmp):
                os.unlink(tmp)

    # -- driver -----------------------------------------------------------
    def progress(self, done, total, phase, t0, final=False):
        # On a tty repaint one line; when redirected, emit a line now and then.
        tty = sys.stderr.isatty()
        every = 10 if tty else max(1, total // 200)
        if not final and done % every:
            return
        el = time.time() - t0
        rate = done / el if el > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        s = self.stats.snapshot()
        sys.stderr.write(
            "%s[%s] %d/%d  %.1f/s  eta %s  mismatch=%d correct=%d other=%d timeout=%d dup=%d   %s"
            % ("\r" if tty else "", phase, done, total, rate, fmt_dur(eta),
               s.get("mismatch", 0), s.get("correct", 0), s.get("other", 0),
               s.get("timeout", 0), s.get("duplicates", 0),
               "" if tty else "\n"))
        sys.stderr.flush()

    def run(self):
        a = self.args
        sys.stderr.write("scanning %s ...\n" % a.tree)
        files = sorted(iter_ir_files(a.tree, a.only_tests))
        if a.shuffle:
            import random
            random.Random(a.seed).shuffle(files)
        if a.limit_files:
            files = files[:a.limit_files]
        sys.stderr.write("found %d .ll/.bc files\n" % len(files))
        if not files:
            sys.stderr.write("nothing to do\n")
            return 1

        tasks = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            for i, res in enumerate(ex.map(self.enumerate_file, files), 1):
                tasks.extend(res)
                self.progress(i, len(files), "enumerate", t0, final=(i == len(files)))
        sys.stderr.write("\nfound %d function definitions\n" % len(tasks))

        if self.done_pairs:
            before = len(tasks)
            tasks = [t for t in tasks
                     if (os.path.relpath(t[0], a.tree), t[1]) not in self.done_pairs]
            sys.stderr.write("resume: skipping %d already-done functions\n"
                             % (before - len(tasks)))
        if a.limit_funcs:
            tasks = tasks[:a.limit_funcs]
        if not tasks:
            sys.stderr.write("nothing to do\n")
            return 0

        t0 = time.time()
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=a.jobs) as ex:
                for _ in ex.map(self.process, tasks):
                    done += 1
                    self.progress(done, len(tasks), "validate", t0,
                                  final=(done == len(tasks)))
        except KeyboardInterrupt:
            self.stop.set()
            sys.stderr.write("\ninterrupted; finishing in-flight work\n")

        sys.stderr.write("\n\nsummary:\n")
        for k, v in sorted(self.stats.snapshot().items()):
            sys.stderr.write("  %-32s %d\n" % (k, v))
        sys.stderr.write("\n  mismatch cases -> %s\n" % self.dir_mismatch)
        sys.stderr.write("  correct cases  -> %s\n" % self.dir_correct)
        sys.stderr.write("  full log       -> %s\n" % self.jsonl_path)
        self.jsonl.close()
        return 0


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def iter_ir_files(tree, only_tests):
    skip_dirs = {".git", ".svn", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not (fn.endswith(".ll") or fn.endswith(".bc")):
                continue
            p = os.path.join(dirpath, fn)
            if only_tests and "/test/" not in p.replace(os.sep, "/"):
                continue
            yield p


def find_tool(name, hint_dirs):
    p = shutil.which(name)
    if p:
        return p
    for d in hint_dirs:
        c = os.path.join(os.path.expanduser(d), name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return name


def main(argv):
    ap = argparse.ArgumentParser(
        description="Split LLVM tests into one-function files and triage them "
                    "with alive2 backend-tv.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("tree", help="path to an LLVM source tree")
    ap.add_argument("-o", "--out", default="tv-cases",
                    help="output directory (mismatch/, correct/, logs/)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                    help="parallel workers")
    ap.add_argument("--backend", default="riscv64", help="backend-tv -backend value")
    ap.add_argument("--smt-timeout", type=float, default=15.0,
                    help="SMT query timeout in seconds (--smt-to)")
    ap.add_argument("--hard-timeout", type=float, default=300.0,
                    help="wall-clock kill timeout for one backend-tv run, seconds")
    ap.add_argument("--tool-timeout", type=float, default=120.0,
                    help="wall-clock timeout for llvm-dis/llvm-extract, seconds")
    ap.add_argument("--backend-tv",
                    default=os.path.expanduser("~/alive2-regehr/build/backend-tv"),
                    help="path to backend-tv")
    ap.add_argument("--llvm-bin", default="~/llvm-project/for-alive/bin",
                    help="fallback directory for llvm-extract/llvm-dis")
    ap.add_argument("--tmp-dir", default=None,
                    help="scratch directory for candidate files")
    ap.add_argument("--only-tests", action="store_true",
                    help="only consider files under a */test/* path")
    ap.add_argument("--func-name", default="f",
                    help="rename every extracted function to this name")
    ap.add_argument("--keep-names", action="store_true",
                    help="keep original function names instead of renaming")
    ap.add_argument("--name-bytes", type=int, default=16,
                    help="bytes of randomness in each output filename")
    ap.add_argument("--resume", action="store_true",
                    help="skip functions already recorded in OUT/results.jsonl")
    ap.add_argument("--no-dedup", action="store_true",
                    help="do not drop functions whose stripped IR is identical")
    ap.add_argument("--no-logs", action="store_true",
                    help="do not save backend-tv output for kept cases")
    ap.add_argument("--extract-only", action="store_true",
                    help="write every extracted function to OUT/extracted and stop")
    ap.add_argument("--limit-files", type=int, default=0,
                    help="only look at the first N source files (0 = all)")
    ap.add_argument("--limit-funcs", type=int, default=0,
                    help="only validate the first N functions (0 = all)")
    ap.add_argument("--max-funcs-per-file", type=int, default=0,
                    help="cap functions taken from one module (0 = no cap)")
    ap.add_argument("--shuffle", action="store_true",
                    help="randomize source file order (useful with --limit-*)")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed")
    args = ap.parse_args(argv)

    args.tree = os.path.abspath(os.path.expanduser(args.tree))
    if not os.path.isdir(args.tree):
        sys.exit("not a directory: %s" % args.tree)
    args.backend_tv = os.path.expanduser(args.backend_tv)
    if not (os.path.isfile(args.backend_tv) and os.access(args.backend_tv, os.X_OK)):
        sys.exit("backend-tv not executable: %s" % args.backend_tv)
    hints = [args.llvm_bin]
    args.llvm_extract = find_tool("llvm-extract", hints)
    args.llvm_dis = find_tool("llvm-dis", hints)
    for t in (args.llvm_extract, args.llvm_dis):
        if not shutil.which(t):
            sys.exit("cannot find %s (use --llvm-bin)" % t)
    if args.name_bytes < 4:
        sys.exit("--name-bytes must be at least 4")
    if not re.fullmatch(r"[-a-zA-Z$._][-a-zA-Z$._0-9]*", args.func_name):
        sys.exit("--func-name must be a bare LLVM identifier: %s" % args.func_name)
    if args.tmp_dir:
        os.makedirs(args.tmp_dir, exist_ok=True)

    return Harvester(args).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
