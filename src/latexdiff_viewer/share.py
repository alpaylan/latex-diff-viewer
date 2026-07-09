"""Share a save-timeline diff as a link: a secret gist + the hosted viewer.

The share target is a per-project secret gist holding the flat store layout
(manifest.json + the diff PDFs). The gist API is text-only, but every gist is
a git repo, so the PDFs go in by pushing the gist's git remote; `gh` supplies
credentials, so `gh auth login` is the only setup. The link is the project's
hosted viewer with the gist id in the URL fragment:

    https://<hosted-viewer>/#<gist-id>

The viewer (index.html remote mode) pulls manifest + PDFs from gist raw URLs
(they serve with CORS *). Secret gists are unlisted and off the user's
profile, but anyone with the link can read — that trade-off is the point.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from . import config as _config
from . import store, workspace

VIEWER_URL = os.environ.get(
    "LDV_VIEWER_URL", "https://alpaylan.github.io/latex-diff-viewer-demo/")
GIST_DESC = "latex-diff-viewer share store (PDF diffs; see README.md)"
# Gist raw URLs stop serving past ~10 MB; larger diffs need another medium.
RAW_LIMIT = 10 * 1024 * 1024


def _gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.check_output(["gh", *args], text=True, encoding="utf-8",
                                   input=input_text, stderr=subprocess.STDOUT)


def ensure_gh() -> str | None:
    """None when gh is ready, else a user-facing error."""
    try:
        _gh("auth", "status")
        return None
    except FileNotFoundError:
        return "GitHub CLI (gh) not found — install it and run `gh auth login`"
    except subprocess.CalledProcessError:
        return "gh is not authenticated — run `gh auth login`"


def _git(share_dir: str, *args: str) -> str:
    """git in the share clone, with gh as the (one-off) credential helper."""
    return subprocess.check_output(
        ["git", "-c", "credential.helper=",
         "-c", "credential.helper=!gh auth git-credential", *args],
        cwd=share_dir, text=True, encoding="utf-8", stderr=subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _mint_gist(project_dir: str) -> dict:
    """Create the project's secret gist; returns the share state entry."""
    with tempfile.TemporaryDirectory(prefix="ldv-gist-") as td:
        stub = os.path.join(td, "README.md")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("initialising…\n")
        out = _gh("gist", "create", "--desc", GIST_DESC, stub)
    url = [ln for ln in out.splitlines() if "gist.github.com" in ln][-1].strip()
    gist_id = url.rstrip("/").rsplit("/", 1)[-1]
    return {"kind": "gist", "id": gist_id, "gist_url": url,
            "link": f"{VIEWER_URL}#{gist_id}"}


def ensure_share(project_dir: str) -> dict:
    st = workspace.load_state(project_dir)
    if st.get("share", {}).get("id"):
        return st["share"]
    share = _mint_gist(project_dir)
    st["share"] = share
    workspace.write_state(project_dir, st)
    return share


def _ensure_clone(project_dir: str, share: dict) -> str:
    d = os.path.join(workspace.project_state_dir(project_dir), "share")
    if os.path.isdir(os.path.join(d, ".git")):
        _git(d, "fetch", "origin")
        _git(d, "reset", "--hard", "FETCH_HEAD")
    else:
        parent = os.path.dirname(d)
        os.makedirs(parent, exist_ok=True)
        _git(parent, "clone", "-q",
             f"https://gist.github.com/{share['id']}.git", d)
        # Pushes need an identity; keep it clone-local like the shadow repo.
        _git(d, "config", "user.email", "ldv@latex-diff-viewer")
        _git(d, "config", "user.name", "ldv")
    return d


def _finish_store_dir(share_dir: str, share: dict, project_name: str) -> None:
    """Adapt the store layout to gist hosting: gists can't serve index.html or
    honour .nojekyll, and the gist page itself should point at the viewer."""
    for name in ("index.html", ".nojekyll"):
        p = os.path.join(share_dir, name)
        if os.path.exists(p):
            os.remove(p)
    mp = store.manifest_path(share_dir)
    m = store.load_manifest(share_dir)
    if m:
        m["repo"] = project_name
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=2)
    with open(os.path.join(share_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {project_name} — LaTeX diffs\n\n"
                 f"**[Open the viewer]({share['link']})**\n\n"
                 "Built with [latex-diff-viewer]"
                 "(https://github.com/alpaylan/latex-diff-viewer). "
                 "This gist is the data store: `manifest.json` + diff PDFs.\n")


def share_diff(project_dir: str, a: str, b: str, retain: int = 20,
               on_line=None) -> dict:
    """Build a..b in the share store and push it; returns {ok, link, …}."""
    err = ensure_gh()
    if err:
        return {"ok": False, "error": err}
    repo = workspace.shadow_root(project_dir)
    share = ensure_share(project_dir)
    share_dir = _ensure_clone(project_dir, share)

    cfg = _config.load(repo)
    res = store.add_diff(cfg, share_dir, a, b, retain=retain, on_line=on_line)
    if not res.get("ok"):
        return res
    pdf = os.path.join(share_dir, res["pdf"])
    if os.path.getsize(pdf) > RAW_LIMIT:
        return {"ok": False, "id": res.get("id"),
                "error": f"{res['pdf']} is over 10 MB — gist raw URLs won't "
                         "serve it; use `ldv diff -o` and send the PDF"}
    _finish_store_dir(share_dir, share, os.path.basename(project_dir))

    _git(share_dir, "add", "-A")
    if _git(share_dir, "status", "--porcelain").strip():
        _git(share_dir, "commit", "-q", "-m", f"share {res.get('id', f'{a}..{b}')}")
        _git(share_dir, "push", "-q", "origin", "HEAD")
    return {"ok": True, "id": res.get("id"), "pdf": res.get("pdf"),
            "existing": res.get("existing", False),
            "changed_pages": res.get("changed_pages", 0),
            "link": share["link"], "gist": share.get("gist_url")}
