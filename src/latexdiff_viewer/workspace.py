"""Personal save timeline: snapshot any LaTeX folder into a shadow git repo.

`ldv save` gives folders that aren't (usable) git repos — an Overleaf source
zip, a synced directory, a plain folder — a diffable history without touching
the project's own git, if it has one. Each save is a commit in a per-project
shadow repo under the user's state dir, tagged s1, s2, …; everything
downstream (build-diff, serve, store) already works on git repos, so the
shadow repo plugs straight in as a repo_root.

Layout (under $XDG_STATE_HOME, default ~/.local/state):
    ldv/<project-id>/repo/        the shadow repo (worktree + .git)
    ldv/<project-id>/state.json   origin + bookkeeping
`project-id` hashes the project folder's real path, so `ldv` commands run
from the same folder always find the same timeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone

from . import config as _config
from . import core

# Never snapshotted: VCS internals, OS/editor noise, this tool's own output.
SKIP_NAMES = {".git", ".hg", ".svn", ".DS_Store", "latexdiff-out"}
# LaTeX build residue: harmless to commit, but bloats every save.
SKIP_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk",
    ".synctex.gz", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".vrb", ".xdv",
)


# ---------------------------------------------------------------------------
# State dir
# ---------------------------------------------------------------------------

def state_root() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "ldv")


def project_id(project_dir: str) -> str:
    real = os.path.realpath(os.path.abspath(project_dir))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


def project_state_dir(project_dir: str) -> str:
    return os.path.join(state_root(), project_id(project_dir))


def shadow_root(project_dir: str) -> str:
    return os.path.join(project_state_dir(project_dir), "repo")


def load_state(project_dir: str) -> dict:
    try:
        with open(os.path.join(project_state_dir(project_dir), "state.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(project_dir: str, state: dict) -> None:
    d = project_state_dir(project_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Shadow repo
# ---------------------------------------------------------------------------

def ensure_shadow(project_dir: str) -> str:
    """The shadow repo path, initialised on first use."""
    repo = shadow_root(project_dir)
    if not os.path.isdir(os.path.join(repo, ".git")):
        os.makedirs(repo, exist_ok=True)
        core.git(repo, "init", "-q")
        # Commits need an identity; keep it repo-local so the user's global
        # git config is never touched (or required to exist).
        core.git(repo, "config", "user.email", "ldv@latex-diff-viewer")
        core.git(repo, "config", "user.name", "ldv")
    return repo


def _head(repo: str) -> str | None:
    try:
        return core.git(repo, "rev-parse", "--verify", "-q", "HEAD").strip()
    except subprocess.CalledProcessError:
        return None


def _config_skips(src_dir: str) -> set[str]:
    """Worktree-relative paths the project's config marks as build output."""
    cfg = _config.load(src_dir)
    skips = set()
    if cfg.build_dir:
        skips.add(os.path.normpath(cfg.build_dir))
    if cfg.output_pdf:
        skips.add(os.path.normpath(cfg.output_pdf))
    return skips


def _ignore(src_root: str, skip_rel: set[str]):
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        rel = os.path.relpath(dirpath, src_root)
        rel = "" if rel == "." else rel
        skipped = set()
        for n in names:
            r = os.path.join(rel, n) if rel else n
            if n in SKIP_NAMES or n.endswith(SKIP_SUFFIXES) or r in skip_rel:
                skipped.add(n)
        return skipped
    return ignore


def _sync_tree(src_dir: str, worktree: str) -> None:
    """Make the worktree mirror src_dir (deletions included), minus skips."""
    for name in os.listdir(worktree):
        if name == ".git":
            continue
        p = os.path.join(worktree, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
    shutil.copytree(src_dir, worktree, dirs_exist_ok=True,
                    ignore=_ignore(src_dir, _config_skips(src_dir)))


def _extract_zip(zip_path: str, dest: str) -> str:
    """Safe-extract into dest; returns the tree root. Descends through a single
    wrapping directory so GitHub-style zips look like Overleaf's flat ones."""
    with zipfile.ZipFile(zip_path) as zf:
        base = os.path.realpath(dest)
        for m in zf.namelist():
            target = os.path.realpath(os.path.join(dest, m))
            if os.path.commonpath([base, target]) != base:
                raise ValueError(f"unsafe path in zip: {m!r}")
        zf.extractall(dest)
    entries = [e for e in os.listdir(dest) if e not in SKIP_NAMES]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        return os.path.join(dest, entries[0])
    return dest


# ---------------------------------------------------------------------------
# Saves
# ---------------------------------------------------------------------------

def save(project_dir: str, message: str | None = None,
         src: str | None = None) -> dict:
    """Snapshot `src` (default: the project folder; a folder or a source zip)
    as the next save. No-op if nothing changed since the last save."""
    project_dir = os.path.abspath(project_dir)
    repo = ensure_shadow(project_dir)
    tmp = None
    try:
        source = project_dir if src is None else os.path.abspath(src)
        if src is not None and zipfile.is_zipfile(source):
            tmp = tempfile.mkdtemp(prefix="ldv-zip-")
            source = _extract_zip(source, tmp)
        elif not os.path.isdir(source):
            return {"ok": False, "error": f"not a folder or zip: {source}"}

        _sync_tree(source, repo)
        core.git(repo, "add", "-A")
        if _head(repo) and not core.git(repo, "status", "--porcelain").strip():
            seq = int(core.git(repo, "rev-list", "--count", "HEAD").strip())
            return {"ok": True, "id": f"s{seq}", "unchanged": True,
                    "commit": _head(repo)[:10]}

        msg = message or (f"import {os.path.basename(src)}" if src else "save")
        core.git(repo, "commit", "-q", "-m", msg)
        seq = int(core.git(repo, "rev-list", "--count", "HEAD").strip())
        sid = f"s{seq}"
        core.git(repo, "tag", sid)
        files = [ln for ln in core.git(repo, "show", "--name-only",
                                       "--format=", "HEAD").splitlines() if ln]

        st = load_state(project_dir)
        st.setdefault("origin", {"kind": "folder", "path": project_dir})
        st["saves"] = seq
        st["last_save"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_state(project_dir, st)
        return {"ok": True, "id": sid, "commit": _head(repo)[:10],
                "message": msg, "files": len(files)}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def timeline(project_dir: str) -> list[dict]:
    """Saves, newest first: [{id, commit, date, message}]."""
    repo = shadow_root(project_dir)
    if not os.path.isdir(os.path.join(repo, ".git")) or not _head(repo):
        return []
    tags = {}
    for line in core.git(repo, "tag", "--list", "s*",
                         "--format=%(refname:short) %(objectname)").splitlines():
        name, sha = line.split()
        tags[sha] = name
    entries = []
    for line in core.git(repo, "log", "--format=%H%x09%h%x09%ct%x09%s").splitlines():
        full, short, ts, subj = line.split("\t", 3)
        entries.append({"id": tags.get(full, short), "commit": short,
                        "date": datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M"),
                        "message": subj})
    return entries
