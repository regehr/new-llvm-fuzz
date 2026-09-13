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
5. Strip debug info and SSA value names with `opt -passes=strip`.
6. Drop functions longer than --max-insts instructions (default 15), before
   anything is handed to backend-tv.
7. Rename the extracted function to @f and renumber the other globals to @g0,
   @g1, ... keeping intrinsics and library functions, so a case reads without
   names inherited from whatever test it came from.  Any other global already called @f is
   renamed out of the way first.  Normalizing the name also makes de-duplication
   much more effective, since identical bodies now hash identically.
8. Run:  backend-tv -backend=<BACKEND> --smt-to=<MS> --fn=f file.ll
9. Classify the output:
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

    Returns (rewritten_text, names), where names maps (sigil, name) -> None in
    order of first appearance; callers that renumber globals rely on that order
    so the result is canonical.  If mapping is None the text is returned
    unchanged.
    """
    out = []
    names = {}
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
                names.setdefault((c, name), None)
            if mapping and name is not None and (c, name) in mapping:
                out.append(c + mapping[(c, name)])
            else:
                out.append(ir[j:end])
            i = end
    return "".join(out), names


# An instruction either binds a result (`%x = ...`) or is one of these.  Listing
# the result-free opcodes is what keeps block labels (`ret:`) and the
# continuation lines of multi-line instructions from being counted.
VOID_OPCODES = frozenset("""
    ret br switch indirectbr invoke callbr resume catchret cleanupret
    unreachable store fence call tail musttail notail
