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
import json
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


def fetch_zip(read_url: str, dest_zip: str, timeout: int = 120) -> dict:
    """Download the project source zip reachable through `read_url`;
    returns {pid, name} (name from the zip's Content-Disposition)."""
    parts = urllib.parse.urlsplit(read_url)
    base = f"{parts.scheme}://{parts.netloc}"
    clean = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, ""))
    op = _opener()
    with op.open(clean, timeout=timeout) as r:
        final, html = r.geturl(), r.read(2_000_000).decode("utf-8", "replace")
    pid = _find_pid(final, html)
    if not pid:
        # The read link serves a grant interstitial; do what its JS does:
        # confirm the token, passing the link's #fragment as the hash check
        # (link-sharing v2 rejects the grant without it).
        m = re.search(r'name="ol-csrfToken"\s+content="([^"]+)"', html)
        if m:
            body: dict = {"confirmedByUser": False}
            if parts.fragment:
                body["tokenHashPrefix"] = f"#{parts.fragment}"
            req = urllib.request.Request(
                f"{base}{parts.path.rstrip('/')}/grant", method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "X-Csrf-Token": m.group(1)})
            try:
                with op.open(req, timeout=timeout) as r:
                    payload = r.read(1_000_000).decode("utf-8", "replace")
                try:
                    redirect = json.loads(payload).get("redirect", "")
                except ValueError:
                    redirect = ""
                pid = _find_pid(redirect, payload)
            except OSError:
                pid = None
    if not pid:
        raise RuntimeError(f"no project id behind {read_url}; {FALLBACK}")
    with op.open(f"{base}/project/{pid}/download/zip", timeout=timeout) as r:
        data = r.read()
        disposition = r.headers.get("Content-Disposition") or ""
    if not data.startswith(b"PK"):
        raise RuntimeError(f"/project/{pid}/download/zip did not return a "
                           f"zip; {FALLBACK}")
    with open(dest_zip, "wb") as fh:
        fh.write(data)
    # Overleaf names the zip after the project title — the natural name.
    m = re.search(r'filename="?([^";]+?)(\.zip)?"?\s*(;|$)', disposition)
    name = re.sub(r"[^\w. -]+", "", m.group(1)).strip() if m else ""
    return {"pid": pid, "name": name or f"overleaf-{pid[:6]}"}


# ---------------------------------------------------------------------------
# link / pull
# ---------------------------------------------------------------------------

def _unique_name(want: str, key: str) -> str:
    """`want`, unless another project already uses it."""
    for p in workspace.projects():
        if p["name"] == want and p["key"] not in (None, key):
            return f"{want}-{key.removeprefix(workspace.OL_PREFIX)[:6]}"
    return want


def link(url: str | None = None, git_url: str | None = None,
         name: str | None = None) -> dict:
    """Connect an Overleaf project (from anywhere — no local folder involved).

    The timeline is keyed on the Overleaf project id, so relinking the same
    project finds the same saves, and every other command addresses it by
    its name (from the project title, or --name)."""
    if git_url:
        if re.fullmatch(_PID, git_url):
            git_url = f"https://git.overleaf.com/{git_url}"
        m = re.search(rf"/({_PID})/?$", git_url)
        if not m:
            return {"ok": False, "error": f"no project id in {git_url!r}"}
        key = f"{workspace.OL_PREFIX}{m.group(1)}"
        repo = workspace.shadow_root(key)
        if not os.path.isdir(os.path.join(repo, ".git")):
            os.makedirs(os.path.dirname(repo), exist_ok=True)
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            try:
                subprocess.check_output(["git", "clone", "-q", git_url, repo],
                                        text=True, stderr=subprocess.STDOUT,
                                        env=env)
            except subprocess.CalledProcessError as exc:
                return {"ok": False, "error":
                        f"clone failed ({exc.output.strip()}) — store an "
                        "Overleaf git authentication token in your git "
                        "credential helper first (see overleaf.com/learn "
                        "Git integration)"}
        st = workspace.load_state(key)
        st["key"] = key
        st["origin"] = {"kind": "git-bridge", "url": git_url}
        st["name"] = _unique_name(name or st.get("name")
                                  or f"overleaf-{m.group(1)[:6]}", key)
        workspace.write_state(key, st)
        return {"ok": True, "project": st["name"], "origin": st["origin"]}

    if not url:
        return {"ok": False, "error": "need a read link or --git URL"}
    with tempfile.TemporaryDirectory(prefix="ldv-link-") as td:
        z = os.path.join(td, "overleaf.zip")
        try:
            meta = fetch_zip(url, z)
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        key = f"{workspace.OL_PREFIX}{meta['pid']}"
        st = workspace.load_state(key)
        st["key"] = key
        st["origin"] = {"kind": "read-link", "url": url}
        st["name"] = _unique_name(name or st.get("name") or meta["name"], key)
        workspace.write_state(key, st)
        res = workspace.save(key, message="pull from Overleaf", src=z)
    res.update(project=st["name"], origin=st["origin"])
    return res


def pull(project: str) -> dict:
    """Refresh the timeline from the linked Overleaf origin."""
    origin = workspace.load_state(project).get("origin") or {}
    kind = origin.get("kind")
    if kind == "read-link":
        with tempfile.TemporaryDirectory(prefix="ldv-pull-") as td:
            z = os.path.join(td, "overleaf.zip")
            try:
                fetch_zip(origin["url"], z)
            except (OSError, RuntimeError) as exc:
                return {"ok": False, "error": str(exc)}
            return workspace.save(project, message="pull from Overleaf",
                                  src=z)
    if kind == "git-bridge":
        repo = workspace.shadow_root(project)
        try:
            out = core.git(repo, "pull", "--ff-only", "-q", timeout=300)
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"git pull failed: {exc.output.strip()}"}
        head = core.git(repo, "rev-parse", "--short", "HEAD").strip()
        return {"ok": True, "commit": head, "output": out.strip()}
    return {"ok": False,
            "error": "no Overleaf origin — run `ldv link <read-url>` "
                     "or `ldv link --git <project>` first"}
