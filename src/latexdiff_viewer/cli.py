#!/usr/bin/env python3
"""Command-line entrypoint — the Docker image's ENTRYPOINT and the local runner.

    python3 -m latexdiff_viewer.cli build-diff --old A --new B -o out/diff.pdf
    python3 -m latexdiff_viewer.cli build-full --commit HEAD -o out/full.pdf
    python3 -m latexdiff_viewer.cli pages --out site
    python3 -m latexdiff_viewer.cli serve --port 8765
    ldv save / ldv list        # personal save timeline (see workspace.py)

Build instructions are resolved by `config` from a difftool.toml/json in --repo,
overridden by Action inputs (env INPUT_*) and then by any explicit flag here.
`build-diff`/`build-full` print a JSON result on stdout (build logs go to stderr)
and, in CI, write step outputs to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from . import config as _config
from . import core


# ---------------------------------------------------------------------------
# CI helpers
# ---------------------------------------------------------------------------

def _default_repo() -> str:
    return os.environ.get("GITHUB_WORKSPACE") or os.getcwd()


def _event() -> dict:
    p = os.environ.get("GITHUB_EVENT_PATH")
    if p and os.path.isfile(p):
        try:
            with open(p) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


def _pr(section: str) -> str | None:
    return (_event().get("pull_request") or {}).get(section, {}).get("sha")


def default_base() -> str | None:
    return os.environ.get("INPUT_BASE") or _pr("base")


def default_head() -> str | None:
    return (os.environ.get("INPUT_HEAD") or _pr("head")
            or (os.environ.get("GITHUB_SHA") if os.environ.get("GITHUB_SHA") else None))


def mark_safe_directory(repo_root: str) -> None:
    """In CI the workspace is owned by a different uid than git expects."""
    if not os.environ.get("GITHUB_WORKSPACE") and not os.environ.get("CI"):
        return
    for target in (repo_root, "*"):
        try:
            subprocess.run(["git", "config", "--global", "--add",
                            "safe.directory", target],
                           check=False, capture_output=True)
        except Exception:  # noqa: BLE001
            pass


def set_output(name: str, value) -> None:
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def stderr_sink(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Config resolution shared by subcommands
# ---------------------------------------------------------------------------

_OVERRIDE_FLAGS = ("main", "build_dir", "jobname", "output_pdf", "build_command",
                   "latexdiff_options", "pages_pairs")


def add_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=_default_repo(), help="LaTeX project root")
    p.add_argument("--config", default=None, help="path to difftool.toml/json")
    p.add_argument("--main", default=None)
    p.add_argument("--build-dir", dest="build_dir", default=None)
    p.add_argument("--jobname", default=None)
    p.add_argument("--output-pdf", dest="output_pdf", default=None)
    p.add_argument("--build-command", dest="build_command", default=None)
    p.add_argument("--latexdiff-options", dest="latexdiff_options", default=None)
    p.add_argument("--assets", default=None,
                   help="untracked asset globs (comma/newline separated)")
    p.add_argument("--pages-pairs", dest="pages_pairs", default=None)


def resolve(args) -> _config.Config:
    overrides = _config.overrides_from_env()
    for k in _OVERRIDE_FLAGS:
        v = getattr(args, k, None)
        if v is not None:
            overrides[k] = v
    if getattr(args, "assets", None) is not None:
        overrides["untracked_assets"] = args.assets
    cfg = _config.load(args.repo, args.config, overrides)
    mark_safe_directory(cfg.repo_root)
    return cfg


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_build_diff(args) -> int:
    cfg = resolve(args)
    old = args.old or default_base()
    new = args.new or default_head() or "HEAD"
    if not old or not new:
        print(json.dumps({"ok": False,
                          "error": "need --old/--new (or a pull_request event)"}))
        return 2
    core.ensure_ref(cfg.repo_root, old)
    core.ensure_ref(cfg.repo_root, new)
    out = args.out or os.path.join(cfg.repo_root, "latexdiff-out", "diff.pdf")
    res = core.build_diff(cfg, old, new, out, on_line=stderr_sink)
    result = {"ok": res.ok, "rc": res.rc, "pdf": res.out_pdf,
              "elapsed": res.elapsed, "changed_pages": len(res.changes),
              "changes": res.changes}
    print(json.dumps(result))
    set_output("diff_pdf", res.out_pdf)
    set_output("changed_pages", len(res.changes))
    return 0 if res.ok else 1


def cmd_build_full(args) -> int:
    cfg = resolve(args)
    commit = args.commit or default_head() or "HEAD"
    core.ensure_ref(cfg.repo_root, commit)
    out = args.out or os.path.join(cfg.repo_root, "latexdiff-out", "full.pdf")
    res = core.build_full(cfg, commit, out, on_line=stderr_sink)
    result = {"ok": res.ok, "rc": res.rc, "pdf": res.out_pdf, "elapsed": res.elapsed}
    print(json.dumps(result))
    set_output("full_pdf", res.out_pdf)
    return 0 if res.ok else 1


def cmd_pages(args) -> int:
    from . import pages
    cfg = resolve(args)
    out_dir = args.out or os.path.join(cfg.repo_root, "site")
    manifest = pages.generate(cfg, out_dir, on_line=stderr_sink)
    print(json.dumps({"ok": True, "out": out_dir,
                      "diffs": len(manifest.get("diffs", []))}))
    set_output("site_dir", out_dir)
    return 0


def cmd_store_add(args) -> int:
    from . import store
    cfg = resolve(args)
    base = args.old or default_base()
    head = args.new or default_head() or "HEAD"
    if not base or not head:
        print(json.dumps({"ok": False, "error": "need --old and --new"}))
        return 2
    res = store.add_diff(cfg, args.store, base, head,
                         retain=args.retain, on_line=stderr_sink)
    print(json.dumps(res))
    set_output("diff_id", res.get("id", ""))
    set_output("changed_pages", res.get("changed_pages", 0))
    return 0 if res.get("ok") else 1


def cmd_store_seed(args) -> int:
    """Append the pre-generated set (pages_pairs, else the last pages_recent commits
    vs parent) to a store dir — so a push can keep a recent-history viewer in the
    same store the on-demand issue flow writes to."""
    from . import store, core
    cfg = resolve(args)
    pairs = cfg.pages_pairs or core.recent_commit_pairs(cfg.repo_root, cfg.pages_recent)
    built = existing = failed = 0
    for old, new in pairs:
        r = store.add_diff(cfg, args.store, old, new, retain=args.retain, on_line=stderr_sink)
        if not r.get("ok"):
            failed += 1
        elif r.get("existing"):
            existing += 1
        else:
            built += 1
    # Also render the current commit in full (no diff) as the "current draft".
    head = default_head() or "HEAD"
    full = store.add_full(cfg, args.store, head, retain=args.retain, on_line=stderr_sink)
    print(json.dumps({"ok": True, "pairs": len(pairs), "built": built,
                      "existing": existing, "failed": failed, "full": full.get("ok", False)}))
    return 0


def cmd_serve(args) -> int:
    from . import server
    cfg = resolve(args)
    server.configure(cfg)
    server.run_server(args.host, args.port)
    return 0


def _resolve_project(token: str | None) -> str | None:
    """User's --project (name or folder, or nothing) -> project key.
    Prints the error JSON and returns None when it can't be resolved."""
    from . import workspace
    try:
        return workspace.resolve_project(token)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return None