""".split())

RESULT_RE = re.compile(r'%(?:"[^"\n]*"|[-a-zA-Z$._0-9]+)\s*=')
# \b so a trailing comma or metadata attachment (`unreachable, !dbg !3`) still
# matches, and so `call` does not match `callbr`.
OPCODE_RE = re.compile(r'(?:%s)\b' % "|".join(sorted(VOID_OPCODES)))


# `opt -passes=strip` removes debug info but leaves the module flags that
# describe it.  These are the flag names that exist only to support debug info.
DEBUG_MODULE_FLAGS = frozenset([
    "Debug Info Version", "Dwarf Version", "CodeView", "CodeViewGHash",
])
MD_FLAGS_RE = re.compile(r'^!llvm\.module\.flags = !\{([^}\n]*)\}$', re.M)
MD_DEF_RE = re.compile(r'^!(\d+) = !\{([^\n]*)\}$', re.M)


def strip_debug_module_flags(ir):
    """Drop debug-only entries from !llvm.module.flags, and any metadata node
    left unreferenced as a result.  Metadata ids need not be contiguous, so the
    survivors keep their numbering."""
    m = MD_FLAGS_RE.search(ir)
    if m is None:
        return ir
    ids = re.findall(r'!(\d+)', m.group(1))
    bodies = dict(MD_DEF_RE.findall(ir))
    drop = set()
    for i in ids:
        name = re.search(r'!"([^"]*)"', bodies.get(i, ""))
        if name is not None and name.group(1) in DEBUG_MODULE_FLAGS:
            drop.add(i)
    if not drop:
        return ir

    keep = [i for i in ids if i not in drop]
    if keep:
        line = "!llvm.module.flags = !{%s}" % ", ".join("!" + i for i in keep)
        ir = ir[:m.start()] + line + ir[m.end():]
    else:
        end = m.end() + (1 if ir[m.end():m.end() + 1] == "\n" else 0)
        ir = ir[:m.start()] + ir[end:]

    # Remove each dropped node's definition, but only once nothing refers to it.
    for i in sorted(drop, key=int):
        dm = re.search(r'^!%s = [^\n]*\n?' % i, ir, re.M)
        if dm is None:
            continue
        rest = ir[:dm.start()] + ir[dm.end():]
        if not re.search(r'!%s(?![0-9])' % i, rest):
            ir = rest
    return ir


def count_instructions(ir):
    """Count the instructions in the single function defined in `ir`.

    llvm-extract emits canonical llvm-dis formatting: instructions are indented,
    block labels and the closing brace sit at column 0.  That makes instructions
    recognizable without a full parse, and keeps a block label such as `ret:`
    from being read as an opcode.  A multi-line instruction (a switch table, an
    invoke's `to label`, a landingpad's clauses) counts once, since only its
    first line binds a result or starts with an opcode.

    Every instruction in the function is counted, including ones in blocks that
    are unreachable.  Debug records (`#dbg_value(...)`) are not instructions and
    are not counted.
    """
    n = 0
    in_body = False
    for line in ir.splitlines():
        if not in_body:
            in_body = line.startswith("define")
            continue
        if line == "}":
            in_body = False
            continue
        if not line[:1].isspace():          # basic block label
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if RESULT_RE.match(stripped) or OPCODE_RE.match(stripped):
            n += 1
    return n


# Every name LLVM's TargetLibraryInfo recognizes, parsed from the generated
# TargetLibraryInfo.inc (LLVM 21, 528 names).  alive2 is handed a
# TargetLibraryInfo, so these names carry semantics: renaming one turns a
# modeled library call into an opaque external call, which quietly changes what
# a test exercises.  Regenerate with:
#   python3 -c "import re;s=open(INC).read();\
#   b=re.search(r'StandardNamesStrTableStorage.. =(.*?);',s,re.S).group(1);\
#   print(' '.join(sorted(x for x in ''.join(re.findall(r'\"((?:[^\"\\\\]|\\\\.)*)\"',b)).split(chr(92)+'0') if x)))"
LIBFUNCS = frozenset("""
    ??2@YAPAXI@Z ??2@YAPAXIABUnothrow_t@std@@@Z ??2@YAPEAX_K@Z
    ??2@YAPEAX_KAEBUnothrow_t@std@@@Z ??3@YAXPAX@Z
    ??3@YAXPAXABUnothrow_t@std@@@Z ??3@YAXPAXI@Z ??3@YAXPEAX@Z
    ??3@YAXPEAXAEBUnothrow_t@std@@@Z ??3@YAXPEAX_K@Z ??_U@YAPAXI@Z
    ??_U@YAPAXIABUnothrow_t@std@@@Z ??_U@YAPEAX_K@Z
    ??_U@YAPEAX_KAEBUnothrow_t@std@@@Z ??_V@YAXPAX@Z
    ??_V@YAXPAXABUnothrow_t@std@@@Z ??_V@YAXPAXI@Z ??_V@YAXPEAX@Z
    ??_V@YAXPEAXAEBUnothrow_t@std@@@Z ??_V@YAXPEAX_K@Z _Exit _IO_getc _IO_putc
    _ZSt9terminatev _ZdaPv _ZdaPvRKSt9nothrow_t _ZdaPvSt11align_val_t
    _ZdaPvSt11align_val_tRKSt9nothrow_t _ZdaPvj _ZdaPvjSt11align_val_t _ZdaPvm
    _ZdaPvmSt11align_val_t _ZdlPv _ZdlPvRKSt9nothrow_t _ZdlPvSt11align_val_t
    _ZdlPvSt11align_val_tRKSt9nothrow_t _ZdlPvj _ZdlPvjSt11align_val_t _ZdlPvm
    _ZdlPvmSt11align_val_t _Znaj _ZnajRKSt9nothrow_t _ZnajSt11align_val_t
    _ZnajSt11align_val_tRKSt9nothrow_t _Znam _Znam12__hot_cold_t
    _ZnamRKSt9nothrow_t _ZnamRKSt9nothrow_t12__hot_cold_t _ZnamSt11align_val_t
    _ZnamSt11align_val_t12__hot_cold_t _ZnamSt11align_val_tRKSt9nothrow_t
    _ZnamSt11align_val_tRKSt9nothrow_t12__hot_cold_t _Znwj _ZnwjRKSt9nothrow_t
    _ZnwjSt11align_val_t _ZnwjSt11align_val_tRKSt9nothrow_t _Znwm
    _Znwm12__hot_cold_t _ZnwmRKSt9nothrow_t _ZnwmRKSt9nothrow_t12__hot_cold_t
    _ZnwmSt11align_val_t _ZnwmSt11align_val_t12__hot_cold_t
    _ZnwmSt11align_val_tRKSt9nothrow_t
    _ZnwmSt11align_val_tRKSt9nothrow_t12__hot_cold_t __acos_finite
    __acosf_finite __acosh_finite __acoshf_finite __acoshl_finite
    __acosl_finite __asin_finite __asinf_finite __asinl_finite __atan2_finite
    __atan2f_finite __atan2l_finite __atanh_finite __atanhf_finite
    __atanhl_finite __atomic_load __atomic_store __cosh_finite __coshf_finite
    __coshl_finite __cospi __cospif __cxa_atexit __cxa_guard_abort
    __cxa_guard_acquire __cxa_guard_release __cxa_throw __exp10_finite
    __exp10f_finite __exp10l_finite __exp2_finite __exp2f_finite
    __exp2l_finite __exp_finite __expf_finite __expl_finite __isoc99_scanf
    __isoc99_sscanf __log10_finite __log10f_finite __log10l_finite
    __log2_finite __log2f_finite __log2l_finite __log_finite __logf_finite
    __logl_finite __memccpy_chk __memcpy_chk __memmove_chk __mempcpy_chk
    __memset_chk __nvvm_reflect __pow_finite __powf_finite __powl_finite
    __sincospi_stret __sincospif_stret __sinh_finite __sinhf_finite
    __sinhl_finite __sinpi __sinpif __size_returning_new
    __size_returning_new_aligned __size_returning_new_aligned_hot_cold
    __size_returning_new_hot_cold __small_fprintf __small_printf
    __small_sprintf __snprintf_chk __sprintf_chk __sqrt_finite __sqrtf_finite
    __sqrtl_finite __stpcpy_chk __stpncpy_chk __strcat_chk __strcpy_chk
    __strdup __strlcat_chk __strlcpy_chk __strlen_chk __strncat_chk
    __strncpy_chk __strndup __strtok_r __vsnprintf_chk __vsprintf_chk abort
    abs access acos acosf acosh acoshf acoshl acosl aligned_alloc asin asinf
    asinh asinhf asinhl asinl atan atan2 atan2f atan2l atanf atanh atanhf
    atanhl atanl atexit atof atoi atol atoll bcmp bcopy bzero cabs cabsf cabsl
    calloc cbrt cbrtf cbrtl ceil ceilf ceill chmod chown clearerr closedir
    copysign copysignf copysignl cos cosf cosh coshf coshl cosl ctermid erf
    erff erfl execl execle execlp execv execvP execve execvp execvpe exit exp
    exp10 exp10f exp10l exp2 exp2f exp2l expf expl expm1 expm1f expm1l fabs
    fabsf fabsl fclose fdim fdimf fdiml fdopen feof ferror fflush ffs ffsl
    ffsll fgetc fgetc_unlocked fgetpos fgets fgets_unlocked fileno fiprintf
    flockfile floor floorf floorl fls flsl flsll fmax fmaxf fmaximum_num
    fmaximum_numf fmaximum_numl fmaxl fmin fminf fminimum_num fminimum_numf
    fminimum_numl fminl fmod fmodf fmodl fopen fopen64 fork fprintf fputc
    fputc_unlocked fputs fputs_unlocked fread fread_unlocked free frexp frexpf
    frexpl fscanf fseek fseeko fseeko64 fsetpos fstat fstat64 fstatvfs
    fstatvfs64 ftell ftello ftello64 ftrylockfile funlockfile fwrite
    fwrite_unlocked getc getc_unlocked getchar getchar_unlocked getenv
    getitimer getlogin_r getpwnam gets gettimeofday htonl htons hypot hypotf
    hypotl ilogb ilogbf ilogbl iprintf isascii isdigit labs lchown ldexp
    ldexpf ldexpl llabs log log10 log10f log10l log1p log1pf log1pl log2 log2f
    log2l logb logbf logbl logf logl lstat lstat64 malloc memalign memccpy
    memchr memcmp memcpy memmove mempcpy memrchr memset memset_pattern16
    memset_pattern4 memset_pattern8 mkdir mktime modf modff modfl nan nanf
    nanl nearbyint nearbyintf nearbyintl nextafter nextafterf nextafterl
    nexttoward nexttowardf nexttowardl ntohl ntohs open open64 opendir pclose
    perror popen posix_memalign pow powf powl pread printf putc putc_unlocked
    putchar putchar_unlocked puts pvalloc pwrite qsort read readlink realloc
    reallocarray reallocf realpath remainder remainderf remainderl remove
    remquo remquof remquol rename rewind rint rintf rintl rmdir round
    roundeven roundevenf roundevenl roundf roundl scalbln scalblnf scalblnl
    scalbn scalbnf scalbnl scanf setbuf setitimer setvbuf sin sincos sincosf
    sincosl sinf sinh sinhf sinhl sinl siprintf snprintf sprintf sqrt sqrtf
    sqrtl sscanf stat stat64 statvfs statvfs64 stpcpy stpncpy strcasecmp
    strcat strchr strcmp strcoll strcpy strcspn strdup strlcat strlcpy strlen
    strncasecmp strncat strncmp strncpy strndup strnlen strpbrk strrchr strspn
    strstr strtod strtof strtok strtok_r strtol strtold strtoll strtoul
    strtoull strxfrm system tan tanf tanh tanhf tanhl tanl tgamma tgammaf
    tgammal times tmpfile tmpfile64 toascii trunc truncf truncl uname ungetc
    unlink unsetenv utime utimes valloc vec_calloc vec_free vec_malloc
    vec_realloc vfprintf vfscanf vprintf vscanf vsnprintf vsprintf vsscanf
    wcslen write
