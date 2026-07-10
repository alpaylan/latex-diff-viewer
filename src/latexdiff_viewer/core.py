"""Project-agnostic build core: git-latexdiff diffs + full builds + change index.

Ported from the thesis-specific difftool/server.py, but every path/command that
used to be hardcoded now comes from a `config.Config`:

  * diff build  -> `git latexdiff --latexmk` with the project's build_dir +
                   latexdiff_options + the change-index --filter.
  * full build  -> the project's *own* build_command, run in a detached worktree.

Both the local server and the CI CLI call these; the only difference is the
`on_line` sink they pass for streamed build output.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable

from . import config as _config

HERE = os.path.dirname(os.path.abspath(__file__))
BOOKMARK_FILTER = os.path.join(HERE, "diff_bookmarks.py")

# A hung build (latex/git waiting on a prompt) is killed after this.
BUILD_TIMEOUT = 420  # seconds

LineSink = Callable[[str], None]


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def git(repo_root: str, *args: str, timeout: int = 120) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # never block on a credential prompt
    return subprocess.check_output(
        ["git", *args], cwd=repo_root, text=True, encoding="utf-8",
        errors="replace", stderr=subprocess.STDOUT, env=env, timeout=timeout)


def list_commits(repo_root: str, limit: int = 200) -> list[dict]:
    # hash <US> short <US> author <US> local date+time <US> subject
    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s"
    out = git(repo_root, "log", f"-{limit}",
              "--date=format-local:%Y-%m-%d %H:%M", f"--pretty=format:{fmt}")
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        full, short, author, date, subject = line.split("\x1f")
        commits.append({"hash": full, "short": short, "author": author,
                        "date": date, "subject": subject})
    return commits


def commit_map(repo_root: str) -> dict[str, dict]:
    """Map both full and short hashes -> {short, subject, date}."""
    m: dict[str, dict] = {}
    try:
        for c in list_commits(repo_root, 500):
            info = {"short": c["short"], "subject": c["subject"], "date": c["date"]}
            m[c["short"]] = info
            m[c["hash"]] = info
    except Exception:  # noqa: BLE001
        pass
    return m


def rev_parse(repo_root: str, ref: str) -> str:
    return git(repo_root, "rev-parse", ref).strip()


def github_remote(repo_root: str) -> str | None:
    """"owner/repo" parsed from the origin remote, or None if it isn't GitHub."""
    try:
        url = git(repo_root, "remote", "get-url", "origin").strip()
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def recent_commit_pairs(repo_root: str, n: int) -> list[list[str]]:
    """[[parent, commit], ...] for the last n commits that have a parent, newest
    first. Used to pre-build a per-commit diff history for the Pages viewer."""
    try:
        out = git(repo_root, "log", f"-{max(int(n), 0)}", "--pretty=%H %P")
    except Exception:  # noqa: BLE001
        return []
    pairs: list[list[str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:                 # commit + at least a first parent
            commit, first_parent = parts[0], parts[1]
            pairs.append([first_parent, commit])
    return pairs


def commit_info(repo_root: str, ref: str) -> dict:
    """Resolve an arbitrary ref (sha/tag/branch/HEAD) to {hash, short, date, subject}."""
    fmt = "%H%x1f%h%x1f%ad%x1f%s"
    out = git(repo_root, "show", "-s", "--date=format-local:%Y-%m-%d %H:%M",
              f"--format={fmt}", ref)
    full, short, date, subject = out.strip().split("\x1f")
    return {"hash": full, "short": short, "date": date, "subject": subject}


def ensure_ref(repo_root: str, ref: str) -> None:
    """Best-effort: fetch `ref` if the checkout doesn't already have it (shallow CI)."""
    try:
        git(repo_root, "cat-file", "-e", f"{ref}^{{commit}}")
        return
    except Exception:  # noqa: BLE001
        pass
    for args in (("fetch", "--no-tags", "--depth=2147483647", "origin", ref),
                 ("fetch", "--no-tags", "origin", ref)):
        try:
            git(repo_root, *args, timeout=180)
            return
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# Change index (parses records the --filter wrote into the .aux)
# ---------------------------------------------------------------------------

CHGMETA_RE = re.compile(r"\\difchgmeta\{(\d+)\}\{(add|del)\}")
ZREF_RE = re.compile(r"\\zref@newlabel\{difchg(\d+)\}\{")
ABSPAGE_RE = re.compile(r"\\abspage\{(\d+)\}")
ZPAGE_RE = re.compile(r"\\page\{([^}]*)\}")


def parse_changes(aux_path: str) -> list[dict]:
    """Collapse the change records to one entry per changed page.

    Each change writes a type record (\\difchgmeta{N}{add|del}) and a zref label
    (difchgN) whose page/abspage are resolved at *shipout* — so floated content
    (tables/figures/landscape) reports the page it lands on, not the source
    location. We join the two by N and group by physical page. A change in a
    heading fires several times (body/TOC/running head), hence the grouping."""
    try:
        with open(aux_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []

    types = {int(m.group(1)): m.group(2) for m in CHGMETA_RE.finditer(text)}
    # page/abspage per change id, from its zref label.
    loc: dict[int, tuple[int, str]] = {}
    for m in ZREF_RE.finditer(text):
        n = int(m.group(1))
        body = text[m.end():m.end() + 240]        # properties follow the label name
        ap = ABSPAGE_RE.search(body)
        if not ap:
            continue
        pg = ZPAGE_RE.search(body)
        loc[n] = (int(ap.group(1)), pg.group(1) if pg else str(int(ap.group(1))))

    pages: dict[int, dict] = {}
    for n, typ in types.items():
        if n not in loc:
            continue
        abspage, page = loc[n]
        e = pages.setdefault(abspage, {"abspage": abspage, "page": page, "types": set()})
        e["types"].add(typ)
    out = []
    for i, e in enumerate(sorted(pages.values(), key=lambda x: x["abspage"]), 1):
        types_ = e["types"]
        typ = "both" if len(types_) > 1 else next(iter(types_))
        out.append({"n": i, "type": typ, "abspage": e["abspage"], "page": e["page"]})
    return out


# ---------------------------------------------------------------------------
# Assets: mirror gitignored-but-present files (e.g. figure PDFs) into a checkout
# ---------------------------------------------------------------------------

def copy_assets(cfg: _config.Config, dest_root: str) -> None:
    """Copy files matching cfg.untracked_assets from the working tree into a
    checkout, the same way git-latexdiff's --ln-untracked does for the diff."""
    for pattern in cfg.untracked_assets:
        for src in glob.glob(os.path.join(cfg.repo_root, pattern), recursive=True):
            if not os.path.isfile(src):
                continue
            rel = os.path.relpath(src, cfg.repo_root)
            dst = os.path.join(dest_root, rel)
            if not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Process runner
# ---------------------------------------------------------------------------

def _run_streamed(cmd: list[str], cwd: str, on_line: LineSink | None = None,
                  timeout: int = BUILD_TIMEOUT) -> tuple[int, list[str]]:
    """Run cmd, stream stdout+stderr into `on_line`, kill it if it hangs."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.Popen(cmd, cwd=cwd, text=True, encoding="utf-8",
                            errors="replace", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timed_out = {"v": False}

    def _kill():
        timed_out["v"] = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()
    log_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            log_lines.append(line)
            if on_line:
                on_line(line)
        rc = proc.wait()
    finally:
        watchdog.cancel()
    if timed_out["v"]:
        msg = f"[latex-diff-viewer] build exceeded {timeout}s and was killed."
        log_lines.append(msg)
        if on_line:
            on_line(msg)
        rc = -99
    return rc, log_lines


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------

class BuildResult:
    def __init__(self, ok: bool, rc: int, out_pdf: str,
                 log: list[str], changes: list[dict] | None = None,
                 elapsed: float = 0.0, error_log: str | None = None):
        self.ok = ok
        self.rc = rc
        self.out_pdf = out_pdf
        self.log = log
        self.changes = changes or []
        self.elapsed = elapsed
        self.error_log = error_log     # salvaged LaTeX .log path, on failure


def _salvage_log(cfg: _config.Config, prefix: str, out_pdf: str,
                 on_line: LineSink | None) -> str | None:
    """After a failed diff compile, rescue the LaTeX .log before the work
    tree is deleted (git-latexdiff says 'please examine' — but we clean up),
    and surface its error lines. Returns the saved log's path."""
    logs = []
    if cfg.build_dir:
        p = os.path.join(cfg.build_path, f"{cfg.jobname}.log")
        if os.path.exists(p):
            logs.append(p)
    for root, _dirs, files in os.walk(prefix, followlinks=True):
        logs += [os.path.join(root, f) for f in files if f.endswith(".log")]
    if not logs:
        return None
    # latexmk's wrapper log can be newer than the engine's — prefer the
    # {jobname}.log (the one with the actual "!" error lines), then mtime.
    job = f"{cfg.jobname}.log"
    newest = max(logs, key=lambda p: (os.path.basename(p) == job,
                                      os.path.getmtime(p)))
    dest = os.path.splitext(out_pdf)[0] + ".log"
    try:
        shutil.copy2(newest, dest)
    except OSError:
        return None
    if on_line:
        try:
            with open(dest, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        shown = 0
        for i, ln in enumerate(lines):
            if ln.startswith("!") and shown < 40:
                for ctx in lines[i:i + 3]:
                    on_line(f"[latex] {ctx}")
                    shown += 1
        on_line(f"[latex] full log: {dest}")
    return dest


def _harvest_diff(cfg: _config.Config, prefix: str) -> tuple[str | None, str | None]:
    """Find the diff PDF and the .aux (the one carrying our \\difchgmeta records)
    in git-latexdiff's preserved work tree under `prefix`.

    A project that builds into an out_dir writes — via the --ln-untracked symlink
    — to the *real* working-tree build dir (outside `prefix`); check that first.
    Otherwise the outputs are real files inside `prefix`."""
    jobname = cfg.jobname

    def has_records(path: str) -> bool:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return "\\difchgmeta" in fh.read()
        except OSError:
            return False

    # Fast path for build_dir projects: latexmk writes (through --ln-untracked's
    # symlink) to the *real* working-tree build dir, outside `prefix`.
    aux: str | None = None
    pdfs: list[str] = []
    if cfg.build_dir:
        if os.path.exists(cfg.aux_path) and has_records(cfg.aux_path):
            aux = cfg.aux_path
        bp = os.path.join(cfg.build_path, f"{jobname}.pdf")
        if os.path.exists(bp):
            pdfs.append(bp)

    # Walk the preserved checkout. followlinks=True so we descend through the
    # new/build symlink (-> real build dir) when the fast path didn't catch it.
    for root, _dirs, files in os.walk(prefix, followlinks=True):
        for f in files:
            p = os.path.join(root, f)
            if f == f"{jobname}.pdf":
                pdfs.append(p)
            elif aux is None and f.endswith(".aux") and has_records(p):
                aux = p
    if not pdfs:  # any PDF, as a last resort
        for root, _dirs, files in os.walk(prefix, followlinks=True):
            pdfs += [os.path.join(root, f) for f in files if f.endswith(".pdf")]
    # git-latexdiff compiles the diff under a "new/" dir; prefer that, then newest.
    pdfs.sort(key=lambda p: ((os.sep + "new" + os.sep) in p, os.path.getmtime(p)))
    return (pdfs[-1] if pdfs else None), aux


def build_diff(cfg: _config.Config, old: str, new: str, out_pdf: str,
               on_line: LineSink | None = None,
               timeout: int = BUILD_TIMEOUT) -> BuildResult:
    """git-latexdiff between two commits, with the per-change index --filter.

    We run with --no-cleanup + our own --tmpdirprefix instead of `-o` (which would
    imply --cleanup all and delete the .aux). Then we harvest the PDF and the .aux
    (carrying the filter's \\difchgmeta records) from the preserved work tree, so
    the changed-pages index works for *any* project — no build_dir required."""
    out_pdf = os.path.abspath(out_pdf)
    os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
    if os.path.exists(out_pdf):
        os.remove(out_pdf)
    if cfg.build_dir:
        # Exists so --ln-untracked symlinks it into the checkout (thesis-style).
        os.makedirs(cfg.build_path, exist_ok=True)
        if os.path.exists(cfg.aux_path):
            os.remove(cfg.aux_path)

    prefix = tempfile.mkdtemp(prefix="ldv-gld-")
    cmd = ["git", "latexdiff", "--main", cfg.main, "--latexmk"]
    if cfg.build_dir:
        cmd += ["--build-dir", cfg.build_dir]
    cmd += [
        "--ln-untracked",                       # pull in gitignored assets
        *cfg.latexdiff_options,
        "--filter", f"python3 {BOOKMARK_FILTER} {cfg.main}",
        # Overleaf-style tolerance: push through recoverable errors instead of
        # relying on errorstop+EOF behavior; with --ignore-latex-errors the
        # run still fails (rc!=0) when no PDF ships, which is what ok keys on.
        "--latexopt", "-f -interaction=nonstopmode",
        "--ignore-latex-errors",
        "--no-view",
        "--quiet",
        "--tmpdirprefix", prefix,               # keep the work tree where we can find it
        "--no-cleanup",                         # ...and don't delete it (that keeps the .aux)
        old, new,
    ]
    start = time.time()
    try:
        rc, log_lines = _run_streamed(cmd, cfg.repo_root, on_line, timeout)
        # Harvest only on success: a checkout can contain committed PDFs
        # (stale builds, figures), so after a failed compile the walk would
        # happily return one of those as "the diff" (ok=true, 0 changes).
        src_pdf, aux = _harvest_diff(cfg, prefix) if rc == 0 else (None, None)
        if src_pdf and os.path.exists(src_pdf):
            shutil.copy2(src_pdf, out_pdf)
        changes = parse_changes(aux) if (aux and os.path.exists(out_pdf)) else []
        error_log = None if rc == 0 else _salvage_log(cfg, prefix, out_pdf, on_line)
    finally:
        shutil.rmtree(prefix, ignore_errors=True)
        if cfg.build_dir:  # tidy artifacts the symlink left in the real build dir
            for f in glob.glob(os.path.join(cfg.build_path, f"{cfg.jobname}.*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
    return BuildResult(os.path.exists(out_pdf), rc, out_pdf, log_lines, changes,
                       round(time.time() - start, 1), error_log)


def _find_output_pdf(cfg: _config.Config, wt: str, start: float) -> str | None:
    """Locate the PDF the build_command produced inside the worktree.

    Prefer the configured output_pdf, then {jobname}.pdf anywhere, then the
    newest PDF written during the build — so we don't hardcode where latexmk
    (or make, or tectonic) happens to drop it."""
    exact = os.path.join(wt, cfg.output_pdf)
    if os.path.exists(exact):
        return exact
    named = glob.glob(os.path.join(wt, "**", f"{cfg.jobname}.pdf"), recursive=True)
    if named:
        return max(named, key=os.path.getmtime)
    produced = [p for p in glob.glob(os.path.join(wt, "**", "*.pdf"), recursive=True)
                if os.path.getmtime(p) >= start - 1]
    return max(produced, key=os.path.getmtime) if produced else None


def build_full(cfg: _config.Config, commit: str, out_pdf: str,
               on_line: LineSink | None = None,
               timeout: int = BUILD_TIMEOUT) -> BuildResult:
    """Plain (no-diff) build of a single commit using the project's build_command.

    Checks the commit out into a detached worktree, mirrors untracked assets in,
    runs the configured build, then copies the project's output_pdf out."""
    out_pdf = os.path.abspath(out_pdf)
    os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
    if os.path.exists(out_pdf):
        os.remove(out_pdf)

    wt = tempfile.mkdtemp(prefix="ldv-full-")
    start = time.time()
    log_lines: list[str] = []
    rc = -1
    try:
        git(cfg.repo_root, "worktree", "add", "--detach", "--force", wt, commit)
        copy_assets(cfg, wt)
        rc, log_lines = _run_streamed(cfg.build_command, wt, on_line, timeout)
        built = _find_output_pdf(cfg, wt, start)
        if built:
            shutil.copy2(built, out_pdf)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt],
                       cwd=cfg.repo_root, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
        shutil.rmtree(wt, ignore_errors=True)
    return BuildResult(os.path.exists(out_pdf), rc, out_pdf, log_lines, [],
                       round(time.time() - start, 1))