def cmd_save(args) -> int:
    from . import workspace
    # save is folder-centric: bare `ldv save` snapshots the cwd even when
    # linked projects exist; names only matter with an explicit --project.
    project = _resolve_project(args.project) if args.project else os.getcwd()
    if not project:
        return 2
    origin = workspace.load_state(project).get("origin") or {}
    if origin.get("kind") == "git-bridge" or \
            (origin.get("kind") == "read-link" and not args.src):
        print(json.dumps({"ok": False, "error":
                          "linked to Overleaf — use `ldv pull` "
                          "(or `ldv save --from <zip>`)"}))
        return 2
    res = workspace.save(project, message=args.message, src=args.src)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


def cmd_list(args) -> int:
    from . import workspace
    project = _resolve_project(args.project)
    if not project:
        return 2
    entries = workspace.timeline(project)
    if args.json:
        print(json.dumps({"ok": True, "project": workspace.project_name(project),
                          "saves": entries}))
    elif not entries:
        print("no saves yet — run `ldv save` (or `ldv link <read-url>`)")
    else:
        for e in entries:
            print(f"{e['id']:>6}  {e['date']}  {e['message']}")
    return 0


def cmd_projects(args) -> int:
    from . import workspace
    entries = workspace.projects()
    if args.json:
        print(json.dumps({"ok": True, "projects": entries}))
    elif not entries:
        print("no projects yet — `ldv save` in a folder or `ldv link <read-url>`")
    else:
        for p in entries:
            origin = p["origin"]
            where = origin.get("url") or origin.get("path") or p["key"] or "?"
            print(f"{p['name'] or '?':24}  {origin.get('kind', 'folder'):10}  {where}")
    return 0


def _shadow_or_error(project: str) -> str | None:
    from . import workspace
    repo = workspace.shadow_root(project)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(json.dumps({"ok": False,
                          "error": "no saves yet — run `ldv save` first"}))
        return None
    return repo


