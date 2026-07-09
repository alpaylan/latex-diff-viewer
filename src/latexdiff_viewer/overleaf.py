"""Overleaf origins for the save timeline.

Two kinds, recorded in the project's state.json as `origin`:

  * read-link (any plan, no account): `ldv link <read-url>` + `ldv pull`
    downloads the project source zip through the share link and snapshots it
    as a save. There is no official API for this — we do what a browser does:
    GET the read link to mint an anonymous session (cookie jar), find the
    project id in the page, then GET /project/<id>/download/zip. Overleaf can
    change this at any time, so every failure ends with the manual fallback:
    download the zip from Overleaf's menu and run `ldv save --from <zip>`.

  * git-bridge (Overleaf premium): `ldv link --git <url>` clones
    git.overleaf.com/<project-id> as the timeline itself; `ldv pull` is a
    git pull. Auth comes from the user's git credential store (Overleaf
    authentication tokens); we never prompt.

Works against overleaf.com and self-hosted instances (the base URL is taken
from the link itself).
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

from . import core, workspace

FALLBACK = ("could not fetch from Overleaf — download the source zip from "
            "Overleaf's Menu → Download → Source, then run "
            "`ldv save --from <zip>`")

_PID = r"[0-9a-f]{24}"


def _opener() -> urllib.request.OpenerDirector:
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", "latex-diff-viewer/ldv"),
                     ("Accept", "text/html,application/zip;q=0.9,*/*;q=0.8")]
    return op


def _find_pid(final_url: str, html: str) -> str | None:
    for pat in (rf"/project/({_PID})",
                rf'name="ol-project_id"\s+content="({_PID})"',
                rf'"project_id"\s*:\s*"({_PID})"'):
        m = re.search(pat, final_url) or re.search(pat, html)
        if m:
            return m.group(1)
    return None


def fetch_zip(read_url: str, dest_zip: str, timeout: int = 120) -> None:
    """Download the project source zip reachable through `read_url`."""
    parts = urllib.parse.urlsplit(read_url)
    base = f"{parts.scheme}://{parts.netloc}"
    op = _opener()
    with op.open(read_url, timeout=timeout) as r:
        final, html = r.geturl(), r.read(2_000_000).decode("utf-8", "replace")
    pid = _find_pid(final, html)
    if not pid:
        # Newer Overleaf wants the anonymous session to claim the token first.
        m = re.search(r'name="ol-csrfToken"\s+content="([^"]+)"', html)
        token = read_url.rstrip("/").rsplit("/", 1)[-1]
        if m:
            req = urllib.request.Request(
                f"{base}/read/{token}/grant", method="POST",
                data=b"{}", headers={"Content-Type": "application/json",
                                     "X-Csrf-Token": m.group(1)})
            try:
                with op.open(req, timeout=timeout) as r:
                    body = r.read(1_000_000).decode("utf-8", "replace")
                pid = _find_pid(r.geturl(), body)
            except OSError:
                pid = None
    if not pid:
        raise RuntimeError(f"no project id behind {read_url}; {FALLBACK}")
    with op.open(f"{base}/project/{pid}/download/zip", timeout=timeout) as r:
        data = r.read()
    if not data.startswith(b"PK"):
        raise RuntimeError(f"/project/{pid}/download/zip did not return a "
                           f"zip; {FALLBACK}")
    with open(dest_zip, "wb") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# link / pull
# ---------------------------------------------------------------------------

def link(project_dir: str, url: str | None = None,
         git_url: str | None = None) -> dict:
    """Record the project's Overleaf origin (and pull once)."""
    st = workspace.load_state(project_dir)
    if git_url:
        repo = workspace.shadow_root(project_dir)
        if os.path.isdir(os.path.join(repo, ".git")):
            return {"ok": False, "error":
                    "this project already has a timeline; git-bridge linking "
                    "needs a fresh one (remove "
                    f"{workspace.project_state_dir(project_dir)} first)"}
        if re.fullmatch(_PID, git_url):
            git_url = f"https://git.overleaf.com/{git_url}"
        os.makedirs(os.path.dirname(repo), exist_ok=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            subprocess.check_output(["git", "clone", "-q", git_url, repo],
                                    text=True, stderr=subprocess.STDOUT, env=env)
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error":
                    f"clone failed ({exc.output.strip()}) — store an Overleaf "
                    "git authentication token in your git credential helper "
                    "first (see overleaf.com/learn Git integration)"}
        st["origin"] = {"kind": "git-bridge", "url": git_url}
        workspace.write_state(project_dir, st)
        return {"ok": True, "origin": st["origin"]}

    if not url:
        return {"ok": False, "error": "need a read link or --git URL"}
    st["origin"] = {"kind": "read-link", "url": url}
    workspace.write_state(project_dir, st)
    res = pull(project_dir)
    res["origin"] = st["origin"]
    return res


def pull(project_dir: str) -> dict:
    """Refresh the timeline from the linked Overleaf origin."""
    origin = workspace.load_state(project_dir).get("origin") or {}
    kind = origin.get("kind")
    if kind == "read-link":
        with tempfile.TemporaryDirectory(prefix="ldv-pull-") as td:
            z = os.path.join(td, "overleaf.zip")
            try:
                fetch_zip(origin["url"], z)
            except (OSError, RuntimeError) as exc:
                return {"ok": False, "error": str(exc)}
            return workspace.save(project_dir, message="pull from Overleaf",
                                  src=z)
    if kind == "git-bridge":
        repo = workspace.shadow_root(project_dir)
        try:
            out = core.git(repo, "pull", "--ff-only", "-q", timeout=300)
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"git pull failed: {exc.output.strip()}"}
        head = core.git(repo, "rev-parse", "--short", "HEAD").strip()
        return {"ok": True, "commit": head, "output": out.strip()}
    return {"ok": False,
            "error": "no Overleaf origin — run `ldv link <read-url>` "
                     "or `ldv link --git <project>` first"}