""".split())


def is_preserved_global(name):
    """True for names whose spelling is load-bearing, so must not be renamed.

    Three groups: LLVM intrinsics and reserved globals (`llvm.*`, which covers
    llvm.used / llvm.global_ctors too), library functions LLVM models through
    TargetLibraryInfo, and reserved identifiers (`__*`) such as the compiler-rt
    runtime calls a backend emits (__muldi3, __divdi3, ...).
    """
    return (name.startswith("llvm.") or name.startswith("__")
            or name in LIBFUNCS)


def rename_globals(ir, keep, preserved=is_preserved_global):
    """Renumber globals to @g0, @g1, ... leaving `keep` and preserved names.

    Covers every global: variables, function definitions and declarations,
    aliases and ifuncs.  Names are assigned in order of first appearance so two
    structurally identical modules normalize to the same text.  A comdat sharing
    a renamed global's name follows it, the way an implicit comdat must.
    """
    _, names = walk_ir(ir)
    taken = {nm for sig, nm in names if sig == '@'}
    comdats = {nm for sig, nm in names if sig == '$'}
    mapping = {}
    n = 0
    for sig, nm in names:
        if sig != '@' or nm == keep or preserved(nm):
            continue
        while True:
            fresh = "g%d" % n
            n += 1
            if fresh not in taken:
                break
        taken.add(fresh)
        mapping[('@', nm)] = fresh
        if nm in comdats:
            mapping[('$', nm)] = fresh
    return walk_ir(ir, mapping)[0] if mapping else ir


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


def run_cmd(cmd, timeout_s, input_text=None):
    """Run cmd and return (returncode_or_None, stdout, stderr, timed_out).

    stdout and stderr are kept apart on purpose: llvm-extract, llvm-dis and opt
    all write IR to stdout and diagnostics to stderr, and merging the two splices
    warning text into the middle of the IR.  On timeout the whole process group
    is killed, so any children die too.
    """
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            start_new_session=True)
    except OSError as e:
        return None, "", "failed to exec %s: %s" % (cmd[0], e), False
    data = input_text.encode() if input_text is not None else None
    try:
        out, err = p.communicate(input=data, timeout=timeout_s)
        return (p.returncode, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"), False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = p.communicate(timeout=15)
        except Exception:
            out = err = b""
        return (None, out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"), True)


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
            rc, out, _, to = run_cmd([self.args.llvm_dis, "-o", "-", path],
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
        rc, out, _, to = run_cmd(
            [self.args.llvm_extract, "-S", "-func", func, "-o", "-", src],
            self.args.tool_timeout)
        if to:
            self.stats.bump("extract_timeout")
            return None
        if rc != 0 or not out.strip():
            self.stats.bump("extract_failed")
            return None

        if not self.args.no_strip:
            # `strip` drops debug info and all value names in one pass.  It also
            # blanks the names of local-linkage globals, so the function may come
            # back numbered; its new name is re-read from the output below.
            rc, out2, _, to = run_cmd(
                [self.args.opt, "-passes=strip", "-S", "-o", "-", "-"],
                self.args.tool_timeout, input_text=out)
            if to:
                self.stats.bump("strip_timeout")
                return None
            if rc != 0 or not out2.strip():
                self.stats.bump("strip_failed")
                return None
            out = out2

        ir = STRIP_RE.sub("", out)
        if not self.args.no_strip:
            ir = strip_debug_module_flags(ir)
        m = FUNC_DEF_RE.search(ir)
        if m is None:
            self.stats.bump("extract_empty")
            return None
        func = unquote_llvm_name(m.group("name"))
        if func is None:
            self.stats.bump("skipped_unrepresentable_name")
            return None

        if self.args.max_insts:
            if count_instructions(ir) > self.args.max_insts:
                self.stats.bump("too_many_instructions")
                return None
        name = func
        if not self.args.keep_names:
            renamed = normalize_function_name(ir, func, self.args.func_name)
            if renamed is None:
                self.stats.bump("rename_failed")
                return None
            ir, name = renamed, self.args.func_name
        if not self.args.keep_global_names:
            ir = rename_globals(ir, name)
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
            rc, out, err, timed_out = run_cmd(
                [self.args.backend_tv,
                 "-backend=" + self.args.backend,
                 "--smt-to=%d" % int(self.args.smt_timeout * 1000),
                 "--fn=" + fn_name,
                 tmp],
                self.args.hard_timeout)
            out = out + err
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
                    help="fallback directory for llvm-extract/llvm-dis/opt")
    ap.add_argument("--tmp-dir", default=None,
                    help="scratch directory for candidate files")
    ap.add_argument("--only-tests", action="store_true",
                    help="only consider files under a */test/* path")
    ap.add_argument("--max-insts", type=int, default=15,
                    help="drop functions with more instructions than this "
                         "before running backend-tv (0 = no limit)")
    ap.add_argument("--func-name", default="f",
                    help="rename every extracted function to this name")
    ap.add_argument("--no-strip", action="store_true",
                    help="skip `opt -passes=strip`, keeping debug info and "
                         "SSA value names")
    ap.add_argument("--keep-global-names", action="store_true",
                    help="keep original global names; by default they are "
                         "renumbered to @g0, @g1, ... except intrinsics, "
                         "TargetLibraryInfo library functions and reserved "
                         "__ names")
    ap.add_argument("--libfuncs-file", default=None,
                    help="file of extra global names to preserve, one per line")
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
    args.opt = find_tool("opt", hints)
    for t in (args.llvm_extract, args.llvm_dis, args.opt):
        if not shutil.which(t):
            sys.exit("cannot find %s (use --llvm-bin)" % t)
    if args.libfuncs_file:
        global LIBFUNCS
        try:
            with open(os.path.expanduser(args.libfuncs_file)) as f:
                extra = set(f.read().split())
        except OSError as e:
            sys.exit("cannot read --libfuncs-file: %s" % e)
        LIBFUNCS = frozenset(LIBFUNCS | extra)
        sys.stderr.write("preserving %d library names (%d from %s)\n"
                         % (len(LIBFUNCS), len(extra), args.libfuncs_file))
    if args.max_insts < 0:
        sys.exit("--max-insts must not be negative")
    if args.name_bytes < 4:
        sys.exit("--name-bytes must be at least 4")
    if not re.fullmatch(r"[-a-zA-Z$._][-a-zA-Z$._0-9]*", args.func_name):
        sys.exit("--func-name must be a bare LLVM identifier: %s" % args.func_name)
    if args.tmp_dir:
        os.makedirs(args.tmp_dir, exist_ok=True)

    return Harvester(args).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