def _safe_name(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", token)


def cmd_diff(args) -> int:
    from . import workspace
    project = _resolve_project(args.project)
    if not project:
        return 2
    repo = _shadow_or_error(project)
    if not repo:
        return 2
    try:
        a = workspace.resolve_ref(repo, args.a)
        b = workspace.resolve_ref(repo, args.b) if args.b else "HEAD"
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    missing = _missing_diff_tools()
    if missing:
        print(json.dumps({"ok": False, "error":
                          "missing tools for building diffs: "
                          + ", ".join(missing) + " — run `ldv doctor`"}))
        return 2
    if args.share:
        from . import share
        res = share.share_diff(project, a, b, on_line=stderr_sink)
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    cfg = _config.load(repo)
    out = args.out or os.path.join(workspace.project_state_dir(project),
                                   "out", f"diff-{_safe_name(a)}-{_safe_name(b)}.pdf")
    res = core.build_diff(cfg, a, b, out, on_line=stderr_sink)
    result = {"ok": res.ok, "rc": res.rc, "pdf": res.out_pdf,
              "old": a, "new": b, "elapsed": res.elapsed,
              "changed_pages": len(res.changes), "changes": res.changes}
    if not res.ok:
        result["error"] = ("the diff document failed to compile — see the "
                           "[latex] lines above (common: bibliography or "
                           "shell-escape needs, or latexdiff markup breaking "
                           "on complex macros; latexdiff_options in "
                           "difftool.toml can help)")
        if res.error_log:
            result["log"] = res.error_log
    print(json.dumps(result))
    return 0 if res.ok else 1


def cmd_link(args) -> int:
    from . import overleaf
    res = overleaf.link(url=args.url, git_url=args.git, name=args.name)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


def cmd_pull(args) -> int:
    from . import overleaf
    project = _resolve_project(args.project)
    if not project:
        return 2
    res = overleaf.pull(project)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


# What each feature needs on PATH. An engine is checked separately (any of
# several works, and which one matters depends on the project's build_command).
DIFF_TOOLS = ("git-latexdiff", "latexdiff", "latexmk", "latexpand")
ENGINES = ("pdflatex", "xelatex", "lualatex")

DOCTOR_HINTS = {
    "git": "install git",
    "git-latexdiff": "brew install git-latexdiff (macOS) — "
                     "or gitlab.com/git-latexdiff/git-latexdiff",
    "latexdiff": "ships with TeX Live / MacTeX",
    "latexmk": "ships with TeX Live / MacTeX",
    "latexpand": "ships with TeX Live / MacTeX",
    "engine": "install a TeX distribution (TeX Live / MacTeX)",
}


def _missing_diff_tools() -> list[str]:
    missing = [t for t in DIFF_TOOLS if not shutil.which(t)]
    if not any(shutil.which(e) for e in ENGINES):
        missing.append("a LaTeX engine")
    return missing


def cmd_doctor(args) -> int:
    from . import share
    checks = []

    def add(need: str, name: str, ok: bool, hint: str = "") -> None:
        checks.append({"need": need, "name": name, "ok": ok,
                       "hint": "" if ok else hint})

    add("save/pull", f"python {sys.version.split()[0]}",
        sys.version_info >= (3, 11), "need Python 3.11+")
    add("save/pull", "git", bool(shutil.which("git")), DOCTOR_HINTS["git"])
    for t in DIFF_TOOLS:
        add("diff", t, bool(shutil.which(t)), DOCTOR_HINTS[t])
    engines = [e for e in ENGINES if shutil.which(e)]
    add("diff", "LaTeX engine (" + (", ".join(engines) or "none") + ")",
        bool(engines), DOCTOR_HINTS["engine"])
    gh_err = share.ensure_gh()
    add("share", "gh (authenticated)", gh_err is None, gh_err or "")

    ready = {need: all(c["ok"] for c in checks if c["need"] == need)
             for need in ("save/pull", "diff", "share")}
    if args.json:
        print(json.dumps({"ok": ready["save/pull"], "ready": ready,
                          "checks": checks}))
    else:
        for c in checks:
            mark = "ok " if c["ok"] else "MISSING"
            line = f"{c['need']:9} {mark:8} {c['name']}"
            print(line + (f"  — {c['hint']}" if c["hint"] else ""))
        summary = ", ".join(f"{k}: {'ready' if v else 'not ready'}"
                            for k, v in ready.items())
        print(f"\n{summary}")
    return 0 if ready["save/pull"] else 1


def cmd_view(args) -> int:
    from . import server, workspace
    project = _resolve_project(args.project)
    if not project:
        return 2
    repo = _shadow_or_error(project)
    if not repo:
        return 2
    server.configure(_config.load(repo), name=workspace.project_name(project))
    server.run_server(args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="latex-diff-viewer",
                                 description="git-latexdiff for any LaTeX project")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("build-diff", help="latexdiff PDF between two commits")
    add_config_args(d)
    d.add_argument("--old", default=None, help="base ref (default: PR base)")
    d.add_argument("--new", default=None, help="compare ref (default: PR head/HEAD)")
    d.add_argument("-o", "--out", default=None, help="output PDF path")
    d.set_defaults(func=cmd_build_diff)

    f = sub.add_parser("build-full", help="plain PDF of a single commit")
    add_config_args(f)
    f.add_argument("--commit", default=None, help="ref to build (default: HEAD)")
    f.add_argument("-o", "--out", default=None, help="output PDF path")
    f.set_defaults(func=cmd_build_full)

    pg = sub.add_parser("pages", help="build a static GitHub Pages viewer")
    add_config_args(pg)
    pg.add_argument("-o", "--out", default=None, help="output site directory")
    pg.set_defaults(func=cmd_pages)

    sa = sub.add_parser("store-add",
                        help="build one diff and append it to a Pages store dir")
    add_config_args(sa)
    sa.add_argument("--old", default=None, help="base ref")
    sa.add_argument("--new", default=None, help="compare ref")
    sa.add_argument("--store", required=True, help="store directory (served by Pages)")
    sa.add_argument("--retain", type=int, default=50,
                    help="keep this many most-recent diffs (default 50)")
    sa.set_defaults(func=cmd_store_add)

    ss = sub.add_parser("store-seed",
                        help="append pages_recent/pages_pairs diffs to a store dir")
    add_config_args(ss)
    ss.add_argument("--store", required=True, help="store directory (served by Pages)")
    ss.add_argument("--retain", type=int, default=50,
                    help="keep this many most-recent diffs (default 50)")
    ss.set_defaults(func=cmd_store_seed)

    s = sub.add_parser("serve", help="run the interactive local web UI")
    add_config_args(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    project_help = "project name or folder (default: cwd's project, " \
                   "or the only one known)"

    sv = sub.add_parser("save", help="snapshot the project into its save timeline")
    sv.add_argument("--project", default=None,
                    help="project name or folder (default: cwd)")
    sv.add_argument("-m", "--message", default=None, help="save message")
    sv.add_argument("--from", dest="src", default=None, metavar="ZIP|DIR",
                    help="snapshot this source zip/folder instead of the "
                         "project folder (e.g. an Overleaf download)")
    sv.set_defaults(func=cmd_save)

    ls = sub.add_parser("list", help="list the project's saves")
    ls.add_argument("--project", default=None, help=project_help)
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_list)

    pj = sub.add_parser("projects", help="list every known project")
    pj.add_argument("--json", action="store_true", help="machine-readable output")
    pj.set_defaults(func=cmd_projects)

    dr = sub.add_parser("doctor",
                        help="check the tools each ldv feature needs")
    dr.add_argument("--json", action="store_true", help="machine-readable output")
    dr.set_defaults(func=cmd_doctor)

    df = sub.add_parser("diff",
                        help="latexdiff PDF between two saves/dates/refs")
    df.add_argument("a", help="base: save id (s1), date (2026-07-01), or ref")
    df.add_argument("b", nargs="?", default=None,
                    help="compare point (default: the latest save)")
    df.add_argument("--project", default=None, help=project_help)
    df.add_argument("-o", "--out", default=None, help="output PDF path")
    df.add_argument("--share", action="store_true",
                    help="publish to the project's share gist (secret, "
                         "anyone with the link can view) and print the link")
    df.set_defaults(func=cmd_diff)

    lk = sub.add_parser("link",
                        help="connect an Overleaf project (run from anywhere)")
    lk.add_argument("url", nargs="?", default=None,
                    help="Overleaf read-only share link (paste it whole — "
                         "the #fragment matters)")
    lk.add_argument("--git", default=None, metavar="URL|PROJECT_ID",
                    help="premium: clone the Overleaf git bridge instead")
    lk.add_argument("--name", default=None,
                    help="project name (default: the Overleaf project title)")
    lk.set_defaults(func=cmd_link)

    pl = sub.add_parser("pull", help="refresh saves from the linked Overleaf")
    pl.add_argument("--project", default=None, help=project_help)
    pl.set_defaults(func=cmd_pull)

    vw = sub.add_parser("view", help="browse saves in the local web viewer")
    vw.add_argument("--project", default=None, help=project_help)
    vw.add_argument("--host", default="127.0.0.1")
    vw.add_argument("--port", type=int, default=8765)
    vw.set_defaults(func=cmd_view)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
